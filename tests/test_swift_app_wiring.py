"""Real app.py wiring for SWIFT — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_swift_service(). Domestic /sendWire stays on the Fedwire module.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.swift import MemorySwiftStore, set_service


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


class SwiftAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.swift_service
        self.service.store = MemorySwiftStore()
        self.service.policy.min_amount = Decimal("1.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.fx.rates["EUR"] = Decimal("1.080000")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Berlin",
            "legal_name": "Ada Lovelace",
            "bic": "DEUTDEFF",
            "iban": "DE89370400440532013000",
            "street": "Taunusanlage 12",
            "city": "Frankfurt",
            "country": "DE",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_swift_without_iban(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addSwiftBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("iban", added.get_json()["beneficiary"])
            self.assertNotIn("account_number", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        swift = response.get_json()["Swift"]
        self.assertEqual(swift["beneficiaries"][0]["nickname"], "Berlin")
        self.assertEqual(swift["beneficiaries"][0]["iban_masked"], "DE****3000")
        self.assertIn("Wires", response.get_json())

    def test_get_customer_staff_snapshot_and_send(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addSwiftBeneficiary", json=self._add_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post("/sendSwift", json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "20.00",
                "currency": "EUR",
                "trace_id": "app-1",
            })
            self.assertEqual(sent.status_code, 201)
            self.assertEqual(sent.get_json()["wire"]["debit_usd"], "21.60")
            self._session("teller", "tier1")
            lookup = self.client.post("/getCustomer", json={"userid": "teller", "customer_id": "alice"})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Swift"]["ytd_sent"], "21.60")
        self.assertEqual(lookup.get_json()["Swift"]["wires"][0]["iban_masked"], "DE****3000")
