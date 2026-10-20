"""Real app.py wiring for SPEI — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_spei_service(); session usertype is trusted as the SPEI actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.spei import MemorySpeiStore, set_service


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


class SpeiAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.spei_service
        self.service.store = MemorySpeiStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.session_max = Decimal("1000000.00")
        self.service.policy.online_max = Decimal("8000.00")
        self.service.policy.codi_max = Decimal("8000.00")
        self.service.policy.dual_control_threshold = Decimal("200000.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Maria",
            "legal_name": "Maria Silva",
            "rail": "spei",
            "clabe": "012180000000000015",
            "rfc": "GODE561231GR8",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_spei_without_raw_clabe(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addSpeiBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("clabe", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        spei = response.get_json()["Spei"]
        self.assertEqual(spei["beneficiaries"][0]["nickname"], "Maria")
        self.assertEqual(spei["beneficiaries"][0]["clabe_masked"], "**************0015")
        self.assertNotIn("clabe", spei["beneficiaries"][0])
        self.assertNotIn("fingerprint", spei["beneficiaries"][0])

    def test_get_customer_staff_snapshot_and_send(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post("/addSpeiBeneficiary", json=self._add_payload())
            beneficiary_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post("/sendSpei", json={
                "userid": "alice",
                "beneficiary_id": beneficiary_id,
                "amount": "12.00",
                "trace_id": "wire-check",
            })
            self.assertEqual(sent.status_code, 201)
            self._session("teller", "tier1")
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post("/getCustomer", json={"userid": "teller", "customer_id": "alice"})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Spei"]["ytd_sent"], "12.00")
        self.assertTrue(lookup.get_json()["Spei"]["payments"][0]["tracking_key_masked"])

    def test_existing_money_routes_still_present(self):
        self._session()
        self.assertTrue(any(rule.rule == "/fundTransfer" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/withdrawAmount" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/sendWire" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/sendSpei" for rule in self.app.url_map.iter_rules()))


if __name__ == "__main__":
    unittest.main()
