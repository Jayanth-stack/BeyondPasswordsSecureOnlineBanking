import os
import tempfile
import unittest
from decimal import Decimal

from utility.settlement import (
    AccountError,
    AmountError,
    HoldPolicy,
    MemorySettlementStore,
    SettlementService,
    SqliteSettlementStore,
    canonical_amount,
    enforce_hold,
    normalize_account,
    parse_money,
)


class ParseHelpersTests(unittest.TestCase):
    def test_parse_rejects_junk(self):
        for value in (None, True, '', 'abc', '1e2', '1.001', '0', '-1', '+10', '1.0.0'):
            with self.subTest(value=value):
                with self.assertRaises(AmountError):
                    parse_money(value)

    def test_parse_accepts_two_decimals(self):
        self.assertEqual(parse_money('10'), Decimal('10'))
        self.assertEqual(canonical_amount('10.50'), '10.50')
        self.assertEqual(parse_money('0.01'), Decimal('0.01'))

    def test_account_normalize(self):
        self.assertEqual(normalize_account('1001'), '1001')
        self.assertEqual(normalize_account('1001.0'), '1001')
        self.assertEqual(normalize_account('', required=False), '')
        with self.assertRaises(AccountError):
            normalize_account('')
        with self.assertRaises(AccountError):
            normalize_account('abc')


class PolicyTests(unittest.TestCase):
    def test_new_destination_is_held(self):
        policy = HoldPolicy(new_destination_seconds=3600)
        decision = policy.evaluate(
            usertype='customer',
            operation='transfer',
            amount=Decimal('25.00'),
            destination_known=False,
            own_account=False,
            has_destination=True,
        )
        self.assertTrue(decision.should_hold)
        self.assertEqual(decision.seconds, 3600)
        self.assertEqual(decision.reason, 'new_destination')

    def test_known_destination_skips_when_base_is_zero(self):
        policy = HoldPolicy(base_hold_seconds=0, new_destination_seconds=3600)
        decision = policy.evaluate(
            usertype='customer',
            operation='transfer',
            amount=Decimal('25.00'),
            destination_known=True,
            own_account=False,
            has_destination=True,
        )
        self.assertFalse(decision.should_hold)
        self.assertEqual(decision.reason, 'immediate')

    def test_known_destination_uses_base_hold(self):
        policy = HoldPolicy(base_hold_seconds=120, new_destination_seconds=3600)
        decision = policy.evaluate(
            usertype='customer',
            operation='transfer',
            amount=Decimal('25.00'),
            destination_known=True,
            own_account=False,
            has_destination=True,
        )
        self.assertEqual(decision.seconds, 120)
        self.assertEqual(decision.reason, 'policy')

    def test_own_account_and_employee_and_disabled(self):
        policy = HoldPolicy(new_destination_seconds=3600)
        self.assertEqual(
            policy.evaluate(
                usertype='customer', operation='transfer', amount=Decimal('10'),
                destination_known=False, own_account=True, has_destination=True,
            ).reason,
            'own_account',
        )
        self.assertEqual(
            policy.evaluate(
                usertype='tier1', operation='transfer', amount=Decimal('10'),
                destination_known=False, own_account=False, has_destination=True,
            ).reason,
            'employee',
        )
        disabled = HoldPolicy(enabled=False, new_destination_seconds=3600)
        self.assertEqual(
            disabled.evaluate(
                usertype='customer', operation='transfer', amount=Decimal('10'),
                destination_known=False, own_account=False, has_destination=True,
            ).reason,
            'disabled',
        )

    def test_amount_threshold_and_ungated_operation(self):
        policy = HoldPolicy(new_destination_seconds=3600, amount_threshold=Decimal('500'))
        self.assertEqual(
            policy.evaluate(
                usertype='customer', operation='transfer', amount=Decimal('10'),
                destination_known=False, own_account=False, has_destination=True,
            ).reason,
            'below_threshold',
        )
        self.assertTrue(
            policy.evaluate(
                usertype='customer', operation='transfer', amount=Decimal('500'),
                destination_known=False, own_account=False, has_destination=True,
            ).should_hold,
        )
        self.assertEqual(
            policy.evaluate(
                usertype='customer', operation='deposit', amount=Decimal('900'),
                destination_known=False, own_account=False, has_destination=False,
            ).reason,
            'ungated_operation',
        )

    def test_withdraw_uses_base_only(self):
        policy = HoldPolicy(base_hold_seconds=0, new_destination_seconds=3600)
        decision = policy.evaluate(
            usertype='customer',
            operation='withdraw',
            amount=Decimal('20'),
            destination_known=False,
            own_account=False,
            has_destination=False,
        )
        self.assertFalse(decision.should_hold)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.service = SettlementService(
            HoldPolicy(new_destination_seconds=100, max_open_holds=2),
            MemorySettlementStore(),
            clock=lambda: self.now[0],
        )

    def test_place_then_authorize_after_settle(self):
        held = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='2002', amount='25.00',
        )
        self.assertIsNotNone(held)
        body, status, headers = held
        self.assertEqual(status, 202)
        self.assertEqual(body['error'], 'held')
        self.assertEqual(body['reason'], 'new_destination')
        self.assertIn('Retry-After', headers)

        executed = []
        settled = self.service.settle_due(lambda hold: executed.append(hold.hold_id) or 'queued', now=1_050)
        self.assertEqual(settled, [])
        self.assertEqual(executed, [])

        self.now[0] = 1_200
        settled = self.service.settle_due(lambda hold: executed.append(hold.amount) or 'queued')
        self.assertEqual(len(settled), 1)
        self.assertEqual(executed, ['25.00'])
        self.assertTrue(self.service.destination_known('alice', '2002'))

        second = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='2002', amount='10.00',
        )
        self.assertIsNone(second)

    def test_cancel_prevents_settle(self):
        body, status, _ = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='2002', amount='25',
        )
        hold_id = body['hold_id']
        cancelled = self.service.cancel('alice', hold_id)
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = 5_000
        settled = self.service.settle_due(lambda hold: 'nope')
        self.assertEqual(settled, [])
        self.assertFalse(self.service.destination_known('alice', '2002'))

    def test_owner_isolation_and_duplicate_and_limit(self):
        enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='2', amount='5',
        )
        dup = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='2', amount='5.00',
        )
        self.assertEqual(dup[1], 409)
        self.assertEqual(dup[0]['error'], 'hold_duplicate')

        enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='3', amount='6',
        )
        over = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='4', amount='7',
        )
        self.assertEqual(over[0]['error'], 'hold_limit')

        bob = enforce_hold(
            self.service, userid='bob', usertype='customer', operation='transfer',
            from_account='1', to_account='2', amount='5',
        )
        self.assertEqual(bob[1], 202)

    def test_cancel_foreign_hold_forbidden(self):
        body, _, _ = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='2', amount='5',
        )
        with self.assertRaises(Exception) as ctx:
            self.service.cancel('bob', body['hold_id'])
        self.assertEqual(ctx.exception.code, 'hold_forbidden')

    def test_own_account_and_employee_skip(self):
        self.assertIsNone(enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='1002', amount='40',
            own_accounts=['1001', '1002'],
        ))
        self.assertIsNone(enforce_hold(
            self.service, userid='teller', usertype='tier1', operation='transfer',
            from_account='1001', to_account='9999', amount='40',
        ))

    def test_invalid_amount_and_account(self):
        body, status, _ = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='2002', amount='nope',
        )
        self.assertEqual((status, body['error']), (400, 'invalid_amount'))
        body, status, _ = enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1001', to_account='dest', amount='10',
        )
        self.assertEqual((status, body['error']), (400, 'invalid_account'))

    def test_failed_executor_leaves_hold_open(self):
        enforce_hold(
            self.service, userid='alice', usertype='customer', operation='transfer',
            from_account='1', to_account='2', amount='9',
        )
        self.now[0] = 2_000

        def boom(_hold):
            raise RuntimeError('db down')

        self.assertEqual(self.service.settle_due(boom), [])
        open_holds = self.service.list_for('alice', include_closed=False)
        self.assertEqual(len(open_holds), 1)
        self.assertEqual(open_holds[0].last_error, 'db down')

        self.assertEqual(len(self.service.settle_due(lambda hold: 'ok')), 1)

    def test_sqlite_reopen_keeps_hold_and_destination(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        now = [50.0]
        first = SettlementService(
            HoldPolicy(new_destination_seconds=10),
            SqliteSettlementStore(path),
            clock=lambda: now[0],
        )
        enforce_hold(
            first, userid='alice', usertype='customer', operation='cheque',
            from_account='11', to_account='22', amount='15',
        )
        now[0] = 80
        first.settle_due(lambda hold: 'Success')

        second = SettlementService(
            HoldPolicy(new_destination_seconds=10),
            SqliteSettlementStore(path),
            clock=lambda: now[0],
        )
        self.assertTrue(second.destination_known('alice', '22'))
        self.assertIsNone(enforce_hold(
            second, userid='alice', usertype='customer', operation='cheque',
            from_account='11', to_account='22', amount='3',
        ))
        held = enforce_hold(
            second, userid='alice', usertype='customer', operation='transfer',
            from_account='11', to_account='99', amount='3',
        )
        self.assertEqual(held[1], 202)


if __name__ == '__main__':
    unittest.main()
