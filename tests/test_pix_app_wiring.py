"""Real app.py wiring for PIX — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_pix_service(); session usertype is trusted as the PIX actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.pix import MemoryPixStore, set_service


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


class PixAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.pix_service
        self.service.store = MemoryPixStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.day_max = Decimal("20000.00")
        self.service.policy.night_max = Decimal("1000.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
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
            "key_type": "cpf",
            "key_value": "52998224725",
            "ispb": "60701190",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_pix_without_raw_key(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addPixKey", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("key_value", added.get_json()["key"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        pix = response.get_json()["Pix"]
        self.assertEqual(pix["keys"][0]["nickname"], "Maria")
        self.assertEqual(pix["keys"][0]["key_masked"], "***.***.***-25")
        self.assertNotIn("key_value", pix["keys"][0])
        self.assertNotIn("key_fingerprint", pix["keys"][0])

    def test_get_customer_staff_snapshot_and_send(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post("/addPixKey", json=self._add_payload())
            key_id = added.get_json()["key"]["key_id"]
            sent = self.client.post("/sendPix", json={
                "userid": "alice",
                "key_id": key_id,
                "amount": "12.00",
                "trace_id": "wire-check",
            })
            self.assertEqual(sent.status_code, 201)
            self._session("teller", "tier1")
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post("/getCustomer", json={"userid": "teller", "customer_id": "alice"})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Pix"]["ytd_sent"], "12.00")
        self.assertTrue(lookup.get_json()["Pix"]["payments"][0]["end_to_end_id_masked"])

    def test_existing_money_routes_still_present(self):
        self._session()
        self.assertTrue(any(rule.rule == "/fundTransfer" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/withdrawAmount" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/sendWire" for rule in self.app.url_map.iter_rules()))
        self.assertTrue(any(rule.rule == "/sendPix" for rule in self.app.url_map.iter_rules()))


if __name__ == "__main__":
    unittest.main()
