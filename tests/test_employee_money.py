"""Dual-control threshold and employee authorization around pending transfers."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class AddTransactionTests(unittest.TestCase):
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

    def _sql(self):
        return " ".join(str(call.args[0]) for call in self.cursor.execute.call_args_list)

    def test_missing_from_account(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.side_effect = [0, 1]
            self.assertEqual(emp.add_transaction(10, 20, 50), "account 10 doesn't exists")
        self.cursor.execute.assert_not_called()

    def test_missing_to_account(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.side_effect = [1, 0]
            self.assertEqual(emp.add_transaction(10, 20, 50), "account 20 doesn't exists")

    def test_amount_at_threshold_stays_tier1(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            msg = emp.add_transaction(10, 20, 1000)
        self.assertEqual(msg, "Request to be approved by tier1 employee")
        self.assertIn(",1,1000,", self._sql().replace(" ", ""))

    def test_amount_over_threshold_escalates_to_tier2(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            msg = emp.add_transaction(10, 20, 1000.01)
        self.assertEqual(msg, "Request to be approved by tier2 employee")

    def test_deposit_escalates_over_threshold(self):
        emp = self.Employee()
        msg = emp.add_transaction_deposit(10, 2500)
        self.assertEqual(msg, "Request to be approved by tier2 employee")
        compact = self._sql().replace(" ", "")
        self.assertIn("1,1)" , compact)

    def test_deposit_at_threshold_stays_tier1(self):
        emp = self.Employee()
        msg = emp.add_transaction_deposit(10, 1000)
        self.assertEqual(msg, "Request to be approved by tier1 employee")
        compact = self._sql().replace(" ", "")
        self.assertIn(",1,1000,1,1)", compact)

    def test_transfer_insert_is_pending_non_deposit(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            emp.add_transaction(10, 20, 50)
        compact = self._sql().replace(" ", "")
        self.assertIn(",1,0)", compact)
        self.assertIn("status", self._sql())


class EmployeeAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor

    def setUp(self):
        self.cursor.reset_mock()
        self.cursor.fetchall.side_effect = None

    def test_missing_employee_has_no_tier(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().get_employee_tier("ghost"), "None")

    def test_tier_lookup(self):
        self.cursor.fetchall.return_value = [(2,)]
        self.assertEqual(self.Employee().get_employee_tier("emp1"), 2)

    def test_tier1_cannot_deny_bank_transfers(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(
                emp.deny_funds_requested("emp1", 9),
                "Not authorized to GET/Approve transactions",
            )

    def test_missing_transaction_amount(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().get_amount_of_transaction(99), -1)

    def test_transaction_field_lookups(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = [(10,)]
        self.assertEqual(emp.get_fromAccount_of_transaction(5), 10)
        self.cursor.fetchall.return_value = [(20,)]
        self.assertEqual(emp.get_toAccount_of_transaction(5), 20)
        self.cursor.fetchall.return_value = [(1,)]
        self.assertEqual(emp.get_transaction_status(5), 1)

    def test_missing_transaction_accounts_are_invalid(self):
        emp = self.Employee()
        self.cursor.fetchall.return_value = []
        self.assertEqual(emp.get_fromAccount_of_transaction(99), -1)
        self.assertEqual(emp.get_toAccount_of_transaction(99), -1)
        self.assertEqual(emp.get_transaction_status(99), -1)

    def test_tier2_can_deny_bank_transfers(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(emp.deny_funds_requested("emp1", 9), "Request Cancelled")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("Request Denied by Bank", sql)
        self.assertIn("status=0", sql)
        self.assertIn("9", sql)

    def test_get_tier_employees(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().getTier1_emp(), "None")
        self.assertEqual(self.Employee().getTier2_emp(), "None")
        self.cursor.fetchall.return_value = [("empT2",)]
        self.assertEqual(self.Employee().getTier2_emp(), "empT2")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("tier = 2", sql)
        self.assertIn("active = 1", sql)


if __name__ == "__main__":
    unittest.main()
