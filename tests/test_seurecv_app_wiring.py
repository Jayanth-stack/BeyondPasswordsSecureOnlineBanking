"""Real app.py wiring for inbound SEPA — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_seurecv_service(); session usertype is trusted as the SEPA actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.seurecv import MemorySeurecvStore, set_service


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


class SeurecvAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.seurecv_service
        self.service.store = MemorySeurecvStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.dual_control_threshold = Decimal("1000000.00")
        self.service.policy.receiver_bank = "COBADEFFXXX"
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_sepa_in_without_proxy(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post(
                "/addSepaRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Jose mobile",
                    "proxy_type": "phone",
                    "proxy": "+4915112345678",
                    "destination_account": "1001",
                },
            )
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("proxy", added.get_json()["key"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        inbound = response.get_json()["SepaIn"]
        self.assertEqual(inbound["keys"][0]["nickname"], "Jose mobile")
        self.assertNotIn("proxy", inbound["keys"][0])
        self.assertTrue(inbound["enabled"])
        self.assertIn("Wires", response.get_json())

    def test_get_customer_includes_sepa_in_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addSepaRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Jose mobile",
                    "proxy_type": "phone",
                    "proxy": "+4915112345678",
                    "destination_account": "1001",
                },
            )
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
        self.assertEqual(response.get_json()["SepaIn"]["keys"][0]["nickname"], "Jose mobile")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post(
                "/addSepaRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Jose mobile",
                    "proxy_type": "phone",
                    "proxy": "+4915112345678",
                    "destination_account": "1001",
                },
            )
            listed = self.client.post(
                "/listSepaIns",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        keys = listed.get_json()["SepaIn"]["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["userid"], "alice")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["SepaIn"]["enabled"], False)
        self.assertEqual(dash.get_json()["SepaIn"]["keys"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addSepaRecvKey",
            json={
                "userid": "teller",
                "nickname": "Ops",
                "proxy_type": "phone",
                "proxy": "+4915112345678",
                "destination_account": "1001",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")


if __name__ == "__main__":
    unittest.main()
