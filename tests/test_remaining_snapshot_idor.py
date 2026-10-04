"""Leftover snapshot side-effects, GET mutations, and claimed-role IDOR.

Prior coverage locked explicit /runDueWires, /cancelWire, /forceVerify, and
money-route GETs. Listing/dashboard snapshots still transmit queued wires,
GET reject/pause/archive/confirm/settle still mutate, and claimed `admin`
sessions still act as staff on another customer's instruments.
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
FRIDAY_AFTER_CUTOFF = datetime(2024, 6, 14, 18, 0, tzinfo=ET).timestamp()
MONDAY_MORNING = datetime(2024, 6, 17, 10, 0, tzinfo=ET).timestamp()

_MODIFY_CUSTOMER = {
    "userid": "alice",
    "customer_id": "alice",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "999-00-0000",
    "dob": "d",
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


class SnapshotSideEffectAndStaffGetTests(unittest.TestCase):
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
        return sent.get_json()["wire"], bene_id

    def test_get_list_wires_transmits_queued_via_snapshot(self):
        wire, _bene_id = self._queue_wire(trace_id="list-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWires", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_get_load_customer_transmits_queued_via_snapshot(self):
        wire, _bene_id = self._queue_wire(trace_id="dash-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            dash = self.client.get("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_get_reject_queued_wire_still_mutates(self):
        wire, _bene_id = self._queue_wire(trace_id="get-reject")
        self._session("teller", "tier2")
        rejected = self.client.get("/rejectWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["wire"]["status"], "rejected")
        self.assertEqual(self.service.store.get_wire(wire["wire_id"]).status, "rejected")

    def test_claimed_admin_can_reject_another_customers_queued_wire(self):
        wire, _bene_id = self._queue_wire(trace_id="admin-reject")
        self._session("bob", "admin")
        rejected = self.client.get("/rejectWire", json={"wire_id": wire["wire_id"]})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["wire"]["status"], "rejected")
        self.assertEqual(rejected.get_json()["wire"]["userid"], "alice")

    def test_get_pause_and_archive_beneficiary_still_mutate(self):
        wire, bene_id = self._queue_wire(trace_id="get-archive")
        self.assertEqual(wire["status"], "queued")
        paused = self.client.get("/pauseWireBeneficiary", json={"beneficiary_id": bene_id})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        archived = self.client.get(
            "/archiveWireBeneficiary", json={"beneficiary_id": bene_id}
        )
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.get_json()["beneficiary"]["status"], "archived")
        stored = self.service.store.get_beneficiary(bene_id)
        self.assertEqual(stored.status, "archived")

    def test_claimed_admin_can_archive_another_customers_beneficiary(self):
        _wire, bene_id = self._queue_wire(trace_id="admin-archive")
        self._session("bob", "admin")
        archived = self.client.get(
            "/archiveWireBeneficiary", json={"beneficiary_id": bene_id}
        )
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.get_json()["beneficiary"]["userid"], "alice")
        self.assertEqual(archived.get_json()["beneficiary"]["status"], "archived")


class RemainingLinkSnapshotAndGetTests(unittest.TestCase):
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
        self.service.policy.prenote_auto_accept = False
        self.service.policy.prenote_wait_seconds = 2 * 86400
        set_link_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "default_account": "1001",
            "routing_last4": "0210",
            "account_last4": "7788",
        }
        body.update(overrides)
        return body

    def _add_link(self, **overrides):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addLinkedAccount", json=self._add_payload(**overrides))
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

    def test_get_confirm_and_close_still_mutate(self):
        link = self._add_link()
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
        closed = self.client.get(
            "/closeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.get_json()["link"]["status"], "closed")
        stored = self.service.store.get_link(link["link_id"])
        self.assertEqual(stored.status, "closed")

    def test_get_resend_challenge_still_rotates_digest(self):
        link = self._add_link(nickname="Ally", account_last4="9900")
        before = self.service.store.get_link(link["link_id"]).challenge_digest
        self.service.amount_fn = lambda: (Decimal("0.21"), Decimal("0.34"))
        resent = self.client.get(
            "/resendLinkedChallenge", json={"link_id": link["link_id"]}
        )
        self.assertEqual(resent.status_code, 200)
        stored = self.service.store.get_link(link["link_id"])
        self.assertNotEqual(before, stored.challenge_digest)
        self.assertEqual(stored.resends, 1)
        self.assertEqual(stored.status, "pending")

    def test_get_list_links_auto_verifies_expired_prenote(self):
        self.service.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="Prenote", account_last4="1122")
        self.assertEqual(link["status"], "pending")
        stored = self.service.store.get_link(link["link_id"])
        stored.expires_at = 0
        self.service.store.update_link(stored)
        listed = self.client.get("/listLinkedAccounts", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            self.service.store.get_link(link["link_id"]).status, "verified"
        )

    def test_get_accept_prenote_still_verifies(self):
        self.service.policy.prenote_wait_seconds = 0
        link = self._add_link(method="prenote", nickname="Wait", account_last4="3344")
        self._session("teller", "tier1")
        accepted = self.client.get("/acceptPrenote", json={"link_id": link["link_id"]})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()["link"]["status"], "verified")

    def test_get_settle_and_return_still_mutate(self):
        link = self._add_link(nickname="Settle", account_last4="5566")
        self._verify(link["link_id"])
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
                    "trace_id": "get-settle-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()["movement"]["movement_id"]
        self._session("teller", "tier2")
        settled = self.client.get("/settleLinkedAch", json={"movement_id": movement_id})
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.get_json()["movement"]["status"], "settled")

        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            pulled = self.client.post(
                "/pullFromLinked",
                json={
                    "userid": "alice",
                    "link_id": link["link_id"],
                    "amount": "15.00",
                    "trace_id": "get-return-1",
                },
            )
        self.assertEqual(pulled.status_code, 201)
        return_id = pulled.get_json()["movement"]["movement_id"]
        self._session("teller", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            returned = self.client.get(
                "/returnLinkedAch",
                json={"movement_id": return_id, "reason": "unauthorized"},
            )
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()["movement"]["status"], "returned")
        customers_cls.return_value.debit_request.assert_called()

    def test_claimed_admin_can_pull_and_close_another_customers_link(self):
        link = self._add_link(nickname="Stolen", account_last4="7788")
        self._verify(link["link_id"])
        self._session("bob", "admin")
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
                    "trace_id": "admin-pull-1",
                },
            )
        self.assertEqual(pulled.status_code, 201)
        self.assertEqual(pulled.get_json()["movement"]["userid"], "alice")
        args, _kwargs = customers_cls.return_value.credit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")
        closed = self.client.get(
            "/closeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.get_json()["link"]["userid"], "alice")
        self.assertEqual(closed.get_json()["link"]["status"], "closed")


class RemainingAppIdorAndCrashTests(unittest.TestCase):
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

    def test_claimed_employee_can_open_account_for_another_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "emp1",
                    "customer_id": "alice",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("alice", "savings")

    def test_claimed_employee_can_book_appointment_for_another_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Booked"
            response = self.client.get(
                "/makeAppointment",
                json={"customer_id": "alice", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with(
            "alice", "10:00"
        )

    def test_claimed_admin_can_rewrite_another_customers_pii(self):
        self._session("bob", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "Updated"
            response = self.client.post(
                "/modifyCustomer",
                json=_MODIFY_CUSTOMER,
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once()
        args = customers_cls.return_value.update_account_info.call_args[0]
        self.assertEqual(args[0], "alice")
        self.assertEqual(args[6], "999-00-0000")

    def test_get_verify_otp_null_json_attributeerrors(self):
        self._session("alice", "customer")
        with self.assertRaises(AttributeError):
            self.client.get(
                "/verify-otp",
                data="null",
                content_type="application/json",
            )


if __name__ == "__main__":
    unittest.main()
