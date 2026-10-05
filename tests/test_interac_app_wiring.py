"""Real app.py wiring for inbound Interac — isolated Flask tests miss this."""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.interac import MemoryInteracStore, set_service


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


class InteracAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.interac_service
        self.service.store = MemoryInteracStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.autodeposit_cap = Decimal("10000.00")
        set_service(self.service)
        self.credits = []
        self.service.credit_fn = lambda account, amount, remark: self.credits.append((account, amount, remark)) or "Success"
        self.service.debit_fn = lambda account, amount, remark: "Amount Debited"
        self.service.accounts_fn = lambda userid: _accounts(userid)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_interacs_without_alias_value(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addInteracAlias", json={
                "userid": "alice",
                "nickname": "Home",
                "kind": "email",
                "alias": "ada@example.com",
                "destination_account": "1001",
            })
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("value", added.get_json()["alias"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("Wires", payload)
        self.assertIn("LinkedAccounts", payload)
        interacs = payload["Interacs"]
        self.assertEqual(interacs["aliases"][0]["nickname"], "Home")
        self.assertEqual(interacs["aliases"][0]["alias_masked"], "a***@example.com")
        self.assertNotIn("value", interacs["aliases"][0])

    def test_get_customer_staff_lookup_includes_interacs(self):
        self._session("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            self.client.post("/addInteracAlias", json={
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
        self.assertEqual(lookup.get_json()["Interacs"]["aliases"][0]["nickname"], "Home")

    def test_customer_cannot_ingest_on_real_app(self):
        self._session()
        denied = self.client.post("/ingestInterac", json={
            "userid": "alice",
            "reference": "IET202406140001",
            "amount": "25.00",
            "sender_name": "Ada Lovelace",
            "alias_type": "email",
            "alias": "ada@example.com",
            "rail": "autodeposit",
            "receiver_routing": "000100016",
        })
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
