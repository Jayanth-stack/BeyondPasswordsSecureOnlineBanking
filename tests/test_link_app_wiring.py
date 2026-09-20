"""Real app.py wiring for linked accounts — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_link_service(); session usertype is trusted as the ACH actor role.
"""
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.link import MemoryLinkStore, set_service


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


class LinkAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.link_service
        self.service.store = MemoryLinkStore()
        self.service.amount_fn = lambda: (Decimal("0.12"), Decimal("0.47"))
        self.service.policy.challenge_secret = "unit-secret"
        self.service.policy.max_attempts = 3
        self.service.policy.min_amount = Decimal("1.00")
        self.service.policy.max_amount = Decimal("10000.00")
        set_service(self.service)

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _add_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "default_account": "1001",
            "routing_last4": "0210",
            "account_last4": "7788",
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_links_without_digest(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addLinkedAccount", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("challenge_digest", added.get_json()["link"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        linked = response.get_json()["LinkedAccounts"]
        self.assertEqual(linked["links"][0]["nickname"], "Chase")
        self.assertNotIn("challenge_digest", linked["links"][0])
        self.assertTrue(linked["enabled"])

    def test_get_customer_includes_links_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addLinkedAccount", json=self._add_payload())
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
        self.assertEqual(response.get_json()["LinkedAccounts"]["links"][0]["nickname"], "Chase")

    def test_customer_list_ignores_foreign_customer_id(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            self.client.post("/addLinkedAccount", json=self._add_payload())
            listed = self.client.post(
                "/listLinkedAccounts",
                json={"userid": "alice", "customer_id": "bob"},
            )
        self.assertEqual(listed.status_code, 200)
        links = listed.get_json()["LinkedAccounts"]["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["userid"], "alice")

    def test_claimed_admin_session_can_link_another_customer(self):
        # /login stores client-supplied usertype; ACH routes trust it as staff.
        self._session("alice", "admin")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 10},
                "savings": "None",
                "credit": "None",
            }
            added = self.client.post(
                "/addLinkedAccount",
                json=self._add_payload(
                    userid="alice",
                    customer_id="bob",
                    default_account="2001",
                    nickname="BobsChase",
                ),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["link"]["userid"], "bob")
        listed = self.client.post(
            "/listLinkedAccounts",
            json={"userid": "alice", "customer_id": "bob"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["LinkedAccounts"]["links"][0]["userid"], "bob")

    def test_accounts_helper_failure_allows_any_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            added = self.client.post(
                "/addLinkedAccount",
                json=self._add_payload(default_account="9999"),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["link"]["default_account"], "9999")

    def test_get_add_and_push_still_mutate(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get("/addLinkedAccount", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            link_id = added.get_json()["link"]["link_id"]
            confirmed = self.client.post(
                "/confirmLinkedAccount",
                json={
                    "userid": "alice",
                    "link_id": link_id,
                    "amount1": "0.12",
                    "amount2": "0.47",
                },
            )
            self.assertEqual(confirmed.status_code, 200)
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            pushed = self.client.get(
                "/pushToLinked",
                json={
                    "userid": "alice",
                    "link_id": link_id,
                    "amount": "40.00",
                    "trace_id": "get-push-1",
                },
            )
        self.assertEqual(pushed.status_code, 201)
        customers_cls.return_value.debit_request.assert_called_once()
        args, kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[0], "1001")
        self.assertEqual(args[1], "40.00")
        self.assertIn("ach to Chase", kwargs.get("remark") or args[2])

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addLinkedAccount", json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["LinkedAccounts"]["enabled"], False)
        self.assertEqual(dash.get_json()["LinkedAccounts"]["links"], [])

    def test_staff_missing_customer_id_on_add_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/addLinkedAccount",
            json={
                "userid": "teller",
                "nickname": "Chase",
                "default_account": "1001",
                "routing_last4": "0210",
                "account_last4": "7788",
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
            10, "40.00", remark="ach to Chase"
        )
        self.assertEqual(result, "Amount Debited")
        executed = " ".join(
            str(call.args[0]) for call in customer_module.cursor.execute.call_args_list
        )
        self.assertIn("ach to Chase", executed)
        customer_module.db.commit.side_effect = None


if __name__ == "__main__":
    unittest.main()
