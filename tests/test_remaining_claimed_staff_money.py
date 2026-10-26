"""Leftover claimed-employee ACH list auto-verify and staff money IDOR.

Prior coverage locked GET /listWires /previewWire snapshot origination.
Sibling list/staff routes still trust session `usertype` as a real teller:
claimed `employee` can auto-verify another customer's expired prenote via
GET /listLinkedAccounts and /listLinkedAch, complete/recall/release another
customer's wire, and credit another customer's ACH push via GET /returnLinkedAch.
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


class RemainingClaimedStaffMoneyTests(unittest.TestCase):
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
        self.link.clock = lambda: FRIDAY_AFTER_CUTOFF
        set_link_service(self.link)

    def _session(self, userid="alice", usertype="customer", emp_tier=None):
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

    def _send_wire(self, *, amount="40.00", trace_id="sent-1", **bene_kw):
        self._session("alice", "customer")
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

    def _add_link(self, **overrides):
        self._session("alice", "customer")
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "default_account": "1001",
            "routing_last4": "0210",
            "account_last4": "7788",
            "method": "prenote",
        }
        body.update(overrides)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addLinkedAccount", json=body)
        self.assertEqual(added.status_code, 201)
        return added.get_json()["link"]

    def _add_and_verify_link(self, **overrides):
        self._session("alice", "customer")
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

    def _expire_prenote(self, link_id):
        stored = self.link.store.get_link(link_id)
        stored.expires_at = 0
        self.link.store.update_link(stored)
        self.assertEqual(self.link.store.get_link(link_id).status, "pending")

    def test_claimed_employee_list_links_auto_verifies_another_customers_prenote(self):
        # snapshot → expire_due auto-accepts expired prenotes for the
        # customer_id claimed-employee staff sessions can point at.
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(nickname="StolenPrenote", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._expire_prenote(link["link_id"])
        self._session("mallory", "employee", emp_tier=1)
        listed = self.client.get(
            "/listLinkedAccounts",
            json={"userid": "mallory", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        stored = self.link.store.get_link(link["link_id"])
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.userid, "alice")
        self.assertEqual(listed.get_json()["LinkedAccounts"]["links"][0]["status"], "verified")

    def test_claimed_employee_list_ach_auto_verifies_another_customers_prenote(self):
        # /listLinkedAch also snapshots the claimed staff customer_id.
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(nickname="ListAchStolen", account_last4="3344")
        self._expire_prenote(link["link_id"])
        self._session("mallory", "employee", emp_tier=1)
        listed = self.client.get(
            "/listLinkedAch",
            json={"userid": "mallory", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_claimed_employee_can_complete_another_customers_sent_wire(self):
        # complete_wire only checks EMPLOYEE_ROLES; GET still mutates.
        wire = self._send_wire(trace_id="emp-complete")
        self.assertEqual(wire["status"], "sent")
        self._session("mallory", "employee", emp_tier=1)
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
        self._session("mallory", "employee", emp_tier=1)
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
        # Dual-control pending_release was originated by alice, so claimed
        # employee is a "different" releaser and transmits (debit + IMAD).
        wire = self._send_wire(amount="10000.00", trace_id="emp-release")
        self.assertEqual(wire["status"], "pending_release")
        self._session("mallory", "employee", emp_tier=1)
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
        self._session("mallory", "employee", emp_tier=1)
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


if __name__ == "__main__":
    unittest.main()
