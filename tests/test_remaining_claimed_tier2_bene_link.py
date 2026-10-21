"""Leftover claimed-tier2 beneficiary/ACH pause, push, close, and reject IDOR.

Prior coverage locked claimed `employee`/`admin`/`tier1` pause/archive/resume
and claimed `tier2` create/cancel/ACH IDOR. Sibling `tier2` is still just a
client-claimed session usertype in EMPLOYEE_ROLES: claimed `tier2` can pause,
archive, or resume another customer's wire beneficiary; reject another
customer's prenote; pause/resume/close another customer's link; debit
another customer's account via GET /pushToLinked; and reject another
customer's queued wire via GET /rejectWire.
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


class RemainingClaimedTier2BeneLinkTests(unittest.TestCase):
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

    def _add_beneficiary(self, **overrides):
        self._session("alice", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(**overrides)
            )
        self.assertEqual(added.status_code, 201)
        return added.get_json()["beneficiary"]

    def _queue_wire(self, *, trace_id="queued-1", **bene_kw):
        self.now[0] = FRIDAY_AFTER_CUTOFF
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
                    "amount": "40.00",
                    "trace_id": trace_id,
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        return sent.get_json()["wire"]

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

    def test_claimed_tier2_can_pause_another_customers_beneficiary(self):
        bene = self._add_beneficiary(nickname="PauseMe", account_number="11112222")
        self.assertEqual(bene["status"], "active")
        self._session("mallory", "tier2", emp_tier=2)
        paused = self.client.get(
            "/pauseWireBeneficiary", json={"beneficiary_id": bene["beneficiary_id"]}
        )
        self.assertEqual(paused.status_code, 200)
        body = paused.get_json()["beneficiary"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "paused")
        self.assertEqual(
            self.service.store.get_beneficiary(bene["beneficiary_id"]).status, "paused"
        )

    def test_claimed_tier2_can_archive_another_customers_beneficiary(self):
        bene = self._add_beneficiary(nickname="ArchiveMe", account_number="33334444")
        self._session("mallory", "tier2", emp_tier=2)
        archived = self.client.get(
            "/archiveWireBeneficiary", json={"beneficiary_id": bene["beneficiary_id"]}
        )
        self.assertEqual(archived.status_code, 200)
        body = archived.get_json()["beneficiary"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "archived")
        self.assertEqual(
            self.service.store.get_beneficiary(bene["beneficiary_id"]).status, "archived"
        )

    def test_claimed_tier2_can_resume_another_customers_paused_beneficiary(self):
        bene = self._add_beneficiary(nickname="ResumeMe", account_number="55556666")
        paused = self.client.get(
            "/pauseWireBeneficiary", json={"beneficiary_id": bene["beneficiary_id"]}
        )
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        self._session("mallory", "tier2", emp_tier=2)
        resumed = self.client.get(
            "/resumeWireBeneficiary", json={"beneficiary_id": bene["beneficiary_id"]}
        )
        self.assertEqual(resumed.status_code, 200)
        body = resumed.get_json()["beneficiary"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "active")
        self.assertEqual(
            self.service.store.get_beneficiary(bene["beneficiary_id"]).status, "active"
        )

    def test_claimed_tier2_can_reject_another_customers_prenote(self):
        link = self._add_link(method="prenote", nickname="RejectMe", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._session("mallory", "tier2", emp_tier=2)
        rejected = self.client.get("/rejectPrenote", json={"link_id": link["link_id"]})
        self.assertEqual(rejected.status_code, 200)
        body = rejected.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "rejected")

    def test_claimed_tier2_can_pause_another_customers_link(self):
        link = self._add_link(nickname="PauseMe", account_last4="3344")
        self._verify(link["link_id"])
        self._session("mallory", "tier2", emp_tier=2)
        paused = self.client.get(
            "/pauseLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(paused.status_code, 200)
        body = paused.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "paused")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "paused")

    def test_claimed_tier2_can_resume_another_customers_paused_link(self):
        link = self._add_link(nickname="ResumeMe", account_last4="4455")
        self._verify(link["link_id"])
        paused = self.client.get(
            "/pauseLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(paused.get_json()["link"]["status"], "paused")
        self._session("mallory", "tier2", emp_tier=2)
        resumed = self.client.get(
            "/resumeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(resumed.status_code, 200)
        body = resumed.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "verified")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "verified")

    def test_claimed_tier2_can_close_another_customers_link(self):
        link = self._add_link(nickname="CloseMe", account_last4="5566")
        self._verify(link["link_id"])
        self._session("mallory", "tier2", emp_tier=2)
        closed = self.client.get(
            "/closeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(closed.status_code, 200)
        body = closed.get_json()["link"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "closed")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "closed")

    def test_claimed_tier2_can_push_another_customers_linked_funds(self):
        link = self._add_link(nickname="PushMe", account_last4="6677")
        self._verify(link["link_id"])
        self._session("mallory", "tier2", emp_tier=2)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            pushed = self.client.get(
                "/pushToLinked",
                json={
                    "userid": "mallory",
                    "customer_id": "alice",
                    "link_id": link["link_id"],
                    "amount": "25.00",
                    "trace_id": "tier2-push-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        self.assertEqual(pushed.get_json()["movement"]["userid"], "alice")
        self.assertEqual(pushed.get_json()["movement"]["direction"], "push")
        args, _kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "25.00")

    def test_claimed_tier2_can_reject_another_customers_queued_wire(self):
        wire = self._queue_wire(trace_id="tier2-reject", nickname="RejectWire")
        self._session("mallory", "tier2", emp_tier=2)
        rejected = self.client.get("/rejectWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(rejected.status_code, 200)
        body = rejected.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "rejected")

    def test_owner_get_push_to_linked_still_mutates(self):
        link = self._add_link(nickname="OwnerPush", account_last4="7788")
        self._verify(link["link_id"])
        self._session("alice", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            pushed = self.client.get(
                "/pushToLinked",
                json={
                    "userid": "alice",
                    "link_id": link["link_id"],
                    "amount": "25.00",
                    "trace_id": "owner-push-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        self.assertEqual(pushed.get_json()["movement"]["userid"], "alice")
        self.assertEqual(pushed.get_json()["movement"]["direction"], "push")
        args, _kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "25.00")

    def test_owner_get_reject_prenote_is_staff_only(self):
        # GET is registered, but reject_prenote requires EMPLOYEE_ROLES.
        link = self._add_link(method="prenote", nickname="OwnerReject", account_last4="8899")
        self.assertEqual(link["status"], "pending")
        self._session("alice", "customer")
        rejected = self.client.get("/rejectPrenote", json={"link_id": link["link_id"]})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.get_json()["error"], "link_forbidden")
        self.assertEqual(self.link.store.get_link(link["link_id"]).status, "pending")

    def test_owner_get_reject_wire_is_staff_only(self):
        # GET is registered, but reject_wire requires EMPLOYEE_ROLES.
        wire = self._queue_wire(trace_id="owner-reject", nickname="OwnerReject")
        self._session("alice", "customer")
        rejected = self.client.get("/rejectWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.get_json()["error"], "wire_forbidden")
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "queued")


if __name__ == "__main__":
    unittest.main()
