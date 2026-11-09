"""Real app.py wiring for Fedwire — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_wire_service(); session usertype is trusted as the wire actor role.
Clock is pinned to Friday 2024-06-14 11:00 ET so cutoff/weekend cannot flake.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.wire import MemoryWireStore, WireCalendar, get_service, set_service

ET = timezone(timedelta(hours=-4))
PINNED = datetime(2024, 6, 14, 11, 0, tzinfo=ET).timestamp()


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(userid="alice"):
    if userid == "bob":
        return {
            "checkin": {"Account": 2001, "Balance": 2000},
            "savings": {"Account": 2002, "Balance": 10},
            "credit": "None",
        }
    return {
        "checkin": {"Account": 1001, "Balance": 5000},
        "savings": {"Account": 1002, "Balance": 80},
        "credit": "None",
    }


class WireAppWiringTests(unittest.TestCase):
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
        self.service.clock = lambda: PINNED
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        set_service(self.service)

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

    def test_load_customer_includes_wires_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account_number", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]
        self.assertEqual(wires["beneficiaries"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", wires["beneficiaries"][0])
        self.assertTrue(wires["enabled"])

    def test_get_customer_includes_wires_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Wires"]["beneficiaries"][0]["nickname"], "Chase")

    def test_get_send_wire_still_originates(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.get(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "get-wire-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        body = sent.get_json()["wire"]
        self.assertEqual(body["status"], "sent")
        self.assertTrue(body["imad"].startswith("20240614KONOHA01"))
        self.assertNotIn("account_number", body)
        customers_cls.return_value.debit_request.assert_called()
        args, kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_claimed_admin_session_can_wire_another_customer(self):
        # /login stores client-supplied usertype; wire routes trust it as staff.
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = (
                lambda userid: _accounts(userid)
            )
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(
                    userid="alice",
                    customer_id="bob",
                    nickname="BobsChase",
                    default_account="2001",
                    account_number="99887766",
                ),
            )
            self.assertEqual(added.status_code, 201)
            self.assertEqual(added.get_json()["beneficiary"]["userid"], "bob")
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "customer_id": "bob",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "40.00",
                    "trace_id": "admin-bob-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["userid"], "bob")
        self.assertEqual(sent.get_json()["wire"]["internal_account"], "2001")

    def test_accounts_helper_failure_allows_any_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(default_account="9999"),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["default_account"], "9999")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Wires"]["enabled"], False)
        self.assertEqual(dash.get_json()["Wires"]["beneficiaries"], [])
        self.assertIsNone(get_service())

    def test_claimed_employee_get_complete_and_recall_another_customer(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "idor-complete",
                },
            )
            other = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "15.00",
                    "trace_id": "idor-recall",
                },
            )
        wire_id = sent.get_json()["wire"]["wire_id"]
        other_id = other.get_json()["wire"]["wire_id"]
        self._session("alice", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.credit_request.return_value = "Success"
            completed = self.client.get(
                "/completeWire",
                json={"userid": "alice", "wire_id": wire_id},
            )
            recalled = self.client.get(
                "/recallWire",
                json={"userid": "alice", "wire_id": other_id},
            )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")
        self.assertTrue(completed.get_json()["wire"]["omad"])
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()["wire"]["status"], "recalled")
        customers_cls.return_value.credit_request.assert_called()

    def test_dual_control_same_approver_then_other_employee_releases(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
        self._session("maker", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            pending = self.client.post(
                "/sendWire",
                json={
                    "userid": "maker",
                    "customer_id": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "10000.00",
                    "trace_id": "hv-1",
                },
            )
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()["wire"]["status"], "pending_release")
        wire_id = pending.get_json()["wire"]["wire_id"]
        same = self.client.post(
            "/releaseWire",
            json={"userid": "maker", "wire_id": wire_id},
        )
        self.assertEqual(same.status_code, 403)
        self.assertEqual(same.get_json()["error"], "same_approver")
        self._session("checker", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.post(
                "/releaseWire",
                json={"userid": "checker", "wire_id": wire_id},
            )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        self.assertEqual(released.get_json()["wire"]["releaser"], "checker")


if __name__ == "__main__":
    unittest.main()
