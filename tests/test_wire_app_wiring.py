"""Real app.py wiring for Fedwire — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_wire_service(); session usertype is trusted as the wire actor role.
Clock is pinned to Friday 2024-06-14 11:00 ET so cutoff/weekend cannot flake.
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
        "savings": {"Account": 1002, "Balance": 80},
        "credit": "None",
    }


class WireAppWiringTests(unittest.TestCase):
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
        self.service.clock = lambda: self.now[0]
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
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

    def test_load_customer_includes_wires_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account_number", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]
        self.assertEqual(wires["beneficiaries"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", wires["beneficiaries"][0])
        self.assertTrue(wires["enabled"])

    def test_claimed_admin_session_can_wire_another_customer(self):
        # /login stores client-supplied usertype; wire routes trust it as staff.
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
        self._session("bob", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "admin-idor-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["userid"], "alice")
        self.assertEqual(sent.get_json()["wire"]["status"], "sent")
        customers_cls.return_value.debit_request.assert_called()

    def test_accounts_helper_failure_allows_any_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(default_account="9999"),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["default_account"], "9999")

    def test_get_add_and_send_still_mutate(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.get("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.get(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "get-send-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["status"], "sent")
        customers_cls.return_value.debit_request.assert_called()
        args, kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Wires"]["enabled"], False)
        self.assertEqual(dash.get_json()["Wires"]["beneficiaries"], [])

    def test_claimed_employee_get_pauses_another_customers_beneficiary(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(nickname="BobsTarget")
            )
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
        self._session("bob", "employee")
        paused = self.client.get(
            "/pauseWireBeneficiary",
            json={"userid": "bob", "beneficiary_id": bene_id},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        self.assertEqual(paused.get_json()["beneficiary"]["userid"], "alice")

    def test_claimed_employee_get_run_due_transmits_another_customers_queued_wire(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(nickname="Queued")
            )
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "33.00",
                    "trace_id": "due-idor-1",
                },
            )
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.now[0] = ts(2024, 6, 17, 10, 0)
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            due = self.client.get(
                "/runDueWires",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(due.status_code, 200)
        wires = due.get_json()["Wires"]["wires"]
        self.assertEqual(wires[0]["status"], "sent")
        customers_cls.return_value.debit_request.assert_called()

    def test_preview_does_not_debit_and_customer_waive_is_403(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            preview = self.client.post(
                "/previewWire",
                json={"userid": "alice", "beneficiary_id": bene_id, "amount": "33.00"},
            )
            self.assertEqual(preview.status_code, 200)
            self.assertIn("preview", preview.get_json())
            customers_cls.return_value.debit_request.assert_not_called()
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "33.00",
                    "trace_id": "preview-q",
                },
            )
        self.assertEqual(queued.status_code, 201)
        waived = self.client.post(
            "/waiveWireFee",
            json={"userid": "alice", "wire_id": queued.get_json()["wire"]["wire_id"]},
        )
        self.assertEqual(waived.status_code, 403)
        self.assertEqual(waived.get_json()["error"], "wire_forbidden")
        cancelled = self.client.get(
            "/cancelWire",
            json={"userid": "alice", "wire_id": queued.get_json()["wire"]["wire_id"]},
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["wire"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
