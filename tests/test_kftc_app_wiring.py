"""Real app.py wiring for KFTC / Open Banking — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_kftc_service(); session usertype is trusted as the KFTC actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.kftc import MemoryKftcStore, set_service


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 5000},
        "savings": {"Account": 1002, "Balance": 10},
        "credit": "None",
    }


class KftcAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.kftc_service
        self.service.store = MemoryKftcStore()
        self.service.policy.krwusd = Decimal("0.000720")
        self.service.policy.outbound_fee = Decimal("0.00")
        self.service.policy.bokwire_fee = Decimal("3000.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Minsu Mobile",
            "legal_name": "Kim Minsu",
            "hangul_name": "김민수",
            "default_account": "1001",
            "bank_code": "004",
            "account_number": "12345678901",
            "proxy_type": "phone",
            "proxy": "01012345678",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_kftc_without_account_or_hangul(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addKftcBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account_number", added.get_json()["beneficiary"])
            self.assertNotIn("hangul_name", added.get_json()["beneficiary"])
            self.assertNotIn("fingerprint", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        kftc = response.get_json()["Kftc"]
        self.assertEqual(kftc["beneficiaries"][0]["nickname"], "Minsu Mobile")
        self.assertNotIn("account_number", kftc["beneficiaries"][0])
        self.assertNotIn("hangul_name", kftc["beneficiaries"][0])
        self.assertNotIn("김민수", str(kftc))
        self.assertNotIn("12345678901", str(kftc))
        self.assertTrue(kftc["enabled"])

    def test_get_customer_includes_kftc_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addKftcBeneficiary", json=self._add_payload())
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
        self.assertEqual(response.get_json()["Kftc"]["beneficiaries"][0]["nickname"], "Minsu Mobile")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addKftcBeneficiary", json=self._add_payload())
            listed = self.client.post(
                "/listKftc",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        benes = listed.get_json()["Kftc"]["beneficiaries"]
        self.assertEqual(len(benes), 1)
        self.assertEqual(benes[0]["userid"], "alice")

    def test_claimed_admin_session_can_add_another_customer(self):
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 10},
                "savings": "None",
                "credit": "None",
            }
            added = self.client.post(
                "/addKftcBeneficiary",
                json=self._add_payload(
                    userid="alice",
                    customer_id="bob",
                    default_account="2001",
                    nickname="BobsKB",
                ),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["userid"], "bob")
        listed = self.client.post(
            "/listKftc",
            json={"userid": "alice", "customer_id": "bob"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["Kftc"]["beneficiaries"][0]["userid"], "bob")

    def test_accounts_helper_failure_allows_any_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            added = self.client.post(
                "/addKftcBeneficiary",
                json=self._add_payload(default_account="9999"),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["default_account"], "9999")

    def test_get_add_and_send_still_mutate(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get("/addKftcBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendKftc",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "10000.00",
                    "trace_id": "get-send-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        customers_cls.return_value.debit_request.assert_called_once()
        args, kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "7.20")
        self.assertIn("kftc to Minsu Mobile", kwargs.get("remark") or args[2])

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addKftcBeneficiary", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Kftc"]["enabled"], False)
        self.assertEqual(dash.get_json()["Kftc"]["beneficiaries"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addKftcBeneficiary",
            json={
                "userid": "teller",
                "nickname": "Minsu",
                "legal_name": "Kim Minsu",
                "hangul_name": "김민수",
                "default_account": "1001",
                "bank_code": "004",
                "account_number": "12345678901",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_debit_remark_is_written_into_transaction_history(self):
        import customer as customer_module

        customer_module.cursor.reset_mock()
        customer_module.db.reset_mock()
        customer_module.cursor.fetchall.side_effect = None
        customer_module.db.commit.side_effect = None
        customer_module.cursor.fetchall.return_value = [(200.0, 1, "checkin")]
        result = customer_module.Customers().debit_request(
            10, "7.20", remark="kftc to Minsu Mobile"
        )
        self.assertEqual(result, "Amount Debited")
        executed = " ".join(
            str(call.args[0]) for call in customer_module.cursor.execute.call_args_list
        )
        self.assertIn("kftc to Minsu Mobile", executed)
        customer_module.db.commit.side_effect = None


if __name__ == "__main__":
    unittest.main()
