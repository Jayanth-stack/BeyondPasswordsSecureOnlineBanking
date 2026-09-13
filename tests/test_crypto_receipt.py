"""HMAC receipts are the only integrity check on transfer acknowledgements."""
import hashlib
import hmac
import json
import os
import unittest

import tests  # noqa: F401 - sets RECEIPT_SECRET before crypto_receipt import

from utility.crypto_receipt import current_timestamp, generate_nonce, generate_receipt


SECRET = os.environ["RECEIPT_SECRET"].encode()


def _expected_signature(data):
    message = json.dumps(data, sort_keys=True).encode("utf-8")
    return hmac.new(SECRET, message, hashlib.sha256).hexdigest()


class CryptoReceiptTests(unittest.TestCase):
    def _payload(self, **overrides):
        data = {
            "transaction_no": -1,
            "from_account": 10,
            "to_account": 20,
            "amount": 50.0,
            "timestamp": "08/09/2026 12:00:00",
            "nonce": "fixed-nonce",
        }
        data.update(overrides)
        return data

    def test_signature_covers_canonical_json(self):
        data = self._payload()
        receipt = generate_receipt(data)
        self.assertEqual(receipt["signature"], _expected_signature(data))
        self.assertEqual(receipt["amount"], 50.0)

    def test_key_order_does_not_change_signature(self):
        a = generate_receipt({"amount": 10, "to": 2, "from": 1})
        b = generate_receipt({"from": 1, "to": 2, "amount": 10})
        self.assertEqual(a["signature"], b["signature"])

    def test_amount_tamper_invalidates_signature(self):
        data = self._payload()
        receipt = generate_receipt(data)
        tampered = dict(receipt)
        tampered["amount"] = 5000.0
        unsigned = {k: v for k, v in tampered.items() if k != "signature"}
        self.assertNotEqual(tampered["signature"], _expected_signature(unsigned))

    def test_does_not_mutate_input(self):
        data = self._payload()
        generate_receipt(data)
        self.assertNotIn("signature", data)

    def test_nonce_is_unique(self):
        self.assertNotEqual(generate_nonce(), generate_nonce())

    def test_timestamp_is_utc_iso(self):
        stamp = current_timestamp()
        self.assertTrue(stamp.endswith("Z"))
        self.assertIn("T", stamp)


if __name__ == "__main__":
    unittest.main()
