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

    def test_request_funds_userid_mismatch(self):
        self._login_customer()
        response = self.client.post(
            "/requestFunds",
            json={"userid": "cust2", "fromAccount": 1, "toAccount": 2, "amount": 10},
        )
        self.assertEqual(response.status_code, 401)

    def test_deposit_userid_mismatch(self):
        self._login_customer()
        response = self.client.post(
            "/depositAmount",
            json={"userid": "other", "account": 1, "amount": 10},
        )
        self.assertEqual(response.status_code, 401)


class PermissionRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid, usertype, emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_approve_request_emp_requires_body(self):
        response = self.client.post("/approveRequestEmp", json={})
        self.assertEqual(response.status_code, 400)

    def test_approve_request_emp_requires_fields(self):
        response = self.client.post("/approveRequestEmp", json={"userid": "emp1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_approve_request_emp_unauthenticated_redirects(self):
        response = self.client.post(
            "/approveRequestEmp",
            json={"userid": "emp1", "transaction_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_approve_request_emp_userid_mismatch_redirects(self):
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post(
            "/approveRequestEmp",
            json={"userid": "emp2", "transaction_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_approve_request_emp_invalid_transaction(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_employee_tier.return_value = 2
            emp.get_fromAccount_of_transaction.return_value = -1
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 1},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Invalid transaction_no")

    def test_deactivate_account_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/deactivateAccount",
            json={"userid": "cust1", "account_no": 10},
        )
        self.assertEqual(response.status_code, 403)

    def test_deactivate_account_userid_mismatch(self):
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post(
            "/deactivateAccount",
            json={"userid": "emp2", "account_no": 10},
        )
        self.assertEqual(response.status_code, 401)

    def test_deactivate_account_rejects_tier1(self):
        self._session("emp1", "tier1", emp_tier=1)
        response = self.client.post(
            "/deactivateAccount",
            json={"userid": "emp1", "account_no": 10},
        )
        self.assertEqual(response.status_code, 403)

    def test_deactivate_customer_rejects_non_tier2(self):
        self._session("emp1", "tier1", emp_tier=1)
        response = self.client.post(
            "/deactivateCustomer",
            json={"userid": "emp1", "customer_id": "cust1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_deactivate_employee_rejects_non_admin(self):
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post(
            "/deactivateEmployee",
            json={"userid": "emp1", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 403)

    def test_deactivate_employee_unauthenticated_redirects(self):
        response = self.client.post(
            "/deactivateEmployee",
            json={"userid": "admin1", "emp_id": "emp2"},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_get_system_logs_rejects_non_admin(self):
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post("/getSystemLogs", json={"userid": "emp1"})
        self.assertIn(response.status_code, (301, 302))

    def test_get_system_logs_userid_mismatch(self):
        self._session("admin1", "admin")
        response = self.client.post("/getSystemLogs", json={"userid": "admin2"})
        self.assertEqual(response.status_code, 401)

    def test_register_employee_requires_fields(self):
        response = self.client.post("/registerEmployee", json={"userid": "emp1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_register_employee_rejects_duplicate(self):
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.check_user_id.return_value = 1
            response = self.client.post(
                "/registerEmployee",
                json={
                    "userid": "emp1",
                    "password": "pw",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 1,
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "AccountID exists")

    def test_register_customer_rejects_duplicate_contact(self):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.check_user_id.return_value = 0
            customers_cls.return_value.check_existing_contact.return_value = 1
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
        self.assertEqual(response.get_json()["message"], "Contact already registered")

    def test_reset_password_requires_fields(self):
        response = self.client.post("/resetPassword", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)

    def test_reset_password_unknown_user(self):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_phone_number.return_value = None
            response = self.client.post(
                "/resetPassword",
                json={
                    "userid": "ghost",
                    "newPassword": "x",
                    "otp": "123456",
                    "requester": "Customer",
                },
            )
        self.assertEqual(response.status_code, 404)

    def test_send_otp_unknown_user(self):
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_phone_number.return_value = None
            response = self.client.post(
                "/sendOTP",
                json={"userid": "ghost", "requester": "Customer"},
            )
        self.assertEqual(response.status_code, 404)

    def test_update_info_userid_mismatch(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/updateInfo",
            json={
                "userid": "cust2",
                "email": "a@b.com",
                "contact_no": "1",
                "address": "x",
                "requester": "Customer",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_approve_update_info_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/approveUpdateInfo",
            json={"userid": "cust1", "update_req_no": 1},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "Insufficient permissions")

    def test_deny_update_info_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/denyUpdateInfo",
            json={"userid": "cust1", "update_req_no": 1},
        )
        self.assertEqual(response.status_code, 403)

    def test_get_customer_rejects_customer_session(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/getCustomer",
            json={"userid": "cust1", "customer_id": "cust2"},
        )
        self.assertEqual(response.status_code, 403)

    def test_get_employee_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/getEmployee",
            json={"userid": "cust1", "emp_id": "emp1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_modify_customer_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/modifyCustomer",
            json={
                "userid": "cust1",
                "customer_id": "cust1",
                "last_name": "L",
                "middle_name": "",
                "first_name": "F",
                "contact_no": "1",
                "email_id": "a@b.com",
                "ssn": "1",
                "dob": "d",
                "address": "x",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_login_employee_uses_employee_store(self):
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.retrieve_hashed_password.return_value = None
            response = self.client.post(
                "/login",
                json={"userid": "emp1", "password": "x", "usertype": "tier1"},
            )
        emp_cls.return_value.retrieve_hashed_password.assert_called_once_with("emp1")
        self.assertEqual(response.status_code, 401)

    def test_logout_requires_userid(self):
        response = self.client.post("/logout", json={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

