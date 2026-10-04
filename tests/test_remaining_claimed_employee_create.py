"""Leftover claimed-employee wire/ACH create, confirm, and beneficiary-list origination.

Prior coverage locked claimed `employee` pause/archive/resume, ACH push/close,
and owner GET /listWireBeneficiaries snapshot. Sibling create routes still
trust session `usertype` as a real teller: claimed `employee` can add or send
a wire for another customer, add/confirm/resend another customer's linked
account, and transmit another customer's queued wire via GET
/listWireBeneficiaries + customer_id.
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


class ClaimedEmployeeWireCreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.now = [FRIDAY_BEFORE_CUTOFF]
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

    def _add_beneficiary(self, **overrides):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload(**overrides))
        self.assertEqual(added.status_code, 201)
        return added.get_json()["beneficiary"]

    def _queue_wire(self, *, trace_id="queued-1", **bene_kw):
        self.now[0] = FRIDAY_AFTER_CUTOFF
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

    def test_claimed_employee_can_add_beneficiary_for_another_customer(self):
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get(
                "/addWireBeneficiary",
                json=self._bene_payload(
                    userid="bob",
                    customer_id="alice",
                    nickname="StolenChase",
                    account_number="11112222",
                ),
            )
        self.assertEqual(added.status_code, 201)
        body = added.get_json()["beneficiary"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["nickname"], "StolenChase")
        self.assertEqual(body["status"], "active")
        stored = self.service.store.get_beneficiary(body["beneficiary_id"])
        self.assertEqual(stored.userid, "alice")
        self.assertEqual(stored.actor, "bob")

    def test_claimed_employee_can_send_wire_for_another_customer(self):
        bene = self._add_beneficiary(nickname="SendMe", account_number="33334444")
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendWire",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "beneficiary_id": bene["beneficiary_id"],
                    "amount": "40.00",
                    "trace_id": "emp-send-alice",
                },
            )
        self.assertEqual(sent.status_code, 201)
        body = sent.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "sent")
        stored = self.service.store.get_wire(body["wire_id"])
        self.assertEqual(stored.userid, "alice")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_employee_list_beneficiaries_transmits_another_customers_queue(self):
        # /listWireBeneficiaries shares handle_list_wires; snapshot still run_due.
        wire = self._queue_wire(trace_id="emp-bene-list-due")
        self.now[0] = MONDAY_MORNING
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get(
                "/listWireBeneficiaries",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(listed.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")


class ClaimedEmployeeLinkCreateTests(unittest.TestCase):
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
        self.link.policy.max_resends = 3
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

    def test_claimed_employee_can_add_link_for_another_customer(self):
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get(
                "/addLinkedAccount",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "nickname": "StolenChase",
                    "default_account": "1001",
                    "routing_last4": "0210",
                    "account_last4": "8899",
                },
            )
        self.assertEqual(added.status_code, 201)
        body = added.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["nickname"], "StolenChase")
        self.assertEqual(body["status"], "pending")
        stored = self.link.store.get_link(body["link_id"])
        self.assertEqual(stored.userid, "alice")
        self.assertEqual(stored.actor, "bob")
        self.assertTrue(stored.challenge_digest)

    def test_claimed_employee_can_confirm_another_customers_link(self):
        link = self._add_link(nickname="ConfirmMe", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._session("bob", "employee")
        confirmed = self.client.get(
            "/confirmLinkedAccount",
            json={
                "userid": "bob",
                "link_id": link["link_id"],
                "amount1": "0.12",
                "amount2": "0.47",
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        body = confirmed.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_claimed_employee_can_resend_another_customers_challenge(self):
        link = self._add_link(nickname="ResendMe", account_last4="3344")
        before = self.link.store.get_link(link["link_id"]).challenge_digest
        self.link.amount_fn = lambda: (Decimal("0.21"), Decimal("0.34"))
        self._session("bob", "employee")
        resent = self.client.get(
            "/resendLinkedChallenge",
            json={"userid": "bob", "link_id": link["link_id"]},
        )
        self.assertEqual(resent.status_code, 200)
        body = resent.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "pending")
        stored = self.link.store.get_link(link["link_id"])
        self.assertNotEqual(before, stored.challenge_digest)
        self.assertEqual(stored.resends, 1)
        self.assertEqual(stored.actor, "bob")


if __name__ == "__main__":
    unittest.main()
