"""Password and SSN hashing — wrong compare here silently breaks login and PII lookup."""
import unittest

from utility.encrypt import (
    check_encrypted_password,
    encrypt,
    encrypt_ssn,
    verify_password,
)


class EncryptSsnTests(unittest.TestCase):
    def test_same_ssn_is_deterministic(self):
        self.assertEqual(encrypt_ssn("123456789"), encrypt_ssn("123456789"))

    def test_different_ssn_differs(self):
        self.assertNotEqual(encrypt_ssn("123456789"), encrypt_ssn("987654321"))

    def test_is_sha256_hex(self):
        digest = encrypt_ssn("123456789")
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_salt_changes_value(self):
        # Production salts with a fixed suffix; raw SHA256 of the SSN must not match.
        import hashlib

        raw = hashlib.sha256(b"123456789").hexdigest()
        self.assertNotEqual(encrypt_ssn("123456789"), raw)


class PasswordHashTests(unittest.TestCase):
    def test_round_trip(self):
        hashed = encrypt("correct horse")
        self.assertTrue(check_encrypted_password("correct horse", hashed))
        self.assertTrue(verify_password(hashed, "correct horse"))

    def test_wrong_password_rejected(self):
        hashed = encrypt("correct horse")
        self.assertFalse(check_encrypted_password("wrong battery", hashed))
        self.assertFalse(verify_password(hashed, "wrong battery"))

    def test_hashes_are_salted(self):
        self.assertNotEqual(encrypt("same-password"), encrypt("same-password"))

    def test_empty_password_hashes_and_verifies(self):
        hashed = encrypt("")
        self.assertTrue(check_encrypted_password("", hashed))
        self.assertFalse(check_encrypted_password("x", hashed))


if __name__ == "__main__":
    unittest.main()
