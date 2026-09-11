import os
import tempfile
import unittest
from decimal import Decimal

from utility.overdraft import (
    AmountError,
    OverdraftError,
    OverdraftPolicy,
    OverdraftService,
    MemoryOverdraftStore,
    SqliteOverdraftStore,
    debit_allowed,
    parse_money,
    set_service,
)


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class OverdraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.balances = {'1001': Decimal('50.00')}
        self.service = OverdraftService(
            OverdraftPolicy(),
            MemoryOverdraftStore(),
            clock=self.clock,
            is_deposit_loader=lambda account, userid: str(account) in {'1001', '1001.0', '1002'},
            balance_loader=lambda account: (
                ('checkin', self.balances[account]) if account in self.balances else ('credit', Decimal('0'))
            ),
        )
        set_service(self.service)

    def tearDown(self):
        set_service(None)

    def test_parse_money_rejects_negative(self):
        with self.assertRaises(AmountError):
            parse_money('-1')

    def test_credit_not_gated(self):
        decision = self.service.evaluate(
            operation='transfer', account='9001', amount='4000', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, 'not_deposit_account')

    def test_deposit_not_gated(self):
        decision = self.service.evaluate(
            operation='deposit', account='1001', amount='9000', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)

    def test_unenrolled_hard_fails_at_zero(self):
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='50.01', balance='50', account_type='checkin'
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, 'overdraft_exceeded')
        self.assertFalse(decision.enrolled)

    def test_unenrolled_allows_exact_balance(self):
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='50', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)

    def test_staff_enroll_allows_overdraft(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='240', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.enrolled)
        self.assertEqual(decision.available, Decimal('250.00'))

    def test_enrolled_blocks_past_line(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='250.01', balance='50', account_type='checkin'
        )
        self.assertTrue(decision.blocked)

    def test_customer_cannot_set_limit(self):
        with self.assertRaises(OverdraftError) as caught:
            self.service.set_limit(
                owner_userid='alice', actor='alice', actor_type='customer', account='1001', limit='200'
            )
        self.assertEqual(caught.exception.code, 'overdraft_forbidden')

    def test_limit_out_of_range(self):
        with self.assertRaises(OverdraftError) as caught:
            self.service.set_limit(
                owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='10'
            )
        self.assertEqual(caught.exception.code, 'limit_out_of_range')

    def test_revoke_restores_hard_fail(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        self.service.revoke(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001'
        )
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='51', balance='50', account_type='checkin'
        )
        self.assertTrue(decision.blocked)

    def test_adjust_limit_delta(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        facility = self.service.adjust_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', delta='50'
        )
        self.assertEqual(facility.base_limit, Decimal('250.00'))

    def test_temp_increase_expires(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        self.service.grant_temp_increase(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='1001', amount='50', seconds=60,
        )
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='290', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)
        self.clock.advance(61)
        decision = self.service.evaluate(
            operation='withdraw', account='1001', amount='290', balance='50', account_type='checkin'
        )
        self.assertTrue(decision.blocked)

    def test_temp_requires_enrollment(self):
        with self.assertRaises(OverdraftError) as caught:
            self.service.grant_temp_increase(
                owner_userid='alice', actor='t1', actor_type='tier1',
                account='1001', amount='50', seconds=60,
            )
        self.assertEqual(caught.exception.code, 'not_enrolled')

    def test_fee_applies_when_going_negative(self):
        policy = OverdraftPolicy(fee=Decimal('35.00'), courtesy=Decimal('5.00'))
        service = OverdraftService(policy, MemoryOverdraftStore(), clock=self.clock)
        service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        decision = service.evaluate(
            operation='withdraw', account='1001', amount='60', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.fee, Decimal('35.00'))
        blocked = service.evaluate(
            operation='withdraw', account='1001', amount='220', balance='50', account_type='checkin'
        )
        self.assertTrue(blocked.blocked)

    def test_courtesy_waives_small_overdraft_fee(self):
        policy = OverdraftPolicy(fee=Decimal('35.00'), courtesy=Decimal('5.00'))
        service = OverdraftService(policy, MemoryOverdraftStore(), clock=self.clock)
        service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        decision = service.evaluate(
            operation='withdraw', account='1001', amount='54', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.fee, Decimal('0.00'))

    def test_reservation_blocks_second_debit(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        self.service.reserve(
            owner_userid='alice', account='1001', amount='200', operation='transfer',
            balance='50', account_type='checkin',
        )
        with self.assertRaises(OverdraftError) as caught:
            self.service.reserve(
                owner_userid='alice', account='1001', amount='60', operation='transfer',
                balance='50', account_type='checkin',
            )
        self.assertEqual(caught.exception.code, 'overdraft_exceeded')

    def test_unenrolled_reservation_against_balance(self):
        self.service.reserve(
            owner_userid='alice', account='1001', amount='40', operation='transfer',
            balance='50', account_type='checkin',
        )
        with self.assertRaises(OverdraftError):
            self.service.reserve(
                owner_userid='alice', account='1001', amount='20', operation='transfer',
                balance='50', account_type='checkin',
            )

    def test_capture_then_evaluate_uses_live_balance(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        self.service.reserve(
            owner_userid='alice', account='1001', amount='200', operation='transfer',
            balance='50', account_type='checkin',
        )
        self.service.capture_matching('1001', '200', 'transfer')
        decision = self.service.evaluate(
            operation='approve', account='1001', amount='200', balance='50', account_type='checkin'
        )
        self.assertFalse(decision.blocked)

    def test_void_restores_available(self):
        self.service.reserve(
            owner_userid='alice', account='1001', amount='40', operation='transfer',
            balance='50', account_type='checkin',
        )
        self.service.void_matching('1001', '40', 'transfer')
        self.service.reserve(
            owner_userid='alice', account='1001', amount='40', operation='transfer',
            balance='50', account_type='checkin',
        )

    def test_customer_request_and_staff_approve(self):
        req = self.service.request_increase(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', requested_limit='200', reason='paycheck float',
            own_accounts=['1001'],
        )
        self.assertEqual(req.status, 'pending')
        decided = self.service.decide_request(
            actor='t1', actor_type='tier1', request_id=req.request_id, approve=True
        )
        self.assertEqual(decided.status, 'approved')
        facility = self.service.store.get_by_account('1001')
        self.assertEqual(facility.base_limit, Decimal('200.00'))
        self.assertTrue(facility.enabled)

    def test_customer_cannot_request_decrease(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        with self.assertRaises(OverdraftError) as caught:
            self.service.request_increase(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', requested_limit='50', own_accounts=['1001'],
            )
        self.assertEqual(caught.exception.code, 'limit_not_increase')

    def test_ownership_gate(self):
        with self.assertRaises(OverdraftError) as caught:
            self.service.request_increase(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', requested_limit='200', own_accounts=['9001'],
            )
        self.assertEqual(caught.exception.code, 'overdraft_forbidden')

    def test_employee_still_limited(self):
        decision = self.service.evaluate(
            operation='approve', account='1001', amount='80', balance='50',
            account_type='checkin', userid='alice',
        )
        self.assertTrue(decision.blocked)

    def test_debit_allowed_uses_service(self):
        self.assertTrue(debit_allowed('1001', '40', '50', 'checkin'))
        self.assertFalse(debit_allowed('1001', '80', '50', 'checkin'))
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
        )
        self.assertTrue(debit_allowed('1001', '80', '50', 'checkin'))

    def test_debit_allowed_skips_credit(self):
        self.assertTrue(debit_allowed('9001', '6000', '0', 'credit'))

    def test_debit_allowed_fallback_when_service_missing(self):
        from utility import overdraft as mod

        class Boom:
            def capture_matching(self, *args, **kwargs):
                raise RuntimeError('store down')

            def evaluate(self, *args, **kwargs):
                raise RuntimeError('store down')

        original = mod._DEFAULT_SERVICE
        try:
            mod._DEFAULT_SERVICE = Boom()
            self.assertTrue(debit_allowed('1001', '40', '50', 'checkin'))
            self.assertFalse(debit_allowed('1001', '80', '50', 'checkin'))
        finally:
            mod._DEFAULT_SERVICE = original

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteOverdraftStore(path)
            service = OverdraftService(OverdraftPolicy(), store, clock=self.clock)
            service.set_limit(
                owner_userid='alice', actor='t1', actor_type='tier1', account='1001', limit='200'
            )
            reloaded = SqliteOverdraftStore(path)
            facility = reloaded.get_by_account('1001')
            self.assertEqual(facility.base_limit, Decimal('200.00'))
            self.assertTrue(facility.enabled)
        finally:
            os.remove(path)

    def test_snapshot_includes_unenrolled_payload_accounts(self):
        snap = self.service.snapshot('alice', balances={'1001': '50.00'})
        self.assertEqual(snap['facilities'][0]['enrolled'], False)
        self.assertEqual(snap['facilities'][0]['available'], '50.00')
        self.assertEqual(snap['policy']['default_limit'], '0.00')

    def test_savings_gated_same_as_checking(self):
        decision = self.service.evaluate(
            operation='withdraw', account='1002', amount='10', balance='5', account_type='savings'
        )
        self.assertTrue(decision.blocked)


if __name__ == '__main__':
    unittest.main()
