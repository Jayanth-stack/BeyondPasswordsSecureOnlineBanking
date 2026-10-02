"""Leftover claimed-employee list origination, staff money IDOR, and PII writes.

Prior coverage locked owner GET /listWires, claimed-employee /getCustomer
snapshot origination, claimed-admin ACH push/reject, and GET complete/recall
as a real tier2 teller. Claimed `employee` can still originate another
customer's queued wire via /listWires, auto-verify another customer's prenote
via list routes, complete/recall/release another customer's wire, return
another customer's ACH, rewrite another emp_id through /updateEmployee when
session userid matches JSON userid, and deactivate an arbitrary account by
claiming `tier2`.
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

_UPDATE_EMPLOYEE = {
    "userid": "bob",
    "emp_id": "admin1",
    "email": "stolen@b.com",
    "firstname": "Ada",
    "midname": "",
    "lastname": "Lovelace",
    "phone": "1",
    "dob": "d",
    "ssn": "999-00-0000",
    "address": "x",
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


class ClaimedEmployeeListSnapshotTests(unittest.TestCase):
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

    def test_claimed_employee_list_wires_transmits_another_customers_queue(self):
        wire = self._queue_wire(trace_id="emp-list-due")
        self.now[0] = MONDAY_MORNING
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get(
                "/listWires",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(listed.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_employee_list_links_auto_verifies_another_customers_prenote(self):
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="StolenPrenote", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        stored = self.link.store.get_link(link["link_id"])
        stored.expires_at = 0
        self.link.store.update_link(stored)
        self._session("bob", "employee")
        listed = self.client.get(
            "/listLinkedAccounts",
            json={"userid": "bob", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).userid, "alice")

    def test_claimed_employee_list_ach_auto_verifies_another_customers_prenote(self):
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="ListAchStolen", account_last4="3344")
        stored = self.link.store.get_link(link["link_id"])
        stored.expires_at = 0
        self.link.store.update_link(stored)
        self._session("bob", "employee")
        listed = self.client.get(
            "/listLinkedAch",
            json={"userid": "bob", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")


class ClaimedEmployeeStaffMoneyTests(unittest.TestCase):
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

        self.link = self.app_module.link_service
        self.link.store = MemoryLinkStore()
        self.link.amount_fn = lambda: (Decimal("0.12"), Decimal("0.47"))
        self.link.policy.challenge_secret = "unit-secret"
        self.link.policy.max_attempts = 3
        self.link.policy.min_amount = Decimal("1.00")
        self.link.policy.max_amount = Decimal("10000.00")
        self.link.policy.prenote_auto_accept = False
        set_link_service(self.link)

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

    def _send_wire(self, *, amount="40.00", trace_id="sent-1", **bene_kw):
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
                    "amount": amount,
                    "trace_id": trace_id,
                },
            )
        self.assertEqual(sent.status_code, 201)
        return sent.get_json()["wire"]

    def _add_and_verify_link(self, **overrides):
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
        link = added.get_json()["link"]
        confirmed = self.client.post(
            "/confirmLinkedAccount",
            json={
                "userid": "alice",
                "link_id": link["link_id"],
                "amount1": "0.12",
                "amount2": "0.47",
            },
        )
        self.assertEqual(confirmed.get_json()["link"]["status"], "verified")
        return link

    def test_claimed_employee_can_complete_another_customers_sent_wire(self):
        wire = self._send_wire(trace_id="emp-complete")
        self.assertEqual(wire["status"], "sent")
        self._session("bob", "employee")
        completed = self.client.get("/completeWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["userid"], "alice")
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")
        self.assertTrue(completed.get_json()["wire"]["omad"])

    def test_claimed_employee_can_recall_and_credit_another_customers_wire(self):
        wire = self._send_wire(
            trace_id="emp-recall", nickname="Ally", account_number="99887766"
        )
        self.assertEqual(wire["status"], "sent")
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            recalled = self.client.get("/recallWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()["wire"]["userid"], "alice")
        self.assertEqual(recalled.get_json()["wire"]["status"], "recalled")
        args, _kwargs = customers_cls.return_value.credit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_employee_can_release_another_customers_pending_wire(self):
        wire = self._send_wire(amount="10000.00", trace_id="emp-release")
        self.assertEqual(wire["status"], "pending_release")
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.get(
                "/releaseWire", json={"wire_id": wire["wire_id"]}
            )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["userid"], "alice")
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "10000.00")

    def test_claimed_employee_can_return_another_customers_ach(self):
        link = self._add_and_verify_link(nickname="ReturnMe", account_last4="5566")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            pushed = self.client.post(
                "/pushToLinked",
                json={
                    "userid": "alice",
                    "link_id": link["link_id"],
                    "amount": "20.00",
                    "trace_id": "emp-return-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()["movement"]["movement_id"]
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            returned = self.client.get(
                "/returnLinkedAch",
                json={"movement_id": movement_id, "reason": "unauthorized"},
            )
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()["movement"]["userid"], "alice")
        self.assertEqual(returned.get_json()["movement"]["status"], "returned")
        args, _kwargs = customers_cls.return_value.credit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")


class ClaimedEmployeeStaffPiiTests(unittest.TestCase):
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

    def test_claimed_employee_matching_userid_can_rewrite_another_emp_id(self):
        # Non-admin is only denied when session userid != JSON userid.
        self._session("bob", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post("/updateEmployee", json=_UPDATE_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_employee.assert_called_once_with(
            emp_id="admin1",
            email="stolen@b.com",
            firstname="Ada",
            midname="",
            lastname="Lovelace",
            phone="1",
            dob="d",
            ssn="999-00-0000",
            address="x",
        )

    def test_claimed_employee_get_load_employee_returns_staff_payload(self):
        self._session("bob", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {
                "first_name": "Bob",
                "ssn": "111-00-0000",
                "tier": 1,
            }
            emp_cls.return_value.fund_transfer_requests.return_value = "None"
            emp_cls.return_value.update_info_request_list.return_value = 0
            response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["ssn"], "111-00-0000")
        emp_cls.return_value.get_employee_details.assert_called_once_with("bob")

    def test_claimed_tier2_can_deactivate_arbitrary_account(self):
        self._session("bob", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_account.return_value = "Account Closed"
            response = self.client.get(
                "/deactivateAccount",
                json={"userid": "bob", "account_no": 9999},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_account.assert_called_once_with("bob", 9999)


if __name__ == "__main__":
    unittest.main()
