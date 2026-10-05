"""Leftover claimed-employee reject/cancel/runDue, ACH force-verify/prenote/pull, and PII.

Prior coverage locked claimed `employee` OFAC override, fee waive, ACH settle,
preview origination, and GET /approveUpdateInfo. Sibling staff routes still
trust session `usertype` as a real teller: claimed `employee` can reject or
cancel another customer's queued wire, transmit another customer's queue via
GET /runDueWires, force-verify or accept another customer's link/prenote, pull
ACH into another customer's account, rewrite another emp_id through GET
/modifyEmployee when session userid matches JSON userid, and deny PII updates
by claiming `employee` with emp_tier >= 2.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
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

_MODIFY_EMPLOYEE = {
    "userid": "bob",
    "emp_id": "admin1",
    "last_name": "Lovelace",
    "middle_name": "",
    "first_name": "Ada",
    "contact_no": "1",
    "email_id": "stolen@b.com",
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


class ClaimedEmployeeRejectCancelRunDueTests(unittest.TestCase):
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

    def _queue_wire(self, *, trace_id="queued-1", **bene_kw):
        self._session()
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
                    "amount": "40.00",
                    "trace_id": trace_id,
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        return sent.get_json()["wire"]

    def test_claimed_employee_can_reject_another_customers_queued_wire(self):
        wire = self._queue_wire(trace_id="emp-reject")
        self._session("bob", "employee")
        rejected = self.client.get("/rejectWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(rejected.status_code, 200)
        body = rejected.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "rejected")

    def test_claimed_employee_can_cancel_another_customers_queued_wire(self):
        wire = self._queue_wire(trace_id="emp-cancel")
        self._session("bob", "employee")
        cancelled = self.client.get("/cancelWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(cancelled.status_code, 200)
        body = cancelled.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "cancelled")

    def test_claimed_employee_run_due_transmits_another_customers_queue(self):
        wire = self._queue_wire(trace_id="emp-due")
        self.now[0] = MONDAY_MORNING
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            due = self.client.get(
                "/runDueWires",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(due.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")


class ClaimedEmployeeAchForcePrenotePullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.link = self.app_module.link_service
        self.link.store = MemoryLinkStore()
        self.link.amount_fn = lambda: (Decimal("0.12"), Decimal("0.47"))
        self.link.policy.challenge_secret = "unit-secret"
        self.link.policy.max_attempts = 3
        self.link.policy.min_amount = Decimal("1.00")
        self.link.policy.max_amount = Decimal("10000.00")
        self.link.policy.prenote_auto_accept = False
        self.link.policy.prenote_wait_seconds = 2 * 86400
        set_link_service(self.link)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_link(self, **overrides):
        self._session()
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "default_account": "1001",
            "routing_last4": "0210",
            "account_last4": "7788",
        }
        body.update(overrides)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addLinkedAccount", json=body)
        self.assertEqual(added.status_code, 201)
        return added.get_json()["link"]

    def _verify(self, link_id):
        confirmed = self.client.post(
            "/confirmLinkedAccount",
            json={
                "userid": "alice",
                "link_id": link_id,
                "amount1": "0.12",
                "amount2": "0.47",
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.get_json()["link"]["status"], "verified")

    def test_claimed_employee_can_force_verify_another_customers_link(self):
        link = self._add_link(nickname="ForceMe", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._session("bob", "employee")
        verified = self.client.get(
            "/forceVerifyLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(verified.status_code, 200)
        body = verified.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_claimed_employee_can_accept_another_customers_prenote(self):
        self.link.policy.prenote_wait_seconds = 0
        link = self._add_link(method="prenote", nickname="AcceptMe", account_last4="3344")
        self.assertEqual(link["status"], "pending")
        self._session("bob", "employee")
        accepted = self.client.get("/acceptPrenote", json={"link_id": link["link_id"]})
        self.assertEqual(accepted.status_code, 200)
        body = accepted.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_claimed_employee_can_pull_another_customers_linked_account(self):
        link = self._add_link(nickname="PullMe", account_last4="5566")
        self._verify(link["link_id"])
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.credit_request.return_value = "Success"
            pulled = self.client.get(
                "/pullFromLinked",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "link_id": link["link_id"],
                    "amount": "20.00",
                    "trace_id": "emp-pull-1",
                },
            )
        self.assertEqual(pulled.status_code, 201)
        self.assertEqual(pulled.get_json()["movement"]["userid"], "alice")
        args, _kwargs = customers_cls.return_value.credit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")


class ClaimedEmployeeModifyDenyPiiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid, usertype, emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_claimed_employee_get_modify_employee_rewrites_another_emp_id(self):
        # Non-admin is only denied when session userid != JSON userid.
        # Matching claimed-employee userid skips the check even if emp_id is
        # someone else, so GET can rewrite SSN/tier.
        self._session("bob", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "admin1",
            "Lovelace",
            "",
            "Ada",
            "1",
            "stolen@b.com",
            "999-00-0000",
            "d",
            "x",
            3,
        )

    def test_claimed_employee_get_deny_update_info_invokes_helper(self):
        # Route checks usertype/tier only — JSON userid is not bound to session.
        self._session("bob", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Request Denied"
            response = self.client.get(
                "/denyUpdateInfo",
                json={"userid": "alice", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("alice", 9)


if __name__ == "__main__":
    unittest.main()
