"""Session/auth and money-route validation — the blast radius is every outbound transfer."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class AuthRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_login_requires_body(self):
        response = self.client.post("/login", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "No data found")

    def test_login_requires_fields(self):
        response = self.client.post("/login", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_login_rejects_unknown_user(self):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = None
            response = self.client.post(
                "/login",
                json={"userid": "ghost", "password": "x", "usertype": "customer"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["message"], "Invalid credentials")

    def test_login_rejects_wrong_password(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("right")
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "wrong", "usertype": "customer"},
            )
        self.assertEqual(response.status_code, 401)

    def test_register_customer_requires_fields(self):
        response = self.client.post("/registerCustomer", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_register_customer_rejects_duplicate_user(self):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.check_user_id.return_value = 1
            response = self.client.post(
                "/registerCustomer",
                json={
                    "empid": "None",
                    "userid": "cust1",
                    "password": "pw",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "AcountID exists")

    def test_verify_otp_requires_session(self):
        response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertEqual(response.status_code, 401)

    def test_verify_otp_requires_code(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        response = self.client.post("/verify-otp", json={})
        self.assertEqual(response.status_code, 400)

    def test_load_customer_requires_customer_session(self):
        response = self.client.post("/loadCustomer", json={})
        self.assertEqual(response.status_code, 401)

        with self.client.session_transaction() as sess:
            sess["userid"] = "emp1"
            sess["usertype"] = "tier1"
        response = self.client.post("/loadCustomer", json={})
        self.assertEqual(response.status_code, 401)

    def test_load_employee_rejects_customer(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        response = self.client.post("/loadEmployee", json={})
        self.assertEqual(response.status_code, 401)

    def test_transaction_history_rejects_employee(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "emp1"
            sess["usertype"] = "tier1"
        response = self.client.post(
            "/getTransactionHistory",
            json={"userid": "emp1", "account_no": 10},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "Insufficient permissions")

    def test_transaction_history_rejects_userid_mismatch(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        response = self.client.post(
            "/getTransactionHistory",
            json={"userid": "cust2", "account_no": 10},
        )
        self.assertEqual(response.status_code, 403)


class MoneyRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _login_customer(self, userid="cust1"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "customer"

    def test_fund_transfer_unauthenticated_redirects(self):
        response = self.client.post(
            "/fundTransfer",
            json={"userid": "cust1", "fromAccount": 1, "toAccount": 2, "amount": 10},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_fund_transfer_userid_mismatch(self):
        self._login_customer("cust1")
        response = self.client.post(
            "/fundTransfer",
            json={"userid": "cust2", "fromAccount": 1, "toAccount": 2, "amount": 10},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["message"], "User ID mismatch")

    def test_fund_transfer_rejects_negative_amount(self):
        self._login_customer()
        response = self.client.post(
            "/fundTransfer",
            json={"userid": "cust1", "fromAccount": 1, "toAccount": 2, "amount": -5},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Enter a valid amount")

    def test_fund_transfer_missing_fields(self):
        self._login_customer()
        response = self.client.post("/fundTransfer", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)

    def test_withdraw_userid_mismatch(self):
        self._login_customer()
        response = self.client.post(
            "/withdrawAmount",
            json={"userid": "other", "account": 1, "amount": 10},
        )
        self.assertEqual(response.status_code, 401)

    def test_withdraw_rejects_negative_amount(self):
        self._login_customer()
        response = self.client.post(
            "/withdrawAmount",
            json={"userid": "cust1", "account": 1, "amount": -1},
        )
        self.assertEqual(response.status_code, 400)

    def test_deposit_rejects_negative_amount(self):
        self._login_customer()
        response = self.client.post(
            "/depositAmount",
            json={"userid": "cust1", "account": 1, "amount": -1},
        )
        self.assertEqual(response.status_code, 400)

    def test_request_funds_rejects_negative_amount(self):
        self._login_customer()
        response = self.client.post(
            "/requestFunds",
            json={"userid": "cust1", "fromAccount": 1, "toAccount": 2, "amount": -10},
        )
        self.assertEqual(response.status_code, 400)

    def test_cashier_cheque_rejects_negative_amount(self):
        self._login_customer()
        response = self.client.post(
            "/getCashierCheque",
            json={"userid": "cust1", "to_account": 2, "from_account": 1, "amount": -1},
        )
        self.assertEqual(response.status_code, 400)

    def test_deposit_check_rejects_employee_session(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "emp1"
            sess["usertype"] = "tier1"
        response = self.client.post(
            "/depositCheck",
            json={"userid": "emp1", "cheque_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_deposit_check_userid_mismatch(self):
        self._login_customer()
        response = self.client.post(
            "/depositCheck",
            json={"userid": "cust2", "cheque_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_approve_request_without_customer_session_redirects(self):
        response = self.client.post(
            "/approveRequest",
            json={"customer_id": "cust1", "transaction_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_approve_request_session_mismatch_redirects(self):
        with self.client.session_transaction() as sess:
            sess["customer_id"] = "cust1"
        response = self.client.post(
            "/approveRequest",
            json={"customer_id": "cust2", "transaction_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_open_account_userid_mismatch(self):
        self._login_customer()
        response = self.client.post(
            "/openNewAccount",
            json={"userid": "cust2", "customer_id": "cust2", "account_type": "savings"},
        )
        self.assertEqual(response.status_code, 401)

    def test_fund_transfer_queues_when_authorized(self):
        self._login_customer()
        with patch("app.Employee") as employee_cls:
            employee_cls.return_value.add_transaction.return_value = (
                "Request to be approved by tier1 employee"
            )
            response = self.client.post(
                "/fundTransfer",
                json={"userid": "cust1", "fromAccount": "10", "toAccount": "20", "amount": "15.5"},
            )
        self.assertEqual(response.status_code, 200)
        employee_cls.return_value.add_transaction.assert_called_once_with(10, 20, 15.5)
        self.assertEqual(
            response.get_json()["message"],
            "Request to be approved by tier1 employee",
        )


if __name__ == "__main__":
    unittest.main()
