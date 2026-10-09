"""Real app.py wiring for Positive Pay — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_pospay_service(); session usertype is trusted as the actor role.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.pospay import MemoryPosPayStore, set_service
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 50},
        "savings": {"Account": 1002, "Balance": 10},
        "credit": "None",
    }


class PosPayAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.pospay_service
        self.service.store = MemoryPosPayStore()
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.clock = lambda: datetime(2024, 6, 14, 11, 0, tzinfo=ET).timestamp()
        self.service.calendar = WireCalendar(cutoff_hour=14, tz_offset_hours=-4)
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_pospay_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addPosPayIssue", json={
                "userid": "alice",
                "account": "1001",
                "serial": "21",
                "amount": "11.00",
                "payee": "Acme",
                "issue_date": "20240601",
            })
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account", added.get_json()["issue"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["PosPay"]
        self.assertEqual(snapshot["issues"][0]["serial"], "21")
        self.assertNotIn("account", snapshot["issues"][0])

    def test_get_customer_staff_snapshot_and_ingest(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            self.client.post("/addPosPayIssue", json={
                "userid": "alice",
                "account": "1001",
                "serial": "22",
                "amount": "5.00",
                "payee": "Acme",
                "issue_date": "20240601",
            })
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            ingested = self.client.post("/ingestPosPay", json={
                "userid": "teller",
                "customer_id": "alice",
                "serial": "22",
                "amount": "5.00",
                "payee": "Acme",
                "account": "1001",
                "trace_id": "wire-1",
            })
            self.assertEqual(ingested.status_code, 201)
            lookup = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["PosPay"]["items"][0]["status"], "paid")
        customers_cls.return_value.debit_request.assert_called()

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addPosPayIssue", json={
                "userid": "alice",
                "account": "1001",
                "serial": "23",
                "amount": "6.00",
                "payee": "Acme",
                "issue_date": "20240601",
            })
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["PosPay"]["enabled"], False)
        self.assertEqual(dash.get_json()["PosPay"]["issues"], [])

    def test_staff_missing_customer_id_on_enroll_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/enrollPosPay",
            json={"userid": "teller", "account": "1001"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")


if __name__ == "__main__":
    unittest.main()
