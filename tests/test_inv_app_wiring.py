"""Real app.py wiring for investigations — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_inv_service(); session usertype is trusted as the investigation actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.inv import MemoryInvStore, set_service


SAMPLE_UETR = 'A1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
SAMPLE_E2E = 'INV20240614000001'


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


class InvAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.inv_service
        self.service.store = MemoryInvStore()
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _enroll_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Payroll",
            "direction": "outbound",
            "rail": "fednow",
            "amount": "25.00",
            "account": "1001",
            "counterparty_name": "Ada Lovelace",
            "counterparty_aba": "021000021",
            "uetr": SAMPLE_UETR,
            "end_to_end_id": SAMPLE_E2E,
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_investigations_without_uetr(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/enrollInv", json=self._enroll_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("uetr", added.get_json()["enrollment"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["Investigations"]
        self.assertEqual(snapshot["enrollments"][0]["nickname"], "Payroll")
        self.assertNotIn(SAMPLE_UETR, str(snapshot))

    def test_get_customer_includes_investigations(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            self.client.post("/enrollInv", json=self._enroll_payload(nickname="Chase"))
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer", json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Investigations"]["enrollments"][0]["nickname"], "Chase")

    def test_staff_missing_customer_id_on_enroll_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/enrollInv",
            json={
                "userid": "teller",
                "nickname": "Payroll",
                "amount": "25.00",
                "account": "1001",
                "counterparty_name": "Ada Lovelace",
                "counterparty_aba": "021000021",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/enrollInv", json=self._enroll_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Investigations"]["enabled"], False)
        self.assertEqual(dash.get_json()["Investigations"]["enrollments"], [])

    def test_idor_list_uses_session_owner_not_claimed_customer(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/enrollInv", json=self._enroll_payload())
        self._session("bob", "customer")
        listed = self.client.post("/listInvs", json={"userid": "bob", "customer_id": "alice"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["Investigations"]["enrollments"], [])
