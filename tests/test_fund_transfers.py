import importlib
import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401 - installs mysql mocks before customer imports


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

    def test_cheque_transfer_without_transaction_no_does_not_crash(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = [
            [(1,)],  # receiver active
            [(1000.0, 1, 'checkin')],  # sender balance/active/type
        ]

        result = customer.fund_transfers(10, 20, 100.0)

        self.assertEqual(result, 'done')
        self.db.commit.assert_called()

    def test_missing_receiver_returns_error_instead_of_crashing(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = []

        result = customer.fund_transfers(10, 20, 100.0)

        self.assertEqual(result, "Receiver's Account doesn't exists")

    def test_debit_request_missing_account_returns_error(self):
        customer = self.Customers()
        self.cursor.fetchall.return_value = []

        result = customer.debit_request(999, 50.0)

        self.assertEqual(result, "Account doesn't exists")


if __name__ == '__main__':
    unittest.main()
