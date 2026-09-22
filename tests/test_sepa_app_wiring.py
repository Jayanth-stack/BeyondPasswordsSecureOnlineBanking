"""Real app.py wiring for SEPA — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_sepa_service(); session usertype is trusted as the actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.sepa import EurUsdBook, MemorySepaStore, Target2Calendar, set_service


DE_IBAN = 'DE89370400440532013000'


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


class SepaAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.sepa_service
        self.service.store = MemorySepaStore()
        self.service.fx = EurUsdBook(Decimal('1.080000'))
        self.service.calendar = Target2Calendar(cutoff_hour=16, tz_offset_hours=2)
        self.service.policy.min_amount = Decimal('1.00')
        self.service.policy.max_amount = Decimal('1000000.00')
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
            "iban": DE_IBAN,
            "bic": "DEUTDEFF",
            "city": "Berlin",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_sepa_without_iban(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addSepaCreditor", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("iban", added.get_json()["creditor"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        sepa = response.get_json()["Sepa"]
        self.assertEqual(sepa["creditors"][0]["nickname"], "Berlin")
        self.assertEqual(sepa["creditors"][0]["iban_masked"], "DE****3000")
        self.assertNotIn("iban", sepa["creditors"][0])
        self.assertTrue(sepa["enabled"])

    def test_get_customer_includes_sepa_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addSepaCreditor", json=self._add_payload())
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
        self.assertEqual(response.get_json()["Sepa"]["creditors"][0]["nickname"], "Berlin")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addSepaCreditor", json=self._add_payload())
            listed = self.client.post(
                "/listSepas",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        creditors = listed.get_json()["Sepa"]["creditors"]
        self.assertEqual(len(creditors), 1)
        self.assertEqual(creditors[0]["userid"], "alice")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addSepaCreditor", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Sepa"]["enabled"], False)
        self.assertEqual(dash.get_json()["Sepa"]["creditors"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addSepaCreditor",
            json={
                "userid": "teller",
                "nickname": "Berlin",
                "legal_name": "Ada Lovelace",
                "iban": DE_IBAN,
                "default_account": "1001",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")


if __name__ == "__main__":
    unittest.main()
