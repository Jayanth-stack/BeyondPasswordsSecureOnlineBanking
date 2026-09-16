"""Employee tier gates on destructive ops and dual-control queues."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class DeactivateAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        importlib.reload(employee_module)
        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None

    def test_tier1_cannot_deactivate_account(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(
                emp.deactivate_account("emp1", 10),
                "Not authorized to dectivate accounts",
            )
        self.cursor.execute.assert_not_called()

    def test_tier2_rejects_missing_account(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch(
            "employee.Customers"
        ) as customers_cls:
            customers_cls.return_value.verify_account.return_value = 0
            self.assertEqual(emp.deactivate_account("emp1", 10), "account doesn't exists")
        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("DELETE FROM Accounts" in sql for sql in executed))

    def test_tier2_closes_existing_account(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            self.assertEqual(emp.deactivate_account("emp1", 10), "Account Closed")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("DELETE FROM Accounts", sql)
        self.db.commit.assert_called()

    def test_tier1_cannot_deactivate_customer(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(
                emp.deactivate_customer("emp1", "cust1"),
                "Not authorized to dectivate customer",
            )

    def test_tier2_rejects_missing_customer(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.check_user_id.return_value = 0
            # SQL interpolates customer_id with %d before the existence check.
            self.assertEqual(
                emp.deactivate_customer("emp1", 999),
                "customer doesn't exists",
            )

    def test_tier2_deletes_customer_and_accounts(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.check_user_id.return_value = 1
            self.assertEqual(emp.deactivate_customer("emp1", 42), "Customer deactivated")
        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM Accounts" in sql and "42" in sql for sql in executed))
        self.assertTrue(any("DELETE FROM Customers" in sql and "42" in sql for sql in executed))
        self.db.commit.assert_called()

    def test_non_admin_cannot_deactivate_employee(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=1), patch.object(
            emp, "get_employee_tier", return_value=2
        ):
            self.assertEqual(
                emp.deactivate_employee("emp1", "emp2"),
                "Not authorized to dectivate employee",
            )
        self.cursor.execute.assert_not_called()

    def test_missing_employee_not_deactivated(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0):
            self.assertEqual(
                emp.deactivate_employee("admin1", "ghost"),
                "Employee doesn't exists",
            )

    def test_admin_deactivates_employee(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=1), patch.object(
            emp, "get_employee_tier", return_value=3
        ), patch.object(emp, "getTier2_emp", return_value="t2"):
            self.assertEqual(emp.deactivate_employee("admin1", "emp2"), "Employee deactivated")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("active = 0", sql)
        self.assertIn("emp2", sql)
        self.db.commit.assert_called()


class UpdateInfoApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None

    def test_tier1_cannot_approve(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(
                emp.approve_update_info("emp1", 9),
                "Not authorized to update customer",
            )
        self.cursor.execute.assert_not_called()

    def test_invalid_request_id(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = []
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ):
            self.assertEqual(emp.approve_update_info("emp1", 9), "Invalid Update reqest ID")

    def test_customer_request_updates_customer_row(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = [
            ("Customer", "cust1", "555", "a@b.com", "addr")
        ]
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ):
            self.assertEqual(emp.approve_update_info("emp1", 9), "Customer Updated")
        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE Customers" in sql and "cust1" in sql for sql in executed))
        self.assertTrue(any("status = 0" in sql and "9" in sql for sql in executed))

    def test_non_tier3_cannot_update_employee_record(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(
                emp.update_employee("emp1", "emp2", "a@b.com", "A", "", "B", "1", "d", "x"),
                "Not authorized to update customer",
            )
        self.cursor.execute.assert_not_called()


class TransferQueueAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None

    def test_non_tier2_cannot_list_pending_transfers(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(emp.fund_transfer_requests("emp1"), "None")
        self.cursor.execute.assert_not_called()

    def test_tier2_lists_pending_for_approver2(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = [(1, 10, 20)]
        with patch.object(emp, "get_employee_tier", return_value=2):
            result = emp.fund_transfer_requests("emp1")
        self.assertEqual(result, [(1, 10, 20)])
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("approver2", sql)
        self.assertIn("status = 1", sql)

    def test_approve_skips_already_closed_transaction(self):
        emp = self.Employee()
        with patch("employee.emp") as global_emp, patch("employee.Customers") as customers_cls:
            global_emp.get_fromAccount_of_transaction.return_value = 10
            global_emp.get_toAccount_of_transaction.return_value = 20
            global_emp.get_amount_of_transaction.return_value = 50
            global_emp.get_transaction_status.return_value = 0
            self.assertEqual(emp.approve_fund_request(5), "Invalid transaction_no")
            customers_cls.return_value.fund_transfers.assert_not_called()

    def test_approve_executes_open_transaction(self):
        emp = self.Employee()
        with patch("employee.emp") as global_emp, patch("employee.Customers") as customers_cls:
            global_emp.get_fromAccount_of_transaction.return_value = 10
            global_emp.get_toAccount_of_transaction.return_value = 20
            global_emp.get_amount_of_transaction.return_value = 50
            global_emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            result = emp.approve_fund_request(5)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 5)
        self.assertEqual(result["amount"], 50)

    def test_missing_transaction_fields_are_invalid(self):
        emp = self.Employee()
        with patch("employee.emp") as global_emp, patch("employee.Customers") as customers_cls:
            global_emp.get_fromAccount_of_transaction.return_value = -1
            global_emp.get_toAccount_of_transaction.return_value = 20
            global_emp.get_amount_of_transaction.return_value = 50
            global_emp.get_transaction_status.return_value = 1
            self.assertEqual(emp.approve_fund_request(5), "Invalid transaction_no")
            customers_cls.return_value.fund_transfers.assert_not_called()


class EmployeePasswordAndPhoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchone.side_effect = None
        self.cursor.fetchall.side_effect = None

    def test_reset_rejects_bad_password(self):
        emp = self.Employee()
        with patch.object(emp, "verify_employee", return_value=0):
            self.assertEqual(
                emp.reset_password("emp1", "old", "new"),
                "Invalid UserID/Password",
            )
        self.cursor.execute.assert_not_called()

    def test_reset_success(self):
        emp = self.Employee()
        with patch.object(emp, "verify_employee", return_value=1):
            self.assertEqual(emp.reset_password("emp1", "old", "new"), "Password Updated")
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("'new'", sql)
        self.assertIn("$2", sql)
        self.db.commit.assert_called()

    def test_retrieve_phone_returns_raw(self):
        self.cursor.fetchone.return_value = ("4155552671",)
        self.assertEqual(self.Employee().retrieve_phone_number("emp1"), "4155552671")

    def test_retrieve_phone_missing(self):
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.Employee().retrieve_phone_number("ghost"))


if __name__ == "__main__":
    unittest.main()
