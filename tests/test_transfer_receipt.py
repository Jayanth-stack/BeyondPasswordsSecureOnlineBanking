import unittest

from utility.crypto_receipt import generate_receipt, is_successful_transfer


class TransferReceiptTests(unittest.TestCase):
    def test_legacy_done_string_is_success(self):
        self.assertTrue(is_successful_transfer("done"))

    def test_signed_receipt_dict_is_success(self):
        receipt = generate_receipt(
            {
                "transaction_no": 1,
                "from_account": 10,
                "to_account": 20,
                "amount": 50.0,
                "timestamp": "01/01/2025 12:00:00",
                "nonce": "abc",
            }
        )
        self.assertTrue(is_successful_transfer(receipt))

    def test_error_string_is_not_success(self):
        self.assertFalse(is_successful_transfer("Insufficient Balance"))

    def test_unsigned_dict_is_not_success(self):
        self.assertFalse(is_successful_transfer({"transaction_no": 1}))


if __name__ == "__main__":
    unittest.main()
