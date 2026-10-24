"""Real app.py wiring for card holds — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_cardhold_service(); session usertype is trusted as the actor role.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.cardhold import MemoryCardHoldStore, set_service

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


class CardHoldAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.cardhold_service
        self.service.store = MemoryCardHoldStore()
        self.service.policy.max_keys = 8
        self.service.policy.max_holds = 240
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Visa Daily",
            "pan": VISA,
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_cardholds_without_pan(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addCardHoldKey", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("fingerprint", added.get_json()["key"])
            self.assertNotIn("pan", added.get_json()["key"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["CardHolds"]
        self.assertEqual(snapshot["keys"][0]["nickname"], "Visa Daily")
        self.assertNotIn("fingerprint", snapshot["keys"][0])
        self.assertTrue(snapshot["enabled"])
        self.assertIn("Wires", response.get_json())
        self.assertIn("LinkedAccounts", response.get_json())

    def test_get_customer_includes_cardholds_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addCardHoldKey", json=self._add_payload())
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
        self.assertEqual(response.get_json()["CardHolds"]["keys"][0]["nickname"], "Visa Daily")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addCardHoldKey", json=self._add_payload())
            listed = self.client.post(
                "/listCardHolds",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        keys = listed.get_json()["CardHolds"]["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["userid"], "alice")

    def test_claimed_admin_session_can_enroll_another_customer(self):
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 10},
                "savings": "None",
                "credit": "None",
            }
            added = self.client.post(
                "/addCardHoldKey",
                json=self._add_payload(
                    userid="alice",
                    customer_id="bob",
                    default_account="2001",
                    nickname="BobsVisa",
                ),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["key"]["userid"], "bob")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addCardHoldKey", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["CardHolds"]["enabled"], False)
        self.assertEqual(dash.get_json()["CardHolds"]["keys"], [])

    def test_staff_ingest_capture_writes_debit_remark(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addCardHoldKey", json=self._add_payload())
        self.assertEqual(added.status_code, 201)
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            ingested = self.client.post(
                "/ingestCardHold",
                json={
                    "userid": "teller",
                    "amount": "22.00",
                    "merchant": "CAFE",
                    "pan": VISA,
                    "stan": "000777",
                },
            )
            self.assertEqual(ingested.status_code, 201)
            hold_id = ingested.get_json()["hold"]["hold_id"]
            captured = self.client.post(
                "/captureCardHold",
                json={"userid": "teller", "hold_id": hold_id},
            )
        self.assertEqual(captured.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once()
        args, kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "22.00")
        self.assertIn("card CAFE", kwargs.get("remark") or args[2])


if __name__ == "__main__":
    unittest.main()
