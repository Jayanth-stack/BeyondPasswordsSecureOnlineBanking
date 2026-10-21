"""Real app.py wiring for inbound SPEI — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_srecv_service(); session usertype is trusted as the SPEI actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.srecv import MemorySrecvStore, compose_clabe, compose_rfc, set_service

CLABE = compose_clabe('646', '180', '1')
RFC = compose_rfc('GODE561231GR')


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


class SrecvAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.srecv_service
        self.service.store = MemorySrecvStore()
        self.service.policy.min_amount = Decimal("0.01")
        self.service.policy.dual_control_threshold = Decimal("200000.00")
        self.service.policy.receiver_bank = "646"
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_load_customer_includes_spei_in_without_clabe(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post(
                "/addSpeiRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Ada CLABE",
                    "rail": "spei",
                    "clabe": CLABE,
                    "rfc": RFC,
                    "destination_account": "1001",
                },
            )
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("clabe", added.get_json()["key"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        spei = response.get_json()["SpeiIn"]
        self.assertEqual(spei["keys"][0]["nickname"], "Ada CLABE")
        self.assertNotIn("clabe", spei["keys"][0])
        self.assertTrue(spei["enabled"])
        self.assertIn("Wires", response.get_json())

    def test_get_customer_includes_spei_in_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addSpeiRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Ada CLABE",
                    "rail": "spei",
                    "clabe": CLABE,
                    "rfc": RFC,
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
        self.assertEqual(response.get_json()["SpeiIn"]["keys"][0]["nickname"], "Ada CLABE")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post(
                "/addSpeiRecvKey",
                json={
                    "userid": "alice",
                    "nickname": "Ada CLABE",
                    "rail": "spei",
                    "clabe": CLABE,
                    "rfc": RFC,
                    "destination_account": "1001",
                },
            )
            listed = self.client.post(
                "/listSpeiIns",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        keys = listed.get_json()["SpeiIn"]["keys"]
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
        self.assertEqual(dash.get_json()["SpeiIn"]["enabled"], False)
        self.assertEqual(dash.get_json()["SpeiIn"]["keys"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addSpeiRecvKey",
            json={
                "userid": "teller",
                "nickname": "Ops",
                "rail": "spei",
                "clabe": CLABE,
                "rfc": RFC,
                "destination_account": "1001",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")


if __name__ == "__main__":
    unittest.main()
