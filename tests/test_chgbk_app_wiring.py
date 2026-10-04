"""Real app.py wiring for chargeback representment."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.chgbk import MemoryChgbkStore, set_service


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


class ChgbkAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.chgbk_service
        self.service.store = MemoryChgbkStore()
        self.service.directory_fn = lambda account: "alice" if str(account) == "1001" else None
        self.service.debit_fn = lambda account, amount, remark=None: "Amount Debited"
        self.service.credit_fn = lambda account, amount, remark=None: "Amount Credited"
        self.service.accounts_fn = lambda userid: _accounts(userid)
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _payload(self, **overrides):
        body = {
            "userid": "teller",
            "network": "visa",
            "reason": "13.1",
            "amount": "25.00",
            "ica": "400000",
            "card_bin": "411111",
            "card_last4": "1111",
            "merchant_account": "1001",
            "cardholder": "Jane Cardholder",
            "merchant": "Acme Store",
            "chargeback_date": "2024-06-14",
            "sequence": 1,
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_chargebacks_without_account(self):
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customerID_from_account.return_value = "alice"
            ingested = self.client.post("/ingestChgbk", json=self._payload())
        self.assertEqual(ingested.status_code, 201)
        self.assertNotIn("merchant_account", ingested.get_json()["case"])
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["Chargebacks"]
        self.assertEqual(snapshot["cases"][0]["merchant"], "Acme Store")
        self.assertNotIn("merchant_account", snapshot["cases"][0])
        self.assertTrue(snapshot["enabled"])

    def test_get_customer_includes_chargebacks_for_staff(self):
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customerID_from_account.return_value = "alice"
            ingested = self.client.post("/ingestChgbk", json=self._payload())
        self.assertEqual(ingested.status_code, 201)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Chargebacks"]["cases"][0]["account_last4"], "1001")
