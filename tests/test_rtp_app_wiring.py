"""Real app.py wiring for FedNow / TCH RTP — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_rtp_service(); session usertype is trusted as the instant-pay actor role.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.rtp import MemoryRtpStore, set_service


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


class RtpAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.rtp_service
        self.service.store = MemoryRtpStore()
        self.service.policy.min_amount = self.service.policy.min_amount
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "legal_name": "Ada Lovelace",
            "aba": "021000021",
            "account_number": "77881234",
            "street": "1 Federal St",
            "city": "New York",
            "state": "NY",
            "postal": "10004",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_rtp_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addRtpCounterparty", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account_number", added.get_json()["counterparty"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["Rtp"]
        self.assertEqual(snapshot["counterparties"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", snapshot["counterparties"][0])
        self.assertEqual(snapshot["clock"]["hours"], "24/7")

    def test_get_customer_staff_snapshot_and_send_does_not_touch_wire(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post("/addRtpCounterparty", json=self._add_payload(nickname="Ally"))
            self.assertEqual(added.status_code, 201)
            party_id = added.get_json()["counterparty"]["counterparty_id"]
            sent = self.client.post(
                "/sendRtp",
                json={"userid": "alice", "counterparty_id": party_id, "amount": "12.00", "trace_id": "w1"},
            )
            self.assertEqual(sent.status_code, 201)
            self.assertEqual(sent.get_json()["payment"]["status"], "completed")
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            lookup = self.client.post("/getCustomer", json={"userid": "teller", "customer_id": "alice"})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()["Rtp"]["ytd_sent"], "12.00")
        recalled = self.client.post(
            "/recallRtp",
            json={"userid": "teller", "payment_id": sent.get_json()["payment"]["payment_id"]},
        )
        self.assertEqual(recalled.status_code, 403)
        self.assertEqual(recalled.get_json()["error"], "scheme_irrevocable")


if __name__ == "__main__":
    unittest.main()
