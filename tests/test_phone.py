"""Phone parsing feeds Twilio OTP delivery — bad numbers skip MFA."""
import unittest

import tests  # noqa: F401

from customer import format_phone_number, Customers
import customer as customer_module


class FormatPhoneNumberTests(unittest.TestCase):
    def test_e164_us(self):
        self.assertEqual(format_phone_number("+1 415 555 2671"), "+14155552671")

    def test_e164_international(self):
        self.assertEqual(format_phone_number("+44 20 7946 0958"), "+442079460958")

    def test_invalid_returns_none(self):
        self.assertIsNone(format_phone_number("not-a-phone"))
        self.assertIsNone(format_phone_number(""))

    def test_bare_national_without_region_is_rejected(self):
        self.assertIsNone(format_phone_number("4155552671"))


class RetrievePhoneNumberTests(unittest.TestCase):
    def setUp(self):
        customer_module.cursor.reset_mock()
        customer_module.cursor.fetchone.side_effect = None

    def test_prefixes_us_country_code(self):
        customer_module.cursor.fetchone.return_value = ("4155552671",)
        self.assertEqual(Customers().retrieve_phone_number("cust1"), "+14155552671")

    def test_keeps_existing_plus(self):
        customer_module.cursor.fetchone.return_value = ("+442079460958",)
        self.assertEqual(Customers().retrieve_phone_number("cust1"), "+442079460958")

    def test_missing_user_returns_none(self):
        customer_module.cursor.fetchone.return_value = None
        self.assertIsNone(Customers().retrieve_phone_number("missing"))


if __name__ == "__main__":
    unittest.main()
