"""Real app.py wiring for card-push OCT — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_oct_service(); session usertype is trusted as the actor role.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.oct import MemoryOctStore, set_service


VISA = '4111111111111111'


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


class OctAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.oct_service
        self.service.store = MemoryOctStore()
        self.service.policy.max_amount = __import__('decimal').Decimal('10000.00')
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Mom",
            "legal_name": "Ada Lovelace",
            "pan": VISA,
            "expiry": "1229",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_card_push_without_pan(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addCardPushBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("pan", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["CardPush"]
        self.assertEqual(snapshot["beneficiaries"][0]["nickname"], "Mom")
        self.assertNotIn("pan", snapshot["beneficiaries"][0])
        self.assertNotIn("fingerprint", snapshot["beneficiaries"][0])
        self.assertTrue(snapshot["enabled"])
        self.assertIn("Wires", response.get_json())

    def test_get_customer_includes_card_push_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addCardPushBeneficiary", json=self._add_payload())
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
        self.assertEqual(response.get_json()["CardPush"]["beneficiaries"][0]["nickname"], "Mom")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addCardPushBeneficiary", json=self._add_payload())
            listed = self.client.post(
                "/listCardPush",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        benes = listed.get_json()["CardPush"]["beneficiaries"]
        self.assertEqual(len(benes), 1)
        self.assertEqual(benes[0]["userid"], "alice")

    def test_send_debits_through_closed_over_customers(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addCardPushBeneficiary", json=self._add_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            sent = self.client.post(
                "/sendCardPush",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "20.00",
                    "trace_id": "wire-oct-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["payment"]["status"], "completed")
        self.assertGreaterEqual(customers_cls.return_value.debit_request.call_count, 1)
        args, kwargs = customers_cls.return_value.debit_request.call_args_list[0]
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "20.00")
        self.assertIn("card push Mom", kwargs.get("remark") or args[2])

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addCardPushBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["CardPush"]["enabled"], False)
        self.assertEqual(dash.get_json()["CardPush"]["beneficiaries"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addCardPushBeneficiary",
            json={
                "userid": "teller",
                "nickname": "Mom",
                "legal_name": "Ada Lovelace",
                "pan": VISA,
                "expiry": "1229",
                "default_account": "1001",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")


if __name__ == "__main__":
    unittest.main()
