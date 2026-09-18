"""Cheque deposit must reject reused/invalid instruments before moving money."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class ChequeDepositTests(unittest.TestCase):
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
        self.cursor.fetchall.return_value = []
        self.db.commit.side_effect = None

    def test_invalid_cheque(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().deposit_check("cust1", 99), "Invalid Cheque")

    def test_already_used_cheque(self):
        # to_account, from_account, amount, active
        self.cursor.fetchall.return_value = [(20, 10, 100.0, 0)]
        customer = self.Customers()
        with patch.object(customer, "fund_transfers") as transfer:
            self.assertEqual(customer.deposit_check("cust1", 5), "Check already used")
            transfer.assert_not_called()

    def test_success_marks_cheque_inactive(self):
        self.cursor.fetchall.return_value = [(20, 10, 75.0, 1)]
        customer = self.Customers()
        with patch.object(customer, "fund_transfers", return_value="done") as transfer:
            self.assertEqual(customer.deposit_check("cust1", 5), "Success")
            transfer.assert_called_once_with(10, 20, 75.0)

        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE Cheque SET active=0" in sql and "5" in sql for sql in executed))
        self.db.commit.assert_called()

    def test_failed_transfer_does_not_consume_cheque(self):
        self.cursor.fetchall.return_value = [(20, 10, 75.0, 1)]
        customer = self.Customers()
        with patch.object(customer, "fund_transfers", return_value="Insufficient Balance"):
            self.assertEqual(customer.deposit_check("cust1", 5), "Insufficient Balance")

        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("UPDATE Cheque SET active=0" in sql for sql in executed))

    def test_hmac_receipt_does_not_mark_cheque_used(self):
        # Production fund_transfers now returns a signed receipt dict, not "done".
        # deposit_check still only consumes the cheque on an exact "done" string.
        self.cursor.fetchall.return_value = [(20, 10, 75.0, 1)]
        customer = self.Customers()
        receipt = {
            "transaction_no": -1,
            "from_account": 10,
            "to_account": 20,
            "amount": 75.0,
            "signature": "deadbeef",
        }
        with patch.object(customer, "fund_transfers", return_value=receipt):
            self.assertEqual(customer.deposit_check("cust1", 5), receipt)

        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("UPDATE Cheque SET active=0" in sql for sql in executed))
        self.db.commit.assert_not_called()

    def test_commit_failure_after_done_does_not_report_success(self):
        self.cursor.fetchall.return_value = [(20, 10, 75.0, 1)]
        self.db.commit.side_effect = RuntimeError("disk full")
        customer = self.Customers()
        with patch.object(customer, "fund_transfers", return_value="done"):
            self.assertEqual(customer.deposit_check("cust1", 5), "fail")
        self.db.rollback.assert_called()


class MakeCashierCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()

    def test_missing_sender(self):
        customer = self.Customers()
        with patch.object(customer, "verify_account", side_effect=[0, 1]):
            self.assertEqual(
                customer.make_cashier_check("cust1", 20, 10, 50),
                "Sender's Account doesn't Exists",
            )

    def test_missing_receiver(self):
        customer = self.Customers()
        with patch.object(customer, "verify_account", side_effect=[1, 0]):
            self.assertEqual(
                customer.make_cashier_check("cust1", 20, 10, 50),
                "Receiver's Account doesn't Exists",
            )

    def test_success(self):
        customer = self.Customers()
        with patch.object(customer, "verify_account", return_value=1):
            self.assertEqual(customer.make_cashier_check("cust1", 20, 10, 50), "Success")
        self.db.commit.assert_called()


if __name__ == "__main__":
    unittest.main()
