"""Real app.py wiring for Fedwire — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_wire_service(); session usertype is trusted as the wire actor role.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    set_service as set_wire_service,
)

ET = timezone(timedelta(hours=-4))
WEEKDAY_BEFORE_CUTOFF = datetime(2024, 6, 14, 16, 0, tzinfo=ET).timestamp()


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
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.policy.outbound_fee = Decimal("25.00")
        # Weekend / after-cutoff clocks queue wires and skip the debit; pin a
        # Friday before 17:00 ET so send/complete/recall stay deterministic.
        self.service.clock = lambda: WEEKDAY_BEFORE_CUTOFF
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

    def test_claimed_admin_session_can_wire_another_customer(self):
        # /login stores client-supplied usertype; wire routes trust it as staff.
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 10},
                "savings": "None",
                "credit": "None",
            }
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(
                    userid="alice",
                    customer_id="bob",
                    default_account="2001",
                    nickname="BobsChase",
                ),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["userid"], "bob")
        listed = self.client.post(
            "/listWires",
            json={"userid": "alice", "customer_id": "bob"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["Wires"]["beneficiaries"][0]["userid"], "bob")

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

    def test_get_add_and_send_still_mutate(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.get("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
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
        customers_cls.return_value.debit_request.assert_called()
        args, kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_wire_service(None)
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

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addWireBeneficiary",
            json={
                "userid": "teller",
                "nickname": "Chase",
                "legal_name": "Ada Lovelace",
                "aba": "021000021",
                "account_number": "77881234",
                "street": "1 Federal St",
                "city": "New York",
                "state": "NY",
                "postal": "10004",
                "default_account": "1001",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_claimed_admin_can_send_wire_for_another_customer(self):
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 500},
                "savings": "None",
                "credit": "None",
            }
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(
                    userid="alice",
                    customer_id="bob",
                    default_account="2001",
                    nickname="BobsChase",
                ),
            )
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "customer_id": "bob",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "admin-send-bob",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["userid"], "bob")
        args, _kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "2001")
        self.assertEqual(args[1], "40.00")

    def test_get_complete_and_recall_still_mutate(self):
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
                    "trace_id": "get-complete-1",
                },
            )
        wire_id = sent.get_json()["wire"]["wire_id"]
        self._session("teller", "tier2")
        completed = self.client.get("/completeWire", json={"wire_id": wire_id})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")

        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(nickname="Ally", account_number="99887766"),
            )
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "15.00",
                    "trace_id": "get-recall-1",
                },
            )
        other_id = sent.get_json()["wire"]["wire_id"]
        self._session("teller", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            recalled = self.client.get("/recallWire", json={"wire_id": other_id})
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()["wire"]["status"], "recalled")
        customers_cls.return_value.credit_request.assert_called()


if __name__ == "__main__":
    unittest.main()
