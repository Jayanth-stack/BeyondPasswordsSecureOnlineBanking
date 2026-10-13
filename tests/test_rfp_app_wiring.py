"""Real app.py wiring for inbound RfP — isolated Flask tests miss this."""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.rfp import MemoryRfpStore, set_service


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


class RfpAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.rfp_service
        self.service.store = MemoryRfpStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.fednow_cap = Decimal("100000.00")
        set_service(self.service)
        self.debits = []
        self.service.debit_fn = lambda account, amount, remark: self.debits.append((account, amount, remark)) or "Amount Debited"
        self.service.credit_fn = lambda account, amount, remark: "Success"
        self.service.accounts_fn = lambda userid: _accounts(userid)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_rfp_without_secrets(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/enrollRfp", json={
                "userid": "alice",
                "nickname": "Home",
                "kind": "email",
                "alias": "ada@example.com",
                "destination_account": "1001",
            })
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("alias_value", added.get_json()["enrollment"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("Wires", payload)
        self.assertIn("LinkedAccounts", payload)
        rfp = payload["Rfp"]
        self.assertEqual(rfp["enrollments"][0]["nickname"], "Home")
        self.assertEqual(rfp["enrollments"][0]["alias_masked"], "a***@example.com")
        self.assertNotIn("alias_value", rfp["enrollments"][0])
        self.assertNotIn("destination_account", rfp["enrollments"][0])

    def test_get_customer_staff_lookup_includes_rfp(self):
        self._session("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            self.client.post("/enrollRfp", json={
                "userid": "alice",
                "nickname": "Home",
                "kind": "email",
                "alias": "ada@example.com",
                "destination_account": "1001",
            })
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post(
                "/getCustomer", json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Rfp"]["enrollments"][0]["nickname"], "Home")

    def test_customer_cannot_ingest_on_real_app(self):
        self._session()
        denied = self.client.post("/ingestRfp", json={
            "userid": "alice",
            "end_to_end_id": "RFP20240614000001",
            "amount": "25.00",
            "creditor_name": "Ada Lovelace",
            "creditor_aba": "021000021",
            "creditor_account": "77881234",
            "debtor_account": "1001",
            "rail": "fednow",
            "receiver_routing": "021000021",
        })
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
