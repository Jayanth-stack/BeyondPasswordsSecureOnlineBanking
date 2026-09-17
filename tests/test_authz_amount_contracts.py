"""Remaining high-risk contracts: staff-gate holes, non-canonical login, inf amounts."""
import importlib
import math
import os
import unittest
from unittest.mock import patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


_MODIFY_EMPLOYEE = {
    "userid": "emp1",
    "emp_id": "emp9",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "999-00-0000",
    "dob": "d",
    "address": "x",
    "tier": 3,
}


class StaffGateHoleTests(unittest.TestCase):
    """Several money/PII routes check session userid but not staff usertype."""

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

    def test_customer_session_can_execute_approve_request_emp(self):
        # Gate is userid match only; fetched employee tier is unused.
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_employee_tier.return_value = "None"
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 8)
        emp.get_employee_tier.assert_called_once_with("cust1")

    def test_modify_employee_matching_userid_can_rewrite_another_emp_id(self):
        # Deny path is session userid != JSON userid AND emp_tier < 3.
        # Matching userid skips the check even when emp_id is someone else.
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.post("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp9",
            "L",
            "",
            "F",
            "1",
            "a@b.com",
            "999-00-0000",
            "d",
            "x",
            3,
        )

    def test_reset_password_ignores_session_userid(self):
        self._session("attacker", "customer")
        with patch("app.Customers") as customers_cls, patch("app.twilio_client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            customers_cls.return_value.reset_password.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post(
                "/resetPassword",
                json={
                    "userid": "cust1",
                    "newPassword": "n3w",
                    "otp": "123456",
                    "requester": "Customer",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.reset_password.assert_called_once_with("cust1", "n3w")

    def test_dashboard_html_is_served_without_session(self):
        for path in ("/", "/customer_dash", "/admin", "/tier1", "/tier2", "/otp_page"):
            with self.subTest(path=path):
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.data)

    def test_register_customer_empty_empid_is_treated_as_staff_create(self):
        # Only the literal string 'None' takes the self-register redirect.
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.post(
                "/registerCustomer",
                json={
                    "empid": "",
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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Done")
        self.assertFalse(response.location)

    def test_login_noncanonical_usertype_crashes_after_customer_auth(self):
        from utility.encrypt import encrypt

        with patch(
            "app.Customers.retrieve_hashed_password", return_value=encrypt("pw")
        ), patch(
            "app.Customers.retrieve_phone_number", return_value="+14155552671"
        ), patch("app.client"):
            with self.assertRaises(AttributeError):
                self.client.post(
                    "/login",
                    json={"userid": "cust1", "password": "pw", "usertype": "hr"},
                )

    def test_login_capitalized_customer_usertype_crashes(self):
        from utility.encrypt import encrypt

        with patch(
            "app.Customers.retrieve_hashed_password", return_value=encrypt("pw")
        ), patch(
            "app.Customers.retrieve_phone_number", return_value="+14155552671"
        ), patch("app.client"):
            with self.assertRaises(AttributeError):
                self.client.post(
                    "/login",
                    json={"userid": "cust1", "password": "pw", "usertype": "Customer"},
                )

    def test_login_uses_env_verify_sid_on_placeholder_client(self):
        self.assertEqual(self.app_module.account_sid, "your Account_sid")
        self.assertEqual(self.app_module.verify_sid, os.getenv("TWILIO_VERIFY_SID"))
        self.assertNotEqual(self.app_module.verify_sid, "your verify_sid")

    def test_secret_key_is_random_bytes_not_configured(self):
        self.assertIsInstance(self.app.secret_key, (bytes, bytearray))
        self.assertEqual(len(self.app.secret_key), 24)

    def test_fund_transfer_forwards_infinity_amount(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": "Infinity",
                },
            )
        self.assertEqual(response.status_code, 200)
        amount = emp_cls.return_value.add_transaction.call_args.args[2]
        self.assertTrue(math.isinf(amount))

    def test_fund_transfer_scientific_notation_is_coerced(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": "1e4",
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 10000.0)

    def test_withdraw_scientific_notation_is_coerced(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.post(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 10, "amount": "1e3"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, "1e3")


class AmountAndLookupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.cust_cursor = customer_module.cursor
        cls.cust_db = customer_module.db
        cls.emp_cursor = employee_module.cursor
        cls.emp_db = employee_module.db

    def setUp(self):
        for mock in (self.cust_cursor, self.cust_db, self.emp_cursor, self.emp_db):
            mock.reset_mock()
        self.cust_cursor.fetchall.side_effect = None
        self.cust_cursor.fetchall.return_value = []
        self.emp_cursor.fetchall.side_effect = None
        self.emp_cursor.fetchall.return_value = []
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def tearDown(self):
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def test_add_transaction_infinity_overflows_integer_format(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            with self.assertRaises(OverflowError):
                emp.add_transaction(10, 20, float("inf"))
        self.emp_cursor.execute.assert_not_called()

    def test_add_transaction_nan_valueerrors_integer_format(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            with self.assertRaises(ValueError):
                emp.add_transaction(10, 20, float("nan"))

    def test_add_transaction_deposit_infinity_overflows(self):
        with self.assertRaises(OverflowError):
            self.Employee().add_transaction_deposit(10, float("inf"))

    def test_verify_and_check_account_omit_active(self):
        self.cust_cursor.fetchall.return_value = [(12, "savings", 0)]
        self.assertEqual(self.Customers().verify_account(12), 1)
        verify_sql = self.cust_cursor.execute.call_args.args[0]
        self.assertNotIn("active", verify_sql)

        self.assertEqual(self.Customers().check_account("cust1", "savings"), 1)
        check_sql = self.cust_cursor.execute.call_args.args[0]
        self.assertNotIn("active", check_sql)
        self.assertIn("cust1", check_sql)

    def test_get_all_account_omits_active_filter(self):
        self.cust_cursor.fetchall.return_value = [(12, "savings", 0.0)]
        result = self.Customers().get_all_account("cust1")
        self.assertEqual(result["savings"]["Account"], 12)
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertNotIn("active", sql)

    def test_open_account_interpolates_account_type(self):
        customer = self.Customers()
        injected = "savings'); DROP TABLE Accounts;--"
        with patch.object(customer, "check_account", return_value=0):
            self.assertEqual(customer.open_account("cust1", injected), "Done")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn(injected, sql)
        self.assertNotIn("%s", sql)

    def test_deny_funds_none_tier_typeerrors_instead_of_unauthorized(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value="None"):
            with self.assertRaises(TypeError):
                emp.deny_funds_requested("ghost", 9)

    def test_missing_employee_update_info_list_uses_tier1_queue(self):
        emp = self.Employee()
        self.emp_cursor.fetchall.return_value = [(1, "cust1")]
        with patch.object(emp, "get_employee_tier", return_value="None"):
            result = emp.update_info_request_list("ghost")
        self.assertEqual(result, [(1, "cust1")])
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("approver = 1", sql)

    def test_transfer_to_tier2_does_not_assign_looked_up_employee(self):
        emp = self.Employee()
        with patch.object(emp, "getTier2_emp", return_value="specific-t2"):
            self.assertEqual(
                emp.transfer_transaction_to_tier2(77),
                "Request Sent to Tier2 employee",
            )
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("approver2 = 2", sql)
        self.assertNotIn("specific-t2", sql)

    def test_create_employee_interpolates_last_name(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=0), patch.object(
            emp, "check_existing_email", return_value=0
        ):
            self.assertEqual(
                emp.create_employee(
                    "emp1",
                    "O'Brien",
                    "",
                    "F",
                    "4155552671",
                    "a@b.com",
                    "pw",
                    "123456789",
                    "2000-01-01",
                    1,
                ),
                1,
            )
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("O'Brien", sql)
        self.assertNotIn("%s", sql)

    def test_fund_request_does_not_verify_destination_account(self):
        customer = self.Customers()
        with patch.object(customer, "get_customerID_from_account", return_value="cust1"), patch.object(
            customer, "verify_account"
        ) as verify:
            self.assertEqual(customer.fund_request(10, 999, 15.5), "Request Sent")
            verify.assert_not_called()
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn("999", sql)


if __name__ == "__main__":
    unittest.main()
