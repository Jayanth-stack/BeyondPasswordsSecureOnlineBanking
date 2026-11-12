"""Real app.py wiring for UK FPS — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_ukfps_service(); session usertype is trusted as the UK FPS actor role.
"""
import importlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('UKFPS_STORE', 'memory')
os.environ.setdefault('UKFPS_FX_GBPUSD', '1.27')

import tests  # noqa: F401

from utility.ukfps import MemoryUkfpsStore, set_service


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


class UkfpsAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.ukfps_service
        self.service.store = MemoryUkfpsStore()
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Ada Mobile",
            "legal_name": "Ada Lovelace",
            "default_account": "1001",
            "proxy_type": "phone",
            "proxy": "07911123456",
            "bank_code": "089999",
            "account_number": "66374958",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_ukfps_without_proxy(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addUkFpsBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("proxy", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        book = response.get_json()["UkFps"]
        self.assertEqual(book["beneficiaries"][0]["nickname"], "Ada Mobile")
        self.assertNotIn("proxy", book["beneficiaries"][0])
        self.assertNotIn("fingerprint", book["beneficiaries"][0])
        self.assertTrue(book["enabled"])

    def test_get_customer_includes_ukfps_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addUkFpsBeneficiary", json=self._add_payload())
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
        self.assertEqual(response.get_json()["UkFps"]["beneficiaries"][0]["nickname"], "Ada Mobile")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addUkFpsBeneficiary", json=self._add_payload())
            listed = self.client.post(
                "/listUkFps",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        benes = listed.get_json()["UkFps"]["beneficiaries"]
        self.assertEqual(len(benes), 1)
        self.assertEqual(benes[0]["userid"], "alice")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addUkFpsBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["UkFps"]["enabled"], False)
        self.assertEqual(dash.get_json()["UkFps"]["beneficiaries"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addUkFpsBeneficiary",
            json=self._add_payload(userid="teller"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_get_send_still_mutates_and_debits_usd(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get("/addUkFpsBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendUkFps",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "get-send-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        customers_cls.return_value.debit_request.assert_called()
        args, kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "50.80")
        self.assertIn("ukfps to Ada Mobile", kwargs.get("remark") or args[2])


if __name__ == "__main__":
    unittest.main()
