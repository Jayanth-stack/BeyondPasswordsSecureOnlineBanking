import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from utility.interest import (
    AccountError,
    InterestError,
    InterestPolicy,
    InterestService,
    MemoryInterestStore,
    RateError,
    SqliteInterestStore,
    days_between,
    money_str,
    normalize_account,
    parse_apy,
    period_id,
    previous_period,
    rate_str,
)


JAN = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
FEB = datetime(2026, 2, 1, 1, tzinfo=timezone.utc).timestamp()
MAR = datetime(2026, 3, 1, 1, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, ts=JAN):
        self.ts = ts

    def __call__(self):
        return self.ts


class InterestUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(JAN)
        self.balances = {'2002': ('savings', '10000.00'), '1001': ('checkin', '500.00'), '9001': ('credit', '0.00')}
        self.credits = []
        self.service = InterestService(
            InterestPolicy(),
            MemoryInterestStore(),
            clock=self.clock,
            is_savings_loader=lambda account, userid=None: str(account) in {'2002', '2002.0'},
            balance_loader=lambda account: self.balances.get(str(int(float(account)))),
            credit_executor=lambda account, amount, remark: self.credits.append((account, str(amount), remark)),
        )

    def enroll(self, apy='4.25', account='2002'):
        return self.service.set_apy(
            owner_userid='alice',
            actor='t1',
            actor_type='tier1',
            account=account,
            apy=apy,
        )

    def test_normalize_account_strips_float_suffix(self):
        self.assertEqual(normalize_account('2002.0'), '2002')

    def test_normalize_account_rejects_junk(self):
        with self.assertRaises(AccountError):
            normalize_account('ab12')

    def test_parse_apy_strips_percent(self):
        self.assertEqual(parse_apy('4.25%'), Decimal('4.25'))

    def test_parse_apy_rejects_negative(self):
        with self.assertRaises(RateError):
            parse_apy('-1')

    def test_period_helpers(self):
        self.assertEqual(period_id(JAN), '2026-01')
        self.assertEqual(previous_period(FEB), '2026-01')
        self.assertEqual(days_between(JAN, JAN + 3 * 86400), 3)

    def test_checking_and_credit_are_not_savings(self):
        with self.assertRaises(InterestError) as ctx:
            self.service.set_apy(
                owner_userid='alice', actor='t1', actor_type='tier1',
                account='1001', apy='1.00',
            )
        self.assertEqual(ctx.exception.code, 'not_savings')
        with self.assertRaises(InterestError) as ctx:
            self.service.set_apy(
                owner_userid='alice', actor='t1', actor_type='tier1',
                account='9001', apy='1.00',
            )
        self.assertEqual(ctx.exception.code, 'not_savings')

    def test_customer_cannot_set_apy(self):
        with self.assertRaises(InterestError) as ctx:
            self.service.set_apy(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='2002', apy='2.00',
            )
        self.assertEqual(ctx.exception.code, 'interest_forbidden')

    def test_default_unenrolled_does_not_accrue(self):
        item = self.service.ensure_account(userid='alice', account='2002', account_type='savings', balance='10000')
        self.clock.ts = JAN + 20 * 86400
        self.service.accrue(item)
        self.assertEqual(money_str(item.accrued), '0.00')
        self.assertFalse(item.enrolled if hasattr(item, 'enrolled') else item.enabled)

    def test_staff_enroll_and_daily_accrual(self):
        item = self.enroll()
        self.assertEqual(rate_str(item.base_apy), '4.25')
        self.assertTrue(item.enabled)
        self.clock.ts = JAN + 10 * 86400
        item = self.service.accrue(self.service.store.get_by_account('2002'))
        expected = (Decimal('10000.00') * Decimal('4.25') / Decimal('36500')) * 10
        self.assertAlmostEqual(float(item.accrued), float(expected), places=5)

    def test_zero_and_negative_balance_do_not_accrue(self):
        self.enroll()
        item = self.service.store.get_by_account('2002')
        item.last_balance = Decimal('0')
        self.service.store.update_account(item)
        self.clock.ts = JAN + 10 * 86400
        item = self.service.accrue(self.service.store.get_by_account('2002'))
        self.assertEqual(money_str(item.accrued), '0.00')

    def test_observe_uses_prior_balance_for_elapsed_days(self):
        self.enroll()
        self.clock.ts = JAN + 5 * 86400
        self.service.observe('2002', '20000.00', userid='alice', account_type='savings')
        item = self.service.store.get_by_account('2002')
        five_days_at_10k = (Decimal('10000.00') * Decimal('4.25') / Decimal('36500')) * 5
        self.assertAlmostEqual(float(item.accrued), float(five_days_at_10k), places=5)
        self.assertEqual(money_str(item.last_balance), '20000.00')

    def test_month_rollover_posts_once(self):
        self.enroll()
        self.clock.ts = JAN + 16 * 86400  # Jan 31
        self.service.accrue(self.service.store.get_by_account('2002'))
        self.clock.ts = FEB
        posted = self.service.post_due(owner_userid='alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].period, '2026-01')
        self.assertEqual(posted[0].occurrence_id, '2002:2026-01')
        self.assertEqual(len(self.credits), 1)
        self.assertGreater(Decimal(self.credits[0][1]), Decimal('0'))

    def test_posting_is_idempotent(self):
        self.enroll()
        self.clock.ts = FEB
        first = self.service.post_due(owner_userid='alice')
        second = self.service.post_due(owner_userid='alice')
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        self.assertEqual(len(self.credits), 1)

    def test_catch_up_is_one_credit(self):
        self.enroll()
        self.clock.ts = MAR
        posted = self.service.post_due(owner_userid='alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].period, '2026-02')
        self.assertEqual(len(self.credits), 1)

    def test_opening_month_does_not_post(self):
        self.enroll()
        self.clock.ts = JAN + 10 * 86400
        posted = self.service.post_due(owner_userid='alice')
        self.assertEqual(posted, [])
        self.assertEqual(self.credits, [])

    def test_revoke_stops_future_accrual_keeps_earned(self):
        self.enroll()
        self.clock.ts = JAN + 10 * 86400
        self.service.accrue(self.service.store.get_by_account('2002'))
        earned = self.service.store.get_by_account('2002').accrued
        self.assertGreater(earned, Decimal('0'))
        self.service.revoke(owner_userid='alice', actor='t1', actor_type='tier1', account='2002')
        self.clock.ts = JAN + 20 * 86400
        item = self.service.accrue(self.service.store.get_by_account('2002'))
        self.assertAlmostEqual(float(item.accrued), float(earned), places=5)
        self.assertFalse(item.enabled)

    def test_promo_expires(self):
        self.enroll()
        self.service.grant_promo(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='2002', apy='1.00', seconds=86400,
        )
        item = self.service.store.get_by_account('2002')
        self.assertEqual(rate_str(item.effective_apy(self.clock.ts)), '5.25')
        self.assertEqual(rate_str(item.effective_apy(self.clock.ts + 86401)), '4.25')

    def test_promo_requires_enrollment(self):
        self.service.ensure_account(userid='alice', account='2002', account_type='savings')
        with self.assertRaises(InterestError) as ctx:
            self.service.grant_promo(
                owner_userid='alice', actor='t1', actor_type='tier1',
                account='2002', apy='1.00',
            )
        self.assertEqual(ctx.exception.code, 'not_enrolled')

    def test_adjust_apy_up_and_down(self):
        self.enroll('2.00')
        up = self.service.adjust_apy(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='2002', delta='0.50',
        )
        self.assertEqual(rate_str(up.base_apy), '2.50')
        down = self.service.adjust_apy(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='2002', delta='-0.25',
        )
        self.assertEqual(rate_str(down.base_apy), '2.25')

    def test_apy_out_of_range(self):
        with self.assertRaises(InterestError) as ctx:
            self.enroll('25.00')
        self.assertEqual(ctx.exception.code, 'apy_out_of_range')

    def test_customer_request_and_staff_approve(self):
        req = self.service.request_apy(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='2002', requested_apy='1.50', reason='want yield',
        )
        self.assertEqual(req.status, 'pending')
        decided = self.service.decide_request(
            actor='t1', actor_type='tier1', request_id=req.request_id, approve=True,
        )
        self.assertEqual(decided.status, 'approved')
        item = self.service.store.get_by_account('2002')
        self.assertEqual(rate_str(item.base_apy), '1.50')
        self.assertTrue(item.enabled)

    def test_customer_request_duplicate_and_not_increase(self):
        self.enroll('2.00')
        with self.assertRaises(InterestError) as ctx:
            self.service.request_apy(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='2002', requested_apy='1.00',
            )
        self.assertEqual(ctx.exception.code, 'apy_not_increase')
        self.service.request_apy(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='2002', requested_apy='3.00',
        )
        with self.assertRaises(InterestError) as ctx:
            self.service.request_apy(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='2002', requested_apy='3.10',
            )
        self.assertEqual(ctx.exception.code, 'request_duplicate')

    def test_staff_deny_leaves_rate(self):
        req = self.service.request_apy(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='2002', requested_apy='2.00',
        )
        self.service.decide_request(
            actor='t1', actor_type='tier1', request_id=req.request_id, approve=False,
        )
        item = self.service.store.get_by_account('2002')
        self.assertEqual(rate_str(item.base_apy), '0.00')
        self.assertFalse(item.enabled)

    def test_failed_credit_does_not_consume_occurrence(self):
        self.service.credit_executor = lambda account, amount, remark: 'Cannot credit funds'
        self.enroll()
        self.clock.ts = FEB
        posted = self.service.post_due(owner_userid='alice')
        self.assertEqual(posted, [])
        self.assertIsNone(self.service.store.get_by_occurrence('2002:2026-01'))

    def test_ytd_resets_on_new_year(self):
        self.enroll()
        self.clock.ts = FEB
        self.service.post_due(owner_userid='alice')
        item = self.service.store.get_by_account('2002')
        self.assertGreater(item.ytd_posted, Decimal('0'))
        self.clock.ts = datetime(2027, 1, 2, tzinfo=timezone.utc).timestamp()
        self.service.accrue(item)
        self.assertEqual(money_str(item.ytd_posted), '0.00')
        self.assertEqual(item.ytd_year, 2027)

    def test_snapshot_includes_policy_and_unenrolled_savings(self):
        snap = self.service.snapshot('alice', balances={'2002': '10000.00'})
        self.assertEqual(len(snap['accounts']), 1)
        self.assertFalse(snap['accounts'][0]['enrolled'])
        self.assertEqual(snap['accounts'][0]['base_apy'], '0.00')
        self.assertEqual(snap['policy']['max_apy'], '10.00')

    def test_force_post_mid_month_is_staff_path(self):
        self.enroll()
        self.clock.ts = JAN + 10 * 86400
        self.service.accrue(self.service.store.get_by_account('2002'))
        posted = self.service.post_due(owner_userid='alice', force=True)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].period, '2026-01')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInterestStore(path)
            service = InterestService(
                InterestPolicy(),
                store,
                clock=self.clock,
                is_savings_loader=lambda account, userid=None: True,
                balance_loader=lambda account: ('savings', '10000.00'),
                credit_executor=lambda account, amount, remark: None,
            )
            service.set_apy(
                owner_userid='alice', actor='t1', actor_type='tier1',
                account='2002', apy='3.00',
            )
            reloaded = SqliteInterestStore(path)
            item = reloaded.get_by_account('2002')
            self.assertEqual(rate_str(item.base_apy), '3.00')
            self.assertTrue(item.enabled)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
