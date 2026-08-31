"""Unit tests for the money-velocity store, policy, and rolling window."""

from decimal import Decimal
import os
import tempfile
import unittest

from utility.velocity import (
    AmountError,
    LimitBand,
    MemoryVelocityStore,
    SqliteVelocityStore,
    VelocityPolicy,
    VelocityService,
    parse_money,
)


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ParseMoneyTests(unittest.TestCase):
    def test_accepts_two_dp_and_ints(self):
        self.assertEqual(parse_money('10.50'), Decimal('10.50'))
        self.assertEqual(parse_money(10), Decimal('10.00'))
        self.assertEqual(parse_money('10'), Decimal('10.00'))
        self.assertEqual(parse_money(10.25), Decimal('10.25'))

    def test_rejects_junk_zero_negative_scientific_overprecise(self):
        for bad in (None, True, False, '', 'abc', '1e2', '0', 0, -1, '-5', '10.001', '1E3', '+12'):
            with self.subTest(bad=bad):
                with self.assertRaises(AmountError):
                    parse_money(bad)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = VelocityPolicy({
            ('customer', 'transfer'): LimitBand(Decimal('100.00'), 3, Decimal('60.00'), 86400),
            ('customer', 'outbound'): LimitBand(Decimal('150.00'), 5, None, 86400),
            ('employee', 'transfer'): LimitBand(Decimal('1000.00'), 50, Decimal('500.00'), 86400),
        })

    def test_per_txn_fires_before_daily(self):
        d = self.policy.evaluate(
            self.policy.band_for('customer', 'transfer'),
            Decimal('0'), 0, Decimal('60.01'), 'transfer',
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, 'per_txn_exceeded')

    def test_daily_amount_boundary_is_inclusive(self):
        band = self.policy.band_for('customer', 'transfer')
        ok = self.policy.evaluate(band, Decimal('40.00'), 1, Decimal('60.00'), 'transfer')
        self.assertTrue(ok.allowed)
        miss = self.policy.evaluate(band, Decimal('40.01'), 1, Decimal('60.00'), 'transfer')
        self.assertFalse(miss.allowed)
        self.assertEqual(miss.code, 'daily_amount_exceeded')
        self.assertEqual(miss.remaining_amount, Decimal('59.99'))

    def test_daily_count(self):
        band = self.policy.band_for('customer', 'transfer')
        d = self.policy.evaluate(band, Decimal('10.00'), 3, Decimal('10.00'), 'transfer')
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, 'daily_count_exceeded')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.policy = VelocityPolicy({
            ('customer', 'transfer'): LimitBand(Decimal('100.00'), 3, Decimal('80.00'), 100),
            ('customer', 'withdraw'): LimitBand(Decimal('50.00'), 2, Decimal('40.00'), 100),
            ('customer', 'cheque'): LimitBand(Decimal('70.00'), 2, Decimal('70.00'), 100),
            ('customer', 'outbound'): LimitBand(Decimal('120.00'), 4, None, 100),
            ('employee', 'transfer'): LimitBand(Decimal('500.00'), 10, Decimal('400.00'), 100),
        })
        self.svc = VelocityService(
            store=MemoryVelocityStore(),
            policy=self.policy,
            now=self.clock,
        )

    def test_happy_path_then_daily_cap(self):
        first = self.svc.consume('customer', 'transfer', '40.00', account=1001, userid='alice')
        self.assertTrue(first.allowed)
        self.assertEqual(first.remaining_amount, Decimal('60.00'))
        second = self.svc.consume('customer', 'transfer', '40.00', account=1001, userid='alice')
        self.assertTrue(second.allowed)
        blocked = self.svc.consume('customer', 'transfer', '40.00', account=1001, userid='alice')
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, 'daily_amount_exceeded')
        self.assertEqual(blocked.remaining_amount, Decimal('20.00'))

    def test_accounts_are_isolated_for_operation_band(self):
        first = self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.assertTrue(first.allowed)
        # Same user, other account: transfer band is per-account so this is still ok
        # (80 + 20 = 100, under the 120 outbound cap).
        other = self.svc.consume('customer', 'transfer', '20.00', account=2001, userid='alice')
        self.assertTrue(other.allowed)
        snap_a = self.svc.snapshot('customer', 'transfer', account=1001)
        snap_b = self.svc.snapshot('customer', 'transfer', account=2001)
        self.assertEqual(snap_a['used_amount'], '80.00')
        self.assertEqual(snap_b['used_amount'], '20.00')
        # Next $20 from 2001 is still under that account's transfer cap but trips outbound.
        blocked = self.svc.consume('customer', 'transfer', '20.01', account=2001, userid='alice')
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.operation, 'outbound')

    def test_outbound_aggregate_across_operations(self):
        self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.assertTrue(self.svc.consume('customer', 'withdraw', '30.00', account=1001, userid='alice').allowed)
        blocked = self.svc.consume('customer', 'cheque', '20.00', account=1001, userid='alice')
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, 'daily_amount_exceeded')
        self.assertEqual(blocked.operation, 'outbound')

    def test_window_expiry_restores_capacity(self):
        self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.clock.t += 101
        again = self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.assertTrue(again.allowed)

    def test_employee_band_is_higher(self):
        d = self.svc.consume('employee', 'transfer', '300.00', account=1001, userid='teller')
        self.assertTrue(d.allowed)
        customer = VelocityService(store=MemoryVelocityStore(), policy=self.policy, now=self.clock)
        blocked = customer.consume('customer', 'transfer', '300.00', account=1001, userid='alice')
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, 'per_txn_exceeded')

    def test_disabled_always_allows_valid_amounts(self):
        svc = VelocityService(store=MemoryVelocityStore(), policy=self.policy, now=self.clock, enabled=False)
        d = svc.consume('customer', 'transfer', '9999', account=1001, userid='alice')
        self.assertTrue(d.allowed)

    def test_release_frees_reservation(self):
        self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice', ref='tx-1')
        self.assertEqual(self.svc.release('transfer', account=1001, userid='alice', ref='tx-1'), 2)
        again = self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.assertTrue(again.allowed)

    def test_snapshot_matches_remaining(self):
        self.svc.consume('customer', 'transfer', '25.00', account=1001, userid='alice')
        snap = self.svc.snapshot('customer', 'transfer', account=1001)
        self.assertEqual(snap['used_amount'], '25.00')
        self.assertEqual(snap['remaining_amount'], '75.00')
        self.assertEqual(snap['used_count'], 1)
        self.assertEqual(snap['remaining_count'], 2)

    def test_retry_after_is_seconds_until_oldest_expires(self):
        self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        self.clock.t += 10
        blocked = self.svc.consume('customer', 'transfer', '30.00', account=1001, userid='alice')
        self.assertEqual(blocked.retry_after, 90)


class SqliteRestartTests(unittest.TestCase):
    def test_usage_survives_reopen(self):
        clock = Clock()
        policy = VelocityPolicy({
            ('customer', 'transfer'): LimitBand(Decimal('100.00'), 5, Decimal('100.00'), 86400),
            ('customer', 'outbound'): LimitBand(Decimal('100.00'), 5, None, 86400),
        })
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        os.unlink(path)
        try:
            store = SqliteVelocityStore(path)
            svc = VelocityService(store=store, policy=policy, now=clock)
            self.assertTrue(svc.consume('customer', 'transfer', '40.00', account=9, userid='bob').allowed)
            store.close()
            store2 = SqliteVelocityStore(path)
            svc2 = VelocityService(store=store2, policy=policy, now=clock)
            blocked = svc2.consume('customer', 'transfer', '70.00', account=9, userid='bob')
            self.assertFalse(blocked.allowed)
            self.assertEqual(blocked.remaining_amount, Decimal('60.00'))
            store2.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == '__main__':
    unittest.main()
