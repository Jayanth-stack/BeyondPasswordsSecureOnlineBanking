"""Fund movement edge cases: balances, credit overdraft, inactive accounts, receipts."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class FundTransfersTests(unittest.TestCase):
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
        self.db.rollback.side_effect = None

    def _regular_transfer_rows(self, sender_row):
        # 1) receiver active  2) deposit flag  3) sender balance/active/type
        return [[(1,)], [(0,)], [sender_row]]

    def test_success_returns_signed_receipt(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 1, "checkin"))

        result = customer.fund_transfers(10, 20, 100.0)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["from_account"], 10)
        self.assertEqual(result["to_account"], 20)
        self.assertEqual(result["amount"], 100.0)
        self.assertIn("signature", result)
        self.assertTrue(result["signature"])
        self.db.commit.assert_called()

    def test_deposit_flag_credits_receiver_only(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = [
            [(1,)],  # receiver active
            [(1,)],  # deposit=1
        ]

        result = customer.fund_transfers(10, 20, 25.0, transaction_no=9)

        self.assertIsInstance(result, dict)
        executed = " ".join(str(call.args[0]) for call in self.cursor.execute.call_args_list)
        self.assertIn("balance=balance +", executed)
        self.assertNotIn("balance=balance-", executed)

    def test_insufficient_balance(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((50.0, 1, "checkin"))

        self.assertEqual(
            customer.fund_transfers(10, 20, 100.0),
            "Insufficient Balance",
        )

    def test_credit_overdraft_limit(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((0.0, 1, "credit"))

        self.assertEqual(
            customer.fund_transfers(10, 20, 5001.0),
            "Insufficient Balance in Credit Card",
        )

    def test_credit_within_limit_succeeds(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((0.0, 1, "credit"))

        result = customer.fund_transfers(10, 20, 5000.0)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["amount"], 5000.0)

    def test_inactive_receiver(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(0,)]

        self.assertEqual(
            customer.fund_transfers(10, 20, 10.0),
            "Receiver's Account not active",
        )

    def test_inactive_sender(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 0, "savings"))

        self.assertEqual(
            customer.fund_transfers(10, 20, 10.0),
            "Sender's Account not active",
        )

    def test_coerces_string_ids_and_amount(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 1, "checkin"))

        result = customer.fund_transfers("10", "20", "12.5")
        self.assertEqual(result["from_account"], 10)
        self.assertEqual(result["to_account"], 20)
        self.assertEqual(result["amount"], 12.5)

    def test_approved_transaction_is_closed(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 1, "checkin"))

        customer.fund_transfers(10, 20, 10.0, transaction_no=77)
        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("status=0" in sql and "77" in sql for sql in executed))

    def test_commit_failure_rolls_back(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 1, "checkin"))
        self.db.commit.side_effect = [None, RuntimeError("disk full")]

        self.assertEqual(customer.fund_transfers(10, 20, 10.0), "Try Again later")
        self.db.rollback.assert_called()


class DebitRequestTests(unittest.TestCase):
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

    def test_insufficient_balance(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(20.0, 1, "checkin")]
        self.assertEqual(customer.debit_request(10, 50.0), "Insufficient Balance")

    def test_inactive_account(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(200.0, 0, "checkin")]
        self.assertEqual(customer.debit_request(10, 50.0), "Account not active")

    def test_credit_overdraft_limit(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(-4999.0, 1, "credit")]
        self.assertEqual(customer.debit_request(10, 2.0), "Insufficient Balance in Credit Card")

    def test_success(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(200.0, 1, "checkin")]
        self.assertEqual(customer.debit_request(10, 50.0), "Amount Debited")
        self.db.commit.assert_called()

    def test_credit_within_overdraft_succeeds(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = [(0.0, 1, "credit")]
        self.assertEqual(customer.debit_request(10, 5000.0), "Amount Debited")
        executed = " ".join(str(call.args[0]) for call in self.cursor.execute.call_args_list)
        self.assertIn("balance=balance-", executed)


class VerifyCustomerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        cls.Customers = customer_module.Customers

    def test_valid_password(self):
        from utility.encrypt import encrypt

        hashed = encrypt("secret")
        customer = self.Customers()
        with patch.object(customer, "retrieve_hashed_password", return_value=hashed):
            self.assertEqual(customer.verify_customer("cust1", "secret"), 1)

    def test_wrong_password(self):
        from utility.encrypt import encrypt

        hashed = encrypt("secret")
        customer = self.Customers()
        with patch.object(customer, "retrieve_hashed_password", return_value=hashed):
            self.assertEqual(customer.verify_customer("cust1", "nope"), 0)

    def test_missing_user(self):
        customer = self.Customers()
        with patch.object(customer, "retrieve_hashed_password", return_value=None):
            self.assertEqual(customer.verify_customer("ghost", "secret"), 0)


if __name__ == "__main__":
    unittest.main()
