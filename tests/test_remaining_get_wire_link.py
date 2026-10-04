"""Leftover mutating GET admin/PII routes, claimed-role Fedwire/ACH, config drift.

Prior coverage locked money-route GETs, approve/deny GETs, and send/complete/
recall GETs. These paths still mutate, skip owner checks, or ignore BANK_LOG_FILE.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import inspect
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.link import MemoryLinkStore, set_service as set_link_service
from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    set_service as set_wire_service,
)

ET = timezone(timedelta(hours=-4))
FRIDAY_BEFORE_CUTOFF = datetime(2024, 6, 14, 16, 0, tzinfo=ET).timestamp()
FRIDAY_AFTER_CUTOFF = datetime(2024, 6, 14, 18, 0, tzinfo=ET).timestamp()
MONDAY_MORNING = datetime(2024, 6, 17, 10, 0, tzinfo=ET).timestamp()

_CUSTOMER_REG = {
    "empid": "None",
    "userid": "cust1",
    "password": "pw",
    "email": "a@b.com",
    "firstname": "A",
    "midname": "",
    "lastname": "B",
    "phone": "4155552671",
    "dob": "2000-01-01",
    "ssn": "123456789",
    "address": "x",
}

_EMPLOYEE_REG = {
    "userid": "admin9",
    "password": "pw",
    "email": "admin9@b.com",
    "firstname": "A",
    "midname": "",
    "lastname": "B",
    "phone": "4155552671",
    "dob": "2000-01-01",
    "ssn": "123456789",
    "address": "x",
    "tier": 3,
}

_MODIFY_EMPLOYEE = {
    "userid": "emp1",
    "emp_id": "emp9",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "999-00-0000",
    "dob": "d",
    "address": "x",
    "tier": 3,
}


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


class RemainingAdminGetMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid="alice", usertype="customer", emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_get_make_appointment_still_books(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.get(
                "/makeAppointment",
                json={"customer_id": "alice", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with(
            "alice", "10:00"
        )

    def test_get_approve_update_info_still_writes_pii(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.get(
                "/approveUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with("emp1", 9)

    def test_get_deny_update_info_still_mutates_without_userid_match(self):
        # Route checks usertype/tier only — JSON userid is not bound to session.
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Request Denied"
            response = self.client.get(
                "/denyUpdateInfo",
                json={"userid": "emp9", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("emp9", 9)

    def test_get_modify_employee_still_rewrites_tier(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp9", "L", "", "F", "1", "a@b.com", "999-00-0000", "d", "x", 3
        )

    def test_get_deactivate_account_still_calls_helper(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_account.return_value = "Account Closed"
            response = self.client.get(
                "/deactivateAccount",
                json={"userid": "emp1", "account_no": 42},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_account.assert_called_once_with("emp1", 42)

    def test_get_register_customer_still_creates(self):
        with patch("app.Customers") as customers_cls, patch(
            "app.url_for", return_value="/customer_dash"
        ):
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.get("/registerCustomer", json=_CUSTOMER_REG)
        self.assertIn(response.status_code, (301, 302))
        cust.create_customer_id.assert_called_once()

    def test_get_register_employee_still_creates_admin_tier(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = "Done"
            response = self.client.get("/registerEmployee", json=_EMPLOYEE_REG)
        self.assertEqual(response.status_code, 200)
        emp.create_employee.assert_called_once()
        self.assertEqual(emp.create_employee.call_args.args[9], 3)

    def test_get_verify_otp_still_redirects(self):
        self._session()
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/customer_dash", response.headers.get("Location", ""))

    def test_get_logout_still_clears_session(self):
        self._session()
        response = self.client.get("/logout", json={"userid": "alice"})
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertNotIn("userid", sess)
            self.assertNotIn("usertype", sess)

    def test_open_account_tier1_usertype_is_forbidden(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "emp1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_get_system_logs_ignores_bank_log_file_env(self):
        src = inspect.getsource(self.app_module.get_system_logs)
        self.assertIn("SystemLogs", src)
        self.assertIn("bank.log", src)
        self.assertNotIn("BANK_LOG_FILE", src)


class RemainingWireStaffGetAndIdorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.wire_service
        self.service.store = MemoryWireStore()
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.clock = lambda: FRIDAY_BEFORE_CUTOFF
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        set_wire_service(self.service)

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

    def _add_and_send(self, *, amount="40.00", trace_id="w1", **bene_kw):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload(**bene_kw))
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": amount,
                    "trace_id": trace_id,
                },
            )
        return sent, customers_cls

    def test_get_cancel_queued_wire_still_mutates(self):
        self.service.clock = lambda: FRIDAY_AFTER_CUTOFF
        self._session()
        sent, _ = self._add_and_send(trace_id="get-cancel")
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        cancelled = self.client.get(
            "/cancelWire", json={"wire_id": sent.get_json()["wire"]["wire_id"]}
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["wire"]["status"], "cancelled")

    def test_get_release_pending_wire_still_debits(self):
        self._session("maker", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(userid="maker", customer_id="alice"),
            )
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            pending = self.client.post(
                "/sendWire",
                json={
                    "userid": "maker",
                    "customer_id": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "10000.00",
                    "trace_id": "get-release",
                },
            )
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()["wire"]["status"], "pending_release")
        self._session("checker", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.get(
                "/releaseWire",
                json={"wire_id": pending.get_json()["wire"]["wire_id"]},
            )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "10000.00")

    def test_get_override_ofac_still_transmits(self):
        self._session()
        sent, _ = self._add_and_send(
            trace_id="get-ofac", nickname="Blocked", legal_name="Blocked Person"
        )
        self.assertEqual(sent.get_json()["wire"]["status"], "held")
        self._session("teller", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            overridden = self.client.get(
                "/overrideOfac",
                json={"wire_id": sent.get_json()["wire"]["wire_id"]},
            )
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()["wire"]["status"], "sent")
        customers_cls.return_value.debit_request.assert_called()

    def test_get_waive_then_run_due_skips_fee_debit(self):
        self.service.clock = lambda: FRIDAY_AFTER_CUTOFF
        self._session()
        sent, _ = self._add_and_send(trace_id="get-waive")
        wire_id = sent.get_json()["wire"]["wire_id"]
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        self._session("teller", "tier2")
        waived = self.client.get("/waiveWireFee", json={"wire_id": wire_id})
        self.assertEqual(waived.status_code, 200)
        self.assertEqual(waived.get_json()["wire"]["fee_status"], "waived")
        self.service.clock = lambda: MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            due = self.client.get(
                "/runDueWires",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(due.status_code, 200)
        stored = self.service.store.get_wire(wire_id)
        self.assertEqual(stored.status, "sent")
        amounts = [
            call.args[1] for call in customers_cls.return_value.debit_request.call_args_list
        ]
        self.assertEqual(amounts, ["40.00"])

    def test_claimed_admin_can_cancel_another_customers_queued_wire(self):
        self.service.clock = lambda: FRIDAY_AFTER_CUTOFF
        self._session()
        sent, _ = self._add_and_send(trace_id="admin-cancel")
        wire_id = sent.get_json()["wire"]["wire_id"]
        self._session("bob", "admin")
        cancelled = self.client.get("/cancelWire", json={"wire_id": wire_id})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["wire"]["status"], "cancelled")
        self.assertEqual(cancelled.get_json()["wire"]["userid"], "alice")

    def test_claimed_admin_run_due_transmits_another_customers_queue(self):
        self.service.clock = lambda: FRIDAY_AFTER_CUTOFF
        self._session()
        sent, _ = self._add_and_send(trace_id="admin-due")
        wire_id = sent.get_json()["wire"]["wire_id"]
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        self.service.clock = lambda: MONDAY_MORNING
        self._session("bob", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            due = self.client.get(
                "/runDueWires",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(due.status_code, 200)
        stored = self.service.store.get_wire(wire_id)
        self.assertEqual(stored.status, "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")


class RemainingLinkGetMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.link_service
        self.service.store = MemoryLinkStore()
        self.service.amount_fn = lambda: (Decimal("0.12"), Decimal("0.47"))
        self.service.policy.challenge_secret = "unit-secret"
        self.service.policy.max_attempts = 3
        self.service.policy.min_amount = Decimal("1.00")
        self.service.policy.max_amount = Decimal("10000.00")
        set_link_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_get_force_verify_and_pull_still_move_money(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addLinkedAccount",
                json={
                    "userid": "alice",
                    "nickname": "Chase",
                    "default_account": "1001",
                    "routing_last4": "0210",
                    "account_last4": "7788",
                },
            )
        self.assertEqual(added.status_code, 201)
        link_id = added.get_json()["link"]["link_id"]
        self._session("teller", "tier1")
        verified = self.client.get(
            "/forceVerifyLinkedAccount", json={"link_id": link_id}
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.get_json()["link"]["status"], "verified")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.credit_request.return_value = "Success"
            pulled = self.client.get(
                "/pullFromLinked",
                json={
                    "userid": "teller",
                    "customer_id": "alice",
                    "link_id": link_id,
                    "amount": "20.00",
                    "trace_id": "get-pull-1",
                },
            )
        self.assertEqual(pulled.status_code, 201)
        customers_cls.return_value.credit_request.assert_called()
        args, kwargs = customers_cls.return_value.credit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")

    def test_claimed_admin_can_force_verify_another_customers_link(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addLinkedAccount",
                json={
                    "userid": "alice",
                    "nickname": "Ally",
                    "default_account": "1001",
                    "routing_last4": "0210",
                    "account_last4": "9900",
                },
            )
        link_id = added.get_json()["link"]["link_id"]
        self._session("bob", "admin")
        verified = self.client.get(
            "/forceVerifyLinkedAccount", json={"link_id": link_id}
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.get_json()["link"]["userid"], "alice")
        self.assertEqual(verified.get_json()["link"]["status"], "verified")


if __name__ == "__main__":
    unittest.main()
