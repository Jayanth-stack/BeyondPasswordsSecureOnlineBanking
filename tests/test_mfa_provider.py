import unittest

from utility.mfa import (
    LOGIN_PURPOSE,
    PASSWORD_RESET_PURPOSE,
    LocalOtpProvider,
    MfaCooldownError,
    MfaService,
    SendCooldown,
    compute_local_otp,
    normalize_phone,
    twilio_configured,
)


class NormalizePhoneTests(unittest.TestCase):
    def test_ten_digit_us_number(self):
        self.assertEqual(normalize_phone('4805550199'), '+14805550199')

    def test_already_e164(self):
        self.assertEqual(normalize_phone('+14805550199'), '+14805550199')


class LocalOtpProviderTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'unit-test-secret'
        self.provider = LocalOtpProvider(secret=self.secret)

    def test_login_code_verifies_in_current_window(self):
        phone = '+14805550199'
        code = compute_local_otp(phone, LOGIN_PURPOSE, secret=self.secret)
        self.assertTrue(self.provider.verify(phone, LOGIN_PURPOSE, code))

    def test_login_code_cannot_be_reused_for_password_reset(self):
        phone = '+14805550199'
        login_code = compute_local_otp(phone, LOGIN_PURPOSE, secret=self.secret)
        self.assertFalse(self.provider.verify(phone, PASSWORD_RESET_PURPOSE, login_code))

    def test_wrong_code_fails(self):
        self.assertFalse(self.provider.verify('+14805550199', LOGIN_PURPOSE, '000000'))


class MfaServiceCooldownTests(unittest.TestCase):
    def test_second_send_is_rate_limited(self):
        service = MfaService(provider=LocalOtpProvider(secret='cd'), cooldown=SendCooldown())
        service.send('4805550199', LOGIN_PURPOSE, cooldown_seconds=60)
        with self.assertRaises(MfaCooldownError):
            service.send('4805550199', LOGIN_PURPOSE, cooldown_seconds=60)

    def test_different_purpose_is_independent(self):
        service = MfaService(provider=LocalOtpProvider(secret='cd'), cooldown=SendCooldown())
        service.send('4805550199', LOGIN_PURPOSE, cooldown_seconds=60)
        service.send('4805550199', PASSWORD_RESET_PURPOSE, cooldown_seconds=60)


class TwilioDetectionTests(unittest.TestCase):
    def test_placeholder_credentials_are_not_configured(self):
        self.assertFalse(twilio_configured())


if __name__ == '__main__':
    unittest.main()
