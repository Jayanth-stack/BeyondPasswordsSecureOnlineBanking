"""Real app.py wiring for CHIPS — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_chips_service(); session usertype is trusted as the CHIPS actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.chips import MemoryChipsStore, set_service


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 5000},
        "savings": {"Account": 1002, "Balance": 10},
        "credit": "None",
    }


class ChipsAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.chips_service
        self.service.store = MemoryChipsStore()
        self.service.policy.min_amount = Decimal("100.00")
        self.service.policy.max_amount = Decimal("100000000.00")
        self.service.policy.outbound_fee = Decimal("8.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Citi",
            "legal_name": "Ada Lovelace",
            "participant": "0002",
            "uid": "123456",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_chips_without_uid(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addChipsBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("uid", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("Chips", payload)
        self.assertIn("Wires", payload)
        chips = payload["Chips"]
        self.assertEqual(chips["beneficiaries"][0]["nickname"], "Citi")
        self.assertEqual(chips["beneficiaries"][0]["uid_masked"], "***456")
        self.assertNotIn("uid", chips["beneficiaries"][0])
        self.assertEqual(chips["settlement_method"], "chips_net")

    def test_get_customer_staff_snapshot(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addChipsBeneficiary", json=self._add_payload(nickname="Wells"))
            self.assertEqual(added.status_code, 201)
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post("/getCustomer", json={"userid": "teller", "customer_id": "alice"})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Chips"]["beneficiaries"][0]["nickname"], "Wells")
