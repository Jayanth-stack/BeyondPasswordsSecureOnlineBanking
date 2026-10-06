"""Real app.py wiring for outbound Interac — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_iet_service(); session usertype is trusted as the Interac actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.iet import MemoryIetStore, set_service


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


class IetAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.iet_service
        self.service.store = MemoryIetStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.autodeposit_cap = Decimal("10000.00")
        self.service.debit_fn = lambda account, amount, remark: "Amount Debited"
        self.service.credit_fn = lambda account, amount, remark: "Success"
        self.service.accounts_fn = lambda userid: _accounts(userid)
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Pat",
            "legal_name": "Pat Singh",
            "kind": "email",
            "alias": "pat@example.com",
            "rail": "autodeposit",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_iet_without_alias(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addIetContact", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("alias_value", added.get_json()["contact"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        iet = response.get_json()["Iet"]
        self.assertEqual(iet["contacts"][0]["nickname"], "Pat")
        self.assertEqual(iet["contacts"][0]["alias_masked"], "p***@example.com")
        self.assertNotIn("alias_value", iet["contacts"][0])

    def test_get_customer_staff_snapshot_and_send(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addIetContact", json=self._add_payload())
            contact_id = added.get_json()["contact"]["contact_id"]
            sent = self.client.post("/sendIet", json={
                "userid": "alice", "contact_id": contact_id, "amount": "10.00", "trace_id": "app-1",
            })
            self.assertEqual(sent.status_code, 201)
            self.assertEqual(sent.get_json()["transfer"]["status"], "completed")
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Iet"]["transfers"][0]["status"], "completed")
        self.assertIn("Wires", lookup.get_json())
