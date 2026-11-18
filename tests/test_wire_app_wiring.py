"""Real app.py wiring: read endpoints still settle queued Fedwires.

listWires / listWireBeneficiaries close over the import-time service and
call snapshot → run_due. loadCustomer / getCustomer go through
get_wire_service(). A GET that looks like a dashboard read can debit.
Unique vs open PRs that lock send/add GET mutations and dedicated
/runDueWires IDOR.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.wire import MemoryWireStore, WireCalendar, set_service

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 5000},
        "savings": {"Account": 1002, "Balance": 10},
        "credit": "None",
    }


class WireSnapshotRunDueWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.service = self.app_module.wire_service
        self.service.store = MemoryWireStore()
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.service.clock = lambda: self.now[0]
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _bene_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "legal_name": "Ada Lovelace",
            "aba": "021000021",
            "account_number": "77881234",
            "street": "1 Federal St",
            "city": "New York",
            "state": "NY",
            "postal": "10004",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def _queue_alice_wire(self, customers_cls, trace_id):
        customers_cls.return_value.get_all_account.return_value = _accounts()
        added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
        self.now[0] = ts(2024, 6, 14, 18, 0)
        queued = self.client.post(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "40.00",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        customers_cls.return_value.debit_request.assert_not_called()
        return queued.get_json()["wire"]["wire_id"]

    def test_get_load_customer_transmits_due_queued_wire(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            wire_id = self._queue_alice_wire(customers_cls, "dash-due-1")
            self.now[0] = ts(2024, 6, 17, 10, 0)
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            dash = self.client.get("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        wires = dash.get_json()["Wires"]["wires"]
        match = [row for row in wires if row["wire_id"] == wire_id]
        self.assertEqual(match[0]["status"], "sent")
        self.assertNotIn("account_number", match[0])
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)
        args, kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")
        self.assertIn("wire to Chase", kwargs.get("remark") or args[2])

    def test_get_list_wires_transmits_due_queued_wire(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            wire_id = self._queue_alice_wire(customers_cls, "list-due-1")
            self.now[0] = ts(2024, 6, 17, 10, 0)
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWires", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        match = [
            row for row in listed.get_json()["Wires"]["wires"] if row["wire_id"] == wire_id
        ]
        self.assertEqual(match[0]["status"], "sent")
        self.assertTrue(match[0]["imad"])
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)

    def test_get_list_beneficiaries_transmits_due_queued_wire(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            wire_id = self._queue_alice_wire(customers_cls, "bene-due-1")
            self.now[0] = ts(2024, 6, 17, 10, 0)
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWireBeneficiaries", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        match = [
            row for row in listed.get_json()["Wires"]["wires"] if row["wire_id"] == wire_id
        ]
        self.assertEqual(match[0]["status"], "sent")
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)

    def test_set_service_none_does_not_stop_closed_over_list_run_due(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            wire_id = self._queue_alice_wire(customers_cls, "closed-due-1")
            set_service(None)
            self.now[0] = ts(2024, 6, 17, 10, 0)
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWires", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        match = [
            row for row in listed.get_json()["Wires"]["wires"] if row["wire_id"] == wire_id
        ]
        self.assertEqual(match[0]["status"], "sent")
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)

    def test_claimed_employee_get_customer_transmits_another_customers_queue(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            wire_id = self._queue_alice_wire(customers_cls, "staff-due-1")
        self._session("teller", "employee")
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        match = [
            row for row in response.get_json()["Wires"]["wires"] if row["wire_id"] == wire_id
        ]
        self.assertEqual(match[0]["status"], "sent")
        self.assertNotIn("account_number", match[0])
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
