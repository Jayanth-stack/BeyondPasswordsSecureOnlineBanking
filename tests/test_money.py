"""Tests for canonical money parsing."""

import math
import unittest
from decimal import Decimal

from utility.money import AmountError, canonical_amount, parse_amount, try_canonical_amount


class ParseAmountTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(parse_amount(10), Decimal('10.00'))

    def test_string_two_dp(self):
        self.assertEqual(parse_amount('12.50'), Decimal('12.50'))

    def test_string_one_dp(self):
        self.assertEqual(parse_amount('12.5'), Decimal('12.50'))

    def test_string_whole(self):
        self.assertEqual(parse_amount('12'), Decimal('12.00'))

    def test_decimal_input(self):
        self.assertEqual(parse_amount(Decimal('3.14')), Decimal('3.14'))

    def test_float_two_dp(self):
        self.assertEqual(parse_amount(10.25), Decimal('10.25'))

    def test_rejects_empty(self):
        with self.assertRaises(AmountError) as ctx:
            parse_amount('')
        self.assertEqual(ctx.exception.code, 'invalid_amount')

    def test_rejects_none(self):
        with self.assertRaises(AmountError):
            parse_amount(None)

    def test_rejects_bool(self):
        with self.assertRaises(AmountError):
            parse_amount(True)

    def test_rejects_letters(self):
        with self.assertRaises(AmountError):
            parse_amount('abc')

    def test_rejects_scientific(self):
        with self.assertRaises(AmountError):
            parse_amount('1e2')

    def test_rejects_three_decimals(self):
        with self.assertRaises(AmountError):
            parse_amount('10.001')
        with self.assertRaises(AmountError) as ctx:
            parse_amount(Decimal('10.001'))
        self.assertIn('two decimal', str(ctx.exception))

    def test_rejects_negative(self):
        with self.assertRaises(AmountError):
            parse_amount('-1.00')

    def test_rejects_zero_by_default(self):
        with self.assertRaises(AmountError):
            parse_amount('0')
        with self.assertRaises(AmountError):
            parse_amount(0)

    def test_allow_zero(self):
        self.assertEqual(parse_amount('0', allow_zero=True), Decimal('0.00'))

    def test_rejects_leading_dot(self):
        with self.assertRaises(AmountError):
            parse_amount('.50')

    def test_rejects_nan_inf(self):
        with self.assertRaises(AmountError):
            parse_amount(math.nan)
        with self.assertRaises(AmountError):
            parse_amount(math.inf)

    def test_rejects_too_large(self):
        with self.assertRaises(AmountError) as ctx:
            parse_amount('1000000000.01')
        self.assertEqual(ctx.exception.code, 'amount_too_large')

    def test_max_allowed(self):
        self.assertEqual(parse_amount('1000000000.00'), Decimal('1000000000.00'))

    def test_canonical_string(self):
        self.assertEqual(canonical_amount('5'), '5.00')
        self.assertEqual(canonical_amount('5.1'), '5.10')

    def test_try_canonical_none_on_bad(self):
        self.assertIsNone(try_canonical_amount('nope'))
        self.assertEqual(try_canonical_amount('1.2'), '1.20')

    def test_strips_whitespace(self):
        self.assertEqual(parse_amount('  8.00  '), Decimal('8.00'))


if __name__ == '__main__':
    unittest.main()
