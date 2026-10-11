"""Leftover GET lookup mutations and claimed-employee IDOR.

getCustomer / getAppointmentList bind customer usertype only (claimed
employee skips the userid match). getCustomer also calls _wire_snapshot,
which run_due-transmits queued wires. verify-otp is registered for GET.
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


class RemainingGetLookupMutationTests(unittest.TestCase):
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
        return sent.get_json()["wire"]

    def test_claimed_employee_get_customer_skips_userid_match(self):
        # Allowed usertypes are tier1/tier2/employee. JSON userid is unused.
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {
                "ssn": "hidden",
                "first_name": "Ada",
            }
            response = self.client.get(
                "/getCustomer",
                json={"userid": "someone-else", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_customer_details.assert_called_once_with("alice")
        self.assertEqual(response.get_json()["Info"]["ssn"], "hidden")

    def test_claimed_employee_get_customer_transmits_queued_wire(self):
        # GET lookup still calls _wire_snapshot → run_due for the requested
        # customer_id. Claimed employee is treated as staff; no ownership bind.
        wire = self._queue_wire(trace_id="get-cust-due")
        self.now[0] = MONDAY_MORNING
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {
                "first_name": "Ada"
            }
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/getCustomer",
                json={"userid": "mallory", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")
        self.assertEqual(response.get_json()["Wires"]["wires"][0]["status"], "sent")

    def test_get_load_customer_transmits_queued_wire(self):
        wire = self._queue_wire(trace_id="dash-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {
                "first_name": "Ada"
            }
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            dash = self.client.get("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_employee_get_appointment_list_for_another_customer(self):
        # customer_id bind only applies when usertype == customer.
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [
                (1, "alice", "10:00", 1)
            ]
            response = self.client.get(
                "/getAppointmentList",
                json={"customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_appointment.assert_called_once_with("alice")
        self.assertEqual(response.get_json()["message"][0][1], "alice")

    def test_get_verify_otp_still_completes_mfa(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = (
                "+14155552671"
            )
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/customer_dash", response.headers.get("Location", ""))
        twilio.verify.v2.services.return_value.verification_checks.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
