"""Real app.py wiring for Zengin — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_zengin_service(); session usertype is trusted as the Zengin actor role.
"""
import importlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('ZENGIN_STORE', 'memory')
os.environ.setdefault('ZENGIN_FX_JPYUSD', '0.0067')

import tests  # noqa: F401

from utility.zengin import MemoryZenginStore, set_service


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


class ZenginAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.zengin_service
        self.service.store = MemoryZenginStore()
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Yamada Mobile",
            "legal_name": "Yamada Taro",
            "kana_name": "ヤマダタロウ",
            "default_account": "1001",
            "proxy_type": "phone",
            "proxy": "09011122222",
            "bank_code": "0001",
            "branch_code": "001",
            "account_number": "1234567",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_zengin_without_account_or_kana(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addZenginBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("proxy", added.get_json()["beneficiary"])
            self.assertNotIn("kana_name", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        book = response.get_json()["Zengin"]
        self.assertEqual(book["beneficiaries"][0]["nickname"], "Yamada Mobile")
        self.assertNotIn("proxy", book["beneficiaries"][0])
        self.assertNotIn("fingerprint", book["beneficiaries"][0])
        self.assertNotIn("account_number", book["beneficiaries"][0])
        self.assertTrue(book["enabled"])

    def test_get_customer_includes_zengin_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addZenginBeneficiary", json=self._add_payload())
        self.assertEqual(added.status_code, 201)
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Zengin"]["beneficiaries"][0]["nickname"], "Yamada Mobile")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addZenginBeneficiary", json=self._add_payload())
            listed = self.client.post(
                "/listZengin",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        benes = listed.get_json()["Zengin"]["beneficiaries"]
        self.assertEqual(len(benes), 1)
        self.assertEqual(benes[0]["userid"], "alice")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addZenginBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Zengin"]["enabled"], False)
        self.assertEqual(dash.get_json()["Zengin"]["beneficiaries"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addZenginBeneficiary",
            json=self._add_payload(userid="teller"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_get_send_still_mutates_and_debits_usd(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get("/addZenginBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendZengin",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "10000.00",
                    "trace_id": "get-send-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        customers_cls.return_value.debit_request.assert_called()
        args, kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "67.00")
        self.assertIn("zengin to Yamada Mobile", kwargs.get("remark") or args[2])


if __name__ == "__main__":
    unittest.main()
