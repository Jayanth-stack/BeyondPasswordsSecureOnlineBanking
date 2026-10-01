"""Leftover claimed-employee OFAC/fee/ACH settle, preview origination, and PII approve.

Prior coverage locked claimed `employee` complete/recall/release/return and
owner GET /previewWire snapshot origination. Sibling staff routes still trust
session `usertype` as a real teller: claimed `employee` can override another
customer's OFAC hold (debit+IMAD), waive another customer's wire fee, settle
another customer's ACH so it cannot be returned, originate another customer's
queued wire via GET /previewWire + customer_id, and approve PII updates by
claiming `employee` with emp_tier >= 2.
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


class ClaimedEmployeeOfacFeeSettleTests(unittest.TestCase):
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
        return sent.get_json()["wire"], bene_id

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

    def test_claimed_employee_can_override_another_customers_ofac_hold(self):
        wire, _bene_id = self._send_wire(
            trace_id="emp-ofac", nickname="Blocked", legal_name="Blocked Person"
        )
        self.assertEqual(wire["status"], "held")
        self._session("bob", "employee")
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

    def test_claimed_employee_can_waive_another_customers_wire_fee(self):
        self.service.clock = lambda: FRIDAY_AFTER_CUTOFF
        wire, _bene_id = self._send_wire(trace_id="emp-waive")
        self.assertEqual(wire["status"], "queued")
        self.assertEqual(wire["fee"], "25.00")
        self._session("bob", "employee")
        waived = self.client.get("/waiveWireFee", json={"wire_id": wire["wire_id"]})
        self.assertEqual(waived.status_code, 200)
        body = waived.get_json()["wire"]
        self.assertEqual(body["userid"], "alice")
        self.assertEqual(body["fee"], "0.00")
        self.assertEqual(body["fee_status"], "waived")
        stored = self.service.store.get_wire(wire["wire_id"])
        self.assertEqual(stored.fee_status, "waived")
        self.assertEqual(stored.fee, "0.00")

    def test_claimed_employee_can_settle_another_customers_ach(self):
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
                    "trace_id": "emp-settle-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()["movement"]["movement_id"]
        self._session("bob", "employee")
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


class ClaimedEmployeePreviewOriginationTests(unittest.TestCase):
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

    def test_claimed_employee_preview_transmits_another_customers_queue(self):
        self._session()
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
                    "trace_id": "emp-preview-due",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["status"], "queued")
        wire_id = sent.get_json()["wire"]["wire_id"]
        self.now[0] = MONDAY_MORNING
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            previewed = self.client.get(
                "/previewWire",
                json={
                    "userid": "bob",
                    "customer_id": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "50.00",
                },
            )
        self.assertEqual(previewed.status_code, 200)
        self.assertEqual(previewed.get_json()["preview"]["amount"], "50.00")
        stored = self.service.store.get_wire(wire_id)
        self.assertEqual(stored.status, "sent")
        self.assertTrue(stored.imad)
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")


class ClaimedEmployeeApproveUpdateInfoTests(unittest.TestCase):
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

    def test_claimed_employee_get_approve_update_info_writes_pii(self):
        # Route trusts session usertype/emp_tier; login-shaped claimed employee
        # with emp_tier>=2 is enough to invoke the PII write helper.
        self._session("bob", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.get(
                "/approveUpdateInfo",
                json={"userid": "bob", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with("bob", 9)


if __name__ == "__main__":
    unittest.main()
