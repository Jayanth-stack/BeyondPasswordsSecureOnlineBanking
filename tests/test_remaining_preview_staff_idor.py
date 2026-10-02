"""Leftover preview/list snapshot origination, GET pause/resume/push, claimed-role IDOR.

Prior coverage locked /listWires, /loadCustomer, /rejectWire, /pauseWireBeneficiary,
/archiveWireBeneficiary, /pullFromLinked, and /forceVerify. Sibling list/preview
routes still transmit queued wires via snapshot→run_due, GET resume/pause/reject/
push still mutate, claimed `employee` staff lookups originate another customer's
queue, and claimed `admin` can push/reject another customer's ACH.
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


class PreviewListSnapshotOriginationTests(unittest.TestCase):
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

    def test_get_list_wire_beneficiaries_transmits_queued_via_snapshot(self):
        # /listWireBeneficiaries shares handle_list_wires; snapshot still run_due.
        wire, _bene_id = self._queue_wire(trace_id="bene-list-due")
        self.now[0] = MONDAY_MORNING
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get("/listWireBeneficiaries", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_get_preview_wire_transmits_queued_via_snapshot(self):
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
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_employee_get_customer_transmits_another_customers_queue(self):
        # /getCustomer allows claimed employee/tier1 and _wire_snapshot run_due.
        wire, _bene_id = self._queue_wire(trace_id="staff-lookup-due")
        self.now[0] = MONDAY_MORNING
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {
                "first_name": "Ada"
            }
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            lookup = self.client.get(
                "/getCustomer",
                json={"userid": "bob", "customer_id": "alice"},
            )
        self.assertEqual(lookup.status_code, 200)
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.status, "sent")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_get_resume_beneficiary_still_mutates(self):
        _wire, bene_id = self._queue_wire(trace_id="get-resume")
        paused = self.client.get("/pauseWireBeneficiary", json={"beneficiary_id": bene_id})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        resumed = self.client.get(
            "/resumeWireBeneficiary", json={"beneficiary_id": bene_id}
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["beneficiary"]["status"], "active")
        self.assertEqual(self.service.store.get_beneficiary(bene_id).status, "active")

    def test_claimed_admin_can_resume_another_customers_paused_beneficiary(self):
        _wire, bene_id = self._queue_wire(trace_id="admin-resume")
        paused = self.client.get("/pauseWireBeneficiary", json={"beneficiary_id": bene_id})
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        self._session("bob", "admin")
        resumed = self.client.get(
            "/resumeWireBeneficiary", json={"beneficiary_id": bene_id}
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["beneficiary"]["userid"], "alice")
        self.assertEqual(resumed.get_json()["beneficiary"]["status"], "active")


class RemainingLinkPausePushRejectTests(unittest.TestCase):
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

    def test_get_pause_and_resume_linked_account_still_mutate(self):
        link = self._add_link(nickname="PauseMe", account_last4="8899")
        self._verify(link["link_id"])
        paused = self.client.get(
            "/pauseLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()["link"]["status"], "paused")
        resumed = self.client.get(
            "/resumeLinkedAccount", json={"link_id": link["link_id"]}
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["link"]["status"], "verified")
        self.assertEqual(
            self.service.store.get_link(link["link_id"]).status, "verified"
        )

    def test_get_reject_prenote_still_mutates(self):
        link = self._add_link(method="prenote", nickname="RejectMe", account_last4="2211")
        self.assertEqual(link["status"], "pending")
        self._session("teller", "tier1")
        rejected = self.client.get(
            "/rejectPrenote", json={"link_id": link["link_id"], "note": "mismatch"}
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["link"]["status"], "rejected")
        self.assertEqual(
            self.service.store.get_link(link["link_id"]).status, "rejected"
        )

    def test_claimed_admin_can_reject_another_customers_prenote(self):
        link = self._add_link(method="prenote", nickname="StolenPrenote", account_last4="3344")
        self._session("bob", "admin")
        rejected = self.client.get("/rejectPrenote", json={"link_id": link["link_id"]})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["link"]["userid"], "alice")
        self.assertEqual(rejected.get_json()["link"]["status"], "rejected")

    def test_get_push_to_linked_still_moves_money(self):
        link = self._add_link(nickname="PushMe", account_last4="5566")
        self._verify(link["link_id"])
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            pushed = self.client.get(
                "/pushToLinked",
                json={
                    "userid": "alice",
                    "link_id": link["link_id"],
                    "amount": "20.00",
                    "trace_id": "get-push-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        self.assertEqual(pushed.get_json()["movement"]["userid"], "alice")
        self.assertEqual(pushed.get_json()["movement"]["direction"], "push")
        args, _kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")

    def test_claimed_admin_can_push_another_customers_linked_funds(self):
        link = self._add_link(nickname="AdminPush", account_last4="6677")
        self._verify(link["link_id"])
        self._session("bob", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            pushed = self.client.get(
                "/pushToLinked",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "link_id": link["link_id"],
                    "amount": "25.00",
                    "trace_id": "admin-push-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        self.assertEqual(pushed.get_json()["movement"]["userid"], "alice")
        args, _kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "25.00")

    def test_get_list_linked_ach_auto_verifies_expired_prenote(self):
        self.service.policy.prenote_auto_accept = True
        link = self._add_link(method="prenote", nickname="ListAch", account_last4="7788")
        self.assertEqual(link["status"], "pending")
        stored = self.service.store.get_link(link["link_id"])
        stored.expires_at = 0
        self.service.store.update_link(stored)
        listed = self.client.get("/listLinkedAch", json={"userid": "alice"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            self.service.store.get_link(link["link_id"]).status, "verified"
        )


class RemainingStaffLookupIdorTests(unittest.TestCase):
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

    def test_claimed_employee_get_employee_reads_another_emp_pii(self):
        self._session("bob", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {
                "first_name": "Ada",
                "ssn": "999-00-0000",
                "tier": 3,
            }
            response = self.client.get(
                "/getEmployee",
                json={"userid": "bob", "emp_id": "admin1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["ssn"], "999-00-0000")
        emp_cls.return_value.get_employee_details.assert_called_once_with("admin1")

    def test_claimed_employee_get_appointment_list_for_another_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [
                {"time": "10:00"}
            ]
            response = self.client.get(
                "/getAppointmentList",
                json={"customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_appointment.assert_called_once_with("alice")


if __name__ == "__main__":
    unittest.main()
