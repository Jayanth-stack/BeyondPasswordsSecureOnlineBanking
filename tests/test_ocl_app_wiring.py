"""Real app.py wiring for outbound Check21 — isolated Flask tests miss this."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.ocl import MemoryOclStore, set_service


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


class OclAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.ocl_service
        self.service.store = MemoryOclStore()
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_ocls_without_drawer_or_fingerprint(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.credit_request.return_value = "Success"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addOclProfile", json={
                "userid": "alice",
                "nickname": "Payroll",
                "payee_name": "Ada Lovelace",
                "payor_aba": "026009593",
                "drawer_account": "77881234",
                "serial": "1001",
                "default_account": "1001",
            })
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("drawer_account", added.get_json()["profile"])
            sent = self.client.post("/sendOcl", json={
                "userid": "alice",
                "profile_id": added.get_json()["profile"]["profile_id"],
                "amount": "20.00",
                "trace_id": "app-1",
            })
            self.assertEqual(sent.status_code, 201)
            self.assertNotIn("ece", sent.get_json()["outbound"])
            self.assertNotIn("image_fingerprint", sent.get_json()["outbound"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        ocls = response.get_json()["Ocls"]
        self.assertEqual(ocls["profiles"][0]["nickname"], "Payroll")
        self.assertEqual(ocls["outbounds"][0]["status"], "submitted")
        self.assertNotIn("drawer_account", ocls["profiles"][0])
        self.assertNotIn("ece", ocls["outbounds"][0])
        self.assertIn("Wires", response.get_json())
        self.assertIn("LinkedAccounts", response.get_json())
