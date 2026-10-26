"""Leftover GET list/preview snapshot origination and claimed-employee IDOR.

getCustomer/loadCustomer already lock _wire_snapshot → run_due. Sibling
list/preview routes still transmit queued wires the same way. Claimed
`employee` can also point handle_list_wires / handle_preview at another
customer_id and debit that queue.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    set_service as set_wire_service,
)

ET = timezone(timedelta(hours=-4))
FRIDAY_AFTER_CUTOFF = datetime(2024, 6, 14, 18, 0, tzinfo=ET).timestamp()
MONDAY_MORNING = datetime(2024, 6, 17, 10, 0, tzinfo=ET).timestamp()


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 500},
        "savings": {"Account": 1002, "Balance": 10},
        "credit": "None",
    }


class RemainingListPreviewSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.now = [FRIDAY_AFTER_CUTOFF]
        self.service = self.app_module.wire_service
        self.service.store = MemoryWireStore()
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.clock = lambda: self.now[0]
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        set_wire_service(self.service)

    def _session(self, userid, usertype, emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

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

    def _queue_wire(self, *, trace_id="queued-1"):
        self._session("alice", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": trace_id,
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        return sent.get_json()["wire"], bene_id

    def _assert_transmitted(self, wire, customers_cls):
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_get_list_wires_transmits_queued_via_snapshot(self):
        # GET list still calls snapshot → run_due for the session owner.
        wire, _bene_id = self._queue_wire(trace_id="list-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWires", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        self._assert_transmitted(wire, customers_cls)
        self.assertEqual(listed.get_json()["Wires"]["wires"][0]["status"], "sent")

    def test_get_list_wire_beneficiaries_transmits_queued_via_snapshot(self):
        # /listWireBeneficiaries shares handle_list_wires; snapshot still run_due.
        wire, _bene_id = self._queue_wire(trace_id="bene-list-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get(
                "/listWireBeneficiaries", json={"userid": "alice"}
            )
        self.assertEqual(listed.status_code, 200)
        self._assert_transmitted(wire, customers_cls)

    def test_get_preview_wire_transmits_queued_via_snapshot(self):
        # Preview is read-shaped but still returns snapshot → run_due.
        wire, bene_id = self._queue_wire(trace_id="preview-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            previewed = self.client.get(
                "/previewWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "50.00",
                },
            )
        self.assertEqual(previewed.status_code, 200)
        self.assertEqual(previewed.get_json()["preview"]["amount"], "50.00")
        self._assert_transmitted(wire, customers_cls)

    def test_claimed_employee_get_list_wires_transmits_another_customers_queue(self):
        # Claimed employee is treated as staff; customer_id is not bound to a
        # real role. GET list still run_due-transmits that owner's queue.
        wire, _bene_id = self._queue_wire(trace_id="emp-list-due")
        self.now[0] = MONDAY_MORNING
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get(
                "/listWires",
                json={"userid": "mallory", "customer_id": "alice"},
            )
        self.assertEqual(listed.status_code, 200)
        self._assert_transmitted(wire, customers_cls)
        self.assertEqual(listed.get_json()["Wires"]["wires"][0]["userid"], "alice")

    def test_claimed_employee_get_preview_wire_transmits_another_customers_queue(self):
        # handle_preview uses _owner_userid(customer_id) for EMPLOYEE_ROLES,
        # then snapshot run_due on that owner.
        wire, bene_id = self._queue_wire(trace_id="emp-preview-due")
        self.now[0] = MONDAY_MORNING
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            previewed = self.client.get(
                "/previewWire",
                json={
                    "userid": "mallory",
                    "customer_id": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "50.00",
                },
            )
        self.assertEqual(previewed.status_code, 200)
        self.assertEqual(previewed.get_json()["preview"]["amount"], "50.00")
        self._assert_transmitted(wire, customers_cls)


if __name__ == "__main__":
    unittest.main()
