"""TOTP verify-once semantics — reused codes must not stay valid."""
import unittest
from unittest.mock import MagicMock

import tests  # noqa: F401

from otp import OtpInterface


class OtpVerifyTests(unittest.TestCase):
    def test_valid_otp_consumes_totp(self):
        iface = OtpInterface()
        iface.totp = MagicMock()
        iface.totp.verify.return_value = True

        self.assertEqual(iface.verify("123456"), "Verified")
        self.assertIsNone(iface.totp)
        self.assertEqual(iface.verify("123456"), "Must send OTP first before Verification ")

    def test_invalid_otp_keeps_totp(self):
        iface = OtpInterface()
        totp = MagicMock()
        totp.verify.return_value = False
        iface.totp = totp

        self.assertEqual(iface.verify("000000"), "Otp not verified")
        self.assertIs(iface.totp, totp)

    def test_verify_without_totp(self):
        iface = OtpInterface()
        iface.totp = None
        self.assertEqual(iface.verify("123456"), "Must send OTP first before Verification ")


if __name__ == "__main__":
    unittest.main()
