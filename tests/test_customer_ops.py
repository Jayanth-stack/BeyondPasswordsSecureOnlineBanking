"""Customer-side money-in, uniqueness, password reset, and account-open control flow."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class CreditRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        importlib.reload(customer_module)
        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None
        self.db.commit.side_effect = None

    def test_inactive_account(self):
        self.cursor.fetchall.return_value = [(0,)]
        self.assertEqual(
            self.Customers().credit_request(10, 50.0),
            "Account(to credit) not active",
        )
        self.db.commit.assert_not_called()

    def test_success_credits_balance(self):
        self.cursor.fetchall.return_value = [(1,)]
        self.assertEqual(self.Customers().credit_request(10, 25.0), "Success")
        executed = " ".join(str(call.args[0]) for call in self.cursor.execute.call_args_list)
        self.assertIn("balance=balance+", executed)
        self.assertIn("10", executed)
        self.db.commit.assert_called()

    def test_commit_failure_rolls_back(self):
        self.cursor.fetchall.return_value = [(1,)]
        self.db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Customers().credit_request(10, 25.0), "Try again later")
        self.db.rollback.assert_called()


class CreateCustomerUniquenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.db.commit.side_effect = None

    def _create(self, customer, **overrides):
        kwargs = dict(
            customer_id="cust1",
            last_name="L",
            middle_name="",
            first_name="F",
            contact_no="4155552671",
            email_id="a@b.com",
            password="pw",
            ssn="123456789",
            dob="2000-01-01",
        )
        kwargs.update(overrides)
        return customer.create_customer_id(**kwargs)

    def test_duplicate_userid(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=1):
            self.assertEqual(self._create(customer), "EmpID already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_contact(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=1
        ):
            self.assertEqual(self._create(customer), "Contact already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_ssn(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=0
        ), patch.object(customer, "check_existing_ssn", return_value=1):
            self.assertEqual(self._create(customer), "SSN already Exists")
        self.cursor.execute.assert_not_called()

    def test_duplicate_email(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=0
        ), patch.object(customer, "check_existing_ssn", return_value=0), patch.object(
            customer, "check_existing_email", return_value=1
        ):
            self.assertEqual(self._create(customer), "Email already Exists")
        self.cursor.execute.assert_not_called()

    def test_insert_hashes_password_and_ssn(self):
        from utility.encrypt import encrypt_ssn

        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=0
        ), patch.object(customer, "check_existing_ssn", return_value=0), patch.object(
            customer, "check_existing_email", return_value=0
        ):
            self.assertEqual(self._create(customer), 1)

        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("'pw'", sql)
        self.assertIn(encrypt_ssn("123456789"), sql)
        self.assertIn("$2", sql)
        self.db.commit.assert_called()

    def test_create_commit_failure_rolls_back(self):
        customer = self.Customers()
        self.db.commit.side_effect = RuntimeError("disk full")
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=0
        ), patch.object(customer, "check_existing_ssn", return_value=0), patch.object(
            customer, "check_existing_email", return_value=0
        ):
            self.assertEqual(self._create(customer), -1)
        self.db.rollback.assert_called()


class OpenAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()

    def test_duplicate_account_type(self):
        customer = self.Customers()
        with patch.object(customer, "check_account", return_value=1):
            result = customer.open_account("cust1", "savings")
        self.assertEqual(result, ("Customer already have ", "savings", "account"))
        self.cursor.execute.assert_not_called()

    def test_opens_with_bonus_credit(self):
        customer = self.Customers()
        with patch.object(customer, "check_account", return_value=0):
            self.assertEqual(customer.open_account("cust1", "savings"), "Done")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("250.0", sql)
        self.assertIn("savings", sql)
        self.db.commit.assert_called()


class FundRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.db.commit.side_effect = None

    def test_inserts_pending_transaction_for_account_owner(self):
        customer = self.Customers()
        with patch.object(
            customer, "get_customerID_from_account", return_value="cust1"
        ) as lookup:
            self.assertEqual(customer.fund_request(10, 20, 15.5), "Request Sent")
        lookup.assert_called_once_with(10)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("cust1", sql)
        self.assertIn("15.500000", sql)
        self.db.commit.assert_called()

    def test_commit_failure_rolls_back(self):
        customer = self.Customers()
        self.db.commit.side_effect = RuntimeError("disk full")
        with patch.object(customer, "get_customerID_from_account", return_value="cust1"):
            self.assertEqual(customer.fund_request(10, 20, 15.5), "Try Again later")
        self.db.rollback.assert_called()


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()

    def test_wrong_old_password(self):
        customer = self.Customers()
        with patch.object(customer, "verify_customer", return_value=0):
            self.assertEqual(
                customer.reset_password("cust1", "old", "new"),
                "Invalid UserID/Password",
            )
        self.cursor.execute.assert_not_called()

    def test_success_hashes_new_password(self):
        customer = self.Customers()
        with patch.object(customer, "verify_customer", return_value=1):
            self.assertEqual(customer.reset_password("cust1", "old", "new-secret"), "Password Updated")
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("new-secret", sql)
        self.assertIn("$2", sql)
        self.db.commit.assert_called()

    def test_force_reset_missing_user(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0):
            self.assertEqual(customer.reset_fpassword("ghost", "new"), "UserID doesn't exists")
        self.cursor.execute.assert_not_called()

    def test_force_reset_success(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=1):
            self.assertEqual(customer.reset_fpassword("cust1", "new"), "Password Updated")
        self.db.commit.assert_called()


class UpdateInfoRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None
        self.db.commit.side_effect = None

    def test_customer_request_goes_to_tier1(self):
        msg = self.Customers().update_info_reqest("Customer", "cust1", "a@b.com", "1", "x")
        self.assertEqual(msg, "Update Info Request Placed")
        sql = self.cursor.execute.call_args.args[0]
        # status=1, approver=1 (tier1) for customer-originated requests
        self.assertRegex(sql, r",\s*1,\s*1\)\s*;")
        self.db.commit.assert_called()

    def test_employee_request_escalates_to_admin(self):
        msg = self.Customers().update_info_reqest("Employee", "emp1", "a@b.com", "1", "x")
        self.assertEqual(msg, "Update Info Request Placed")
        sql = self.cursor.execute.call_args.args[0]
        self.assertRegex(sql, r",\s*1,\s*3\)\s*;")

    def test_pending_list_filters_open_customer_requests(self):
        rows = [("cust1", "1", "a@b.com", "x", 1, 1)]
        self.cursor.fetchall.return_value = rows
        self.assertEqual(self.Customers().update_info_reqest_list("cust1"), rows)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("userid = 'cust1'", sql)
        self.assertIn("status = 1", sql)
        self.assertIn("requester = 'Customer'", sql)

    def test_pending_list_empty(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().update_info_reqest_list("cust1"), "None")


class DenyFundsAndLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None

    def test_deny_marks_transaction_closed(self):
        self.cursor.rowcount = 1
        self.assertEqual(self.Customers().deny_funds_requested(42, "alice"), "Request Cancelled")
        sql, params = self.cursor.execute.call_args[0]
        self.assertIn("Request Denied", sql)
        self.assertIn("status=0", sql)
        self.assertIn("approver1_id", sql)
        self.assertEqual(params, (42, "alice"))
        self.db.commit.assert_called()

    def test_get_all_account_maps_types(self):
        self.cursor.fetchall.return_value = [
            (11, "checkin", 100.0),
            (12, "savings", 200.0),
        ]
        result = self.Customers().get_all_account("cust1")
        self.assertEqual(result["checkin"]["Account"], 11)
        self.assertEqual(result["savings"]["Balance"], 200.0)
        self.assertEqual(result["credit"], "None")

    def test_get_customer_details_missing(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_customer_details("ghost"), "None")

    def test_get_customerID_from_missing_account(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_customerID_from_account(99), -1)

    def test_verify_and_check_account(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().verify_account(99), 0)
        self.assertEqual(self.Customers().check_account("cust1", "savings"), 0)
        self.cursor.fetchall.return_value = [(12,)]
        self.assertEqual(self.Customers().verify_account(12), 1)
        self.assertEqual(self.Customers().check_account("cust1", "savings"), 1)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("cust1", sql)
        self.assertIn("savings", sql)

    def test_get_cashier_check_missing_and_found(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_cashier_check(9), "None")
        row = [(9, "cust1", 20, 10, 40.0, 1)]
        self.cursor.fetchall.return_value = row
        self.assertEqual(self.Customers().get_cashier_check(9), row)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("cheque_no=9", sql)

    def test_get_customer_details_maps_fields(self):
        self.cursor.fetchall.return_value = [
            ("F", "M", "L", "dob", "phone", "a@b.com", "addr", "ssn", 1, "hist")
        ]
        result = self.Customers().get_customer_details("cust1")
        self.assertEqual(result["first_name"], "F")
        self.assertEqual(result["email_id"], "a@b.com")
        self.assertEqual(result["active"], 1)

    def test_uniqueness_helpers(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().check_existing_contact("1"), 0)
        self.assertEqual(self.Customers().check_existing_email("a@b.com"), 0)
        self.assertEqual(self.Customers().check_existing_ssn("123456789"), 0)
        self.cursor.fetchall.return_value = [("row",)]
        self.assertEqual(self.Customers().check_existing_contact("1"), 1)
        self.assertEqual(self.Customers().check_existing_email("a@b.com"), 1)
        self.assertEqual(self.Customers().check_existing_ssn("123456789"), 1)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("ssn='123456789'", sql)

    def test_get_customer_contact_no(self):
        self.cursor.fetchall.return_value = [("4155552671",)]
        self.assertEqual(self.Customers().get_customer_contactNo("cust1"), "4155552671")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("contact_no", sql)
        self.assertIn("cust1", sql)


if __name__ == "__main__":
    unittest.main()
