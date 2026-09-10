import os
import tempfile
import unittest
from decimal import Decimal

from utility.credit_limit import (
    AmountError,
    CreditLimitError,
    CreditLimitPolicy,
    CreditLimitService,
    MemoryCreditLimitStore,
    SqliteCreditLimitStore,
    charge_allowed,
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


class CreditLimitServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.balances = {'9001': Decimal('0.00')}
        self.service = CreditLimitService(
            CreditLimitPolicy(),
            MemoryCreditLimitStore(),
            clock=self.clock,
            is_credit_loader=lambda account, userid: str(account) in {'9001', '9001.0'},
            balance_loader=lambda account: (
                ('credit', self.balances[account]) if account in self.balances else ('savings', Decimal('100'))
            ),
        )
        self.service.ensure_facility(userid='alice', account='9001')
        set_service(self.service)

    def tearDown(self):
        set_service(None)

    def test_parse_money_rejects_negative(self):
        with self.assertRaises(AmountError):
            parse_money('-1')

    def test_savings_not_gated(self):
        decision = self.service.evaluate(
            operation='transfer', account='1001', amount='4000', balance='50', account_type='savings'
        )
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, 'not_credit')

    def test_deposit_not_gated(self):
        decision = self.service.evaluate(
            operation='deposit', account='9001', amount='9000', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)

    def test_default_limit_blocks_over_5000(self):
        decision = self.service.evaluate(
            operation='withdraw', account='9001', amount='5000.01', balance='0', account_type='credit'
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, 'credit_limit_exceeded')

    def test_default_limit_allows_5000(self):
        decision = self.service.evaluate(
            operation='withdraw', account='9001', amount='5000', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)

    def test_utilized_balance_reduces_available(self):
        decision = self.service.evaluate(
            operation='transfer', account='9001', amount='4000', balance='-2000', account_type='credit'
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.available, Decimal('3000.00'))

    def test_staff_can_raise_limit(self):
        self.service.set_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='9001', limit='8000'
        )
        decision = self.service.evaluate(
            operation='withdraw', account='9001', amount='7000', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)

    def test_customer_cannot_set_limit(self):
        with self.assertRaises(CreditLimitError) as caught:
            self.service.set_limit(
                owner_userid='alice', actor='alice', actor_type='customer', account='9001', limit='8000'
            )
        self.assertEqual(caught.exception.code, 'limit_forbidden')

    def test_limit_out_of_range(self):
        with self.assertRaises(CreditLimitError) as caught:
            self.service.set_limit(
                owner_userid='alice', actor='t1', actor_type='tier1', account='9001', limit='50'
            )
        self.assertEqual(caught.exception.code, 'limit_out_of_range')

    def test_adjust_limit_delta(self):
        facility = self.service.adjust_limit(
            owner_userid='alice', actor='t1', actor_type='tier1', account='9001', delta='500'
        )
        self.assertEqual(facility.base_limit, Decimal('5500.00'))

    def test_temp_increase_expires(self):
        self.service.grant_temp_increase(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='9001', amount='1000', seconds=60,
        )
        decision = self.service.evaluate(
            operation='withdraw', account='9001', amount='5500', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)
        self.clock.advance(61)
        decision = self.service.evaluate(
            operation='withdraw', account='9001', amount='5500', balance='0', account_type='credit'
        )
        self.assertTrue(decision.blocked)

    def test_reservation_blocks_second_charge(self):
        self.service.reserve(
            owner_userid='alice', account='9001', amount='4000', operation='transfer',
            balance='0', account_type='credit',
        )
        with self.assertRaises(CreditLimitError) as caught:
            self.service.reserve(
                owner_userid='alice', account='9001', amount='2000', operation='transfer',
                balance='0', account_type='credit',
            )
        self.assertEqual(caught.exception.code, 'credit_limit_exceeded')

    def test_capture_then_evaluate_uses_live_balance(self):
        self.service.reserve(
            owner_userid='alice', account='9001', amount='4000', operation='transfer',
            balance='0', account_type='credit',
        )
        self.service.capture_matching('9001', '4000', 'transfer')
        decision = self.service.evaluate(
            operation='approve', account='9001', amount='4000', balance='0', account_type='credit'
        )
        self.assertFalse(decision.blocked)

    def test_void_restores_available(self):
        self.service.reserve(
            owner_userid='alice', account='9001', amount='4000', operation='transfer',
            balance='0', account_type='credit',
        )
        self.service.void_matching('9001', '4000', 'transfer')
        self.service.reserve(
            owner_userid='alice', account='9001', amount='4000', operation='transfer',
            balance='0', account_type='credit',
        )

    def test_customer_request_and_staff_approve(self):
        req = self.service.request_increase(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', requested_limit='7000', reason='travel',
            own_accounts=['9001'],
        )
        self.assertEqual(req.status, 'pending')
        decided = self.service.decide_request(
            actor='t1', actor_type='tier1', request_id=req.request_id, approve=True
        )
        self.assertEqual(decided.status, 'approved')
        facility = self.service.store.get_by_account('9001')
        self.assertEqual(facility.base_limit, Decimal('7000.00'))

    def test_customer_cannot_request_decrease(self):
        with self.assertRaises(CreditLimitError) as caught:
            self.service.request_increase(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', requested_limit='1000', own_accounts=['9001'],
            )
        self.assertEqual(caught.exception.code, 'limit_not_increase')

    def test_ownership_gate(self):
        with self.assertRaises(CreditLimitError) as caught:
            self.service.request_increase(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', requested_limit='7000', own_accounts=['1001'],
            )
        self.assertEqual(caught.exception.code, 'limit_forbidden')

    def test_employee_still_limited(self):
        decision = self.service.evaluate(
            operation='approve', account='9001', amount='6000', balance='0',
            account_type='credit', userid='alice',
        )
        self.assertTrue(decision.blocked)

    def test_charge_allowed_uses_service(self):
        self.assertTrue(charge_allowed('9001', '100', '0', 'credit'))
        self.assertFalse(charge_allowed('9001', '6000', '0', 'credit'))

    def test_charge_allowed_fallback_when_service_missing(self):
        set_service(None)
        # Recreate a broken get_service path by pointing at a dummy that raises.
        from utility import credit_limit as mod

        class Boom:
            def capture_matching(self, *args, **kwargs):
                raise RuntimeError('store down')

            def evaluate(self, *args, **kwargs):
                raise RuntimeError('store down')

        original = mod._DEFAULT_SERVICE
        try:
            mod._DEFAULT_SERVICE = Boom()
            self.assertTrue(charge_allowed('9001', '1000', '0', 'credit'))
            self.assertFalse(charge_allowed('9001', '6000', '0', 'credit'))
        finally:
            mod._DEFAULT_SERVICE = original

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCreditLimitStore(path)
            service = CreditLimitService(CreditLimitPolicy(), store, clock=self.clock)
            service.ensure_facility(userid='alice', account='9001')
            service.set_limit(
                owner_userid='alice', actor='t1', actor_type='tier1', account='9001', limit='6000'
            )
            reloaded = SqliteCreditLimitStore(path)
            facility = reloaded.get_by_account('9001')
            self.assertEqual(facility.base_limit, Decimal('6000.00'))
        finally:
            os.remove(path)

    def test_snapshot_includes_utilization(self):
        snap = self.service.snapshot('alice', balances={'9001': '-250.00'})
        self.assertEqual(snap['facilities'][0]['utilized'], '250.00')
        self.assertEqual(snap['facilities'][0]['available'], '4750.00')
        self.assertEqual(snap['policy']['default_limit'], '5000.00')


if __name__ == '__main__':
    unittest.main()
