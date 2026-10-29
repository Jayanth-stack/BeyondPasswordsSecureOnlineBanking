"""Leftover claimed-admin complete/recall/release/OFAC/list IDOR and owner GET.

Prior coverage locked claimed `admin` create/cancel/ACH pull and owner
resume/pause/close. Sibling `admin` is still just a client-claimed session
usertype in EMPLOYEE_ROLES: claimed `admin` can complete, recall, or release
another customer's wire; override another customer's OFAC hold; waive
another customer's queued fee; settle or return another customer's ACH;
transmit another customer's queued wire via GET /listWires or /previewWire;
and auto-verify another customer's expired prenote via GET
/listLinkedAccounts or /listLinkedAch.

Owner GET /resumeLinkedAccount and /confirmLinkedAccount remain registered
for GET and still mutate.
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


class RemainingClaimedAdminStaffMoneyTests(unittest.TestCase):
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
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(**bene_kw)
            )
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
        return sent.get_json()["wire"], bene_id

    def _queue_wire(self, *, trace_id="queued-1", **bene_kw):
        self.now[0] = FRIDAY_AFTER_CUTOFF
        wire, bene_id = self._send_wire(trace_id=trace_id, **bene_kw)
        self.assertEqual(wire["status"], "queued")
        return wire, bene_id

    def _add_link(self, **overrides):
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

    def _add_and_verify_link(self, **overrides):
        link = self._add_link(**overrides)
        self._verify(link["link_id"])
        return link

    def _expire_prenote(self, link_id):
        stored = self.link.store.get_link(link_id)
        stored.expires_at = 0
        self.link.store.update_link(stored)
        self.assertEqual(self.link.store.get_link(link_id).status, "pending")

    def _assert_transmitted(self, wire, customers_cls):
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_admin_can_complete_another_customers_sent_wire(self):
        # complete_wire only checks EMPLOYEE_ROLES; GET still mutates.
        wire, _bene_id = self._send_wire(trace_id="admin-complete")
        self.assertEqual(wire["status"], "sent")
        self._session("mallory", "admin", emp_tier=3)
        completed = self.client.get("/completeWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(completed.status_code, 200)
        body = completed.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["omad"])
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "completed")

    def test_claimed_admin_can_recall_and_credit_another_customers_wire(self):
        wire, _bene_id = self._send_wire(
            trace_id="admin-recall", nickname="Ally", account_number="99887766"
        )
        self.assertEqual(wire["status"], "sent")
        self._session("mallory", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            recalled = self.client.get("/recallWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(recalled.status_code, 200)
        body = recalled.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "recalled")
        args, _kwargs = customers_cls.return_value.credit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_admin_can_release_another_customers_pending_wire(self):
        # Dual-control pending_release was originated by alice, so claimed
        # admin is a "different" releaser and transmits (debit + IMAD).
        wire, _bene_id = self._send_wire(amount="10000.00", trace_id="admin-release")
        self.assertEqual(wire["status"], "pending_release")
        self._session("mallory", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.get(
                "/releaseWire", json={"wire_id": wire["wire_id"]}
            )
        self.assertEqual(released.status_code, 200)
        body = released.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "10000.00")

    def test_claimed_admin_can_override_another_customers_ofac_hold(self):
        # override_ofac only checks EMPLOYEE_ROLES; GET still transmits.
        wire, _bene_id = self._send_wire(
            trace_id="admin-ofac", nickname="Blocked", legal_name="Blocked Person"
        )
        self.assertEqual(wire["status"], "held")
        self._session("mallory", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            overridden = self.client.get(
                "/overrideOfac", json={"wire_id": wire["wire_id"]}
            )
        self.assertEqual(overridden.status_code, 200)
        body = overridden.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "sent")
        self.assertTrue(body["imad"])
        self.assertEqual(body["ofac_hit"], 0)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_admin_can_waive_another_customers_wire_fee(self):
        wire, _bene_id = self._queue_wire(trace_id="admin-waive")
        self.assertEqual(wire["fee"], "25.00")
        self._session("mallory", "admin", emp_tier=3)
        waived = self.client.get("/waiveWireFee", json={"wire_id": wire["wire_id"]})
        self.assertEqual(waived.status_code, 200)
        body = waived.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["fee"], "0.00")
        self.assertEqual(body["fee_status"], "waived")
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.fee_status, "waived")
        self.assertEqual(stored.fee, "0.00")

    def test_claimed_admin_can_settle_another_customers_ach(self):
        link = self._add_and_verify_link(nickname="SettleMe", account_last4="5566")
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
                    "trace_id": "admin-settle-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()["movement"]["movement_id"]
        self._session("mallory", "admin", emp_tier=3)
        settled = self.client.get("/settleLinkedAch", json={"movement_id": movement_id})
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.get_json()["movement"]["userid"], "alice")
        self.assertEqual(settled.get_json()["movement"]["status"], "settled")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            returned = self.client.get(
                "/returnLinkedAch",
                json={"movement_id": movement_id, "reason": "unauthorized"},
            )
        self.assertEqual(returned.status_code, 409)
        self.assertEqual(returned.get_json()["error"], "already_settled")
        customers_cls.return_value.credit_request.assert_not_called()

    def test_claimed_admin_can_return_another_customers_ach(self):
        link = self._add_and_verify_link(nickname="ReturnMe", account_last4="6677")
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
                    "trace_id": "admin-return-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()["movement"]["movement_id"]
        self._session("mallory", "admin", emp_tier=3)
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

    def test_claimed_admin_list_wires_transmits_another_customers_queue(self):
        # Claimed admin is treated as staff; customer_id is not bound to a
        # real role. GET list still run_due-transmits that owner's queue.
        wire, _bene_id = self._queue_wire(trace_id="admin-list-due")
        self.now[0] = MONDAY_MORNING
        self._session("mallory", "admin", emp_tier=3)
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

    def test_claimed_admin_preview_wire_transmits_another_customers_queue(self):
        # handle_preview uses _owner_userid(customer_id) for EMPLOYEE_ROLES,
        # then snapshot run_due on that owner.
        wire, bene_id = self._queue_wire(trace_id="admin-preview-due")
        self.now[0] = MONDAY_MORNING
        self._session("mallory", "admin", emp_tier=3)
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

    def test_claimed_admin_list_links_auto_verifies_another_customers_prenote(self):
        # snapshot → expire_due auto-accepts expired prenotes for the
        # customer_id claimed-admin staff sessions can point at.
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="StolenPrenote", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._expire_prenote(link["link_id"])
        self._session("mallory", "admin", emp_tier=3)
        listed = self.client.get(
            "/listLinkedAccounts",
            json={"userid": "mallory", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        stored = self.link.store.get_link(link["link_id"])
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.userid, "alice")
        self.assertEqual(listed.get_json()["LinkedAccounts"]["links"][0]["status"], "verified")

    def test_claimed_admin_list_ach_auto_verifies_another_customers_prenote(self):
        # /listLinkedAch also snapshots the claimed staff customer_id.
        self.link.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="ListAchStolen", account_last4="3344")
        self._expire_prenote(link["link_id"])
        self._session("mallory", "admin", emp_tier=3)
        listed = self.client.get(
            "/listLinkedAch",
            json={"userid": "mallory", "customer_id": "alice"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_owner_get_resume_link_still_mutates(self):
        link = self._add_and_verify_link(nickname="OwnerResume", account_last4="1122")
        paused = self.client.get(
            "/pauseLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(paused.get_json()["link"]["status"], "paused")
        self._session("alice", "customer")
        resumed = self.client.get(
            "/resumeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["link"]["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_owner_get_confirm_link_still_mutates(self):
        link = self._add_link(nickname="OwnerConfirm", account_last4="2233")
        self.assertEqual(link["status"], "pending")
        self._session("alice", "customer")
        confirmed = self.client.get(
            "/confirmLinkedAccount",
            json={
                "userid": "alice",
                "link_id": link["link_id"],
                "amount1": "0.12",
                "amount2": "0.47",
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.get_json()["link"]["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")


if __name__ == "__main__":
    unittest.main()
