import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from utility.statement import (
    AccountError,
    AmountError,
    MemoryStatementStore,
    PeriodError,
    SqliteStatementStore,
    StatementError,
    StatementPolicy,
    StatementService,
    month_id,
    money_str,
    normalize_account,
    parse_money,
    resolve_period,
)


JAN = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()
FEB = datetime(2026, 2, 10, 12, tzinfo=timezone.utc).timestamp()
MAR = datetime(2026, 3, 5, 12, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, ts=MAR):
        self.ts = ts

    def __call__(self):
        return self.ts


class StatementUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(MAR)
        self.service = StatementService(StatementPolicy(), MemoryStatementStore(), clock=self.clock)

    def journal(self, account='1001', amount='100.00', kind='deposit', **kwargs):
        return self.service.observe(account, amount, kind, userid=kwargs.pop('userid', 'alice'), **kwargs)

    def test_normalize_account_strips_float_suffix(self):
        self.assertEqual(normalize_account('1001.0'), '1001')

    def test_normalize_account_rejects_junk(self):
        with self.assertRaises(AccountError):
            normalize_account('ab12')

    def test_parse_money_strips_dollar(self):
        self.assertEqual(parse_money('$12.50'), Decimal('12.50'))

    def test_parse_money_rejects_negative(self):
        with self.assertRaises(AmountError):
            parse_money('-1')

    def test_monthly_defaults_to_previous_closed_month(self):
        spec = resolve_period('monthly', now=MAR)
        self.assertEqual(spec.kind, 'monthly')
        self.assertEqual(spec.period_id, '2026-02')
        self.assertFalse(spec.interim)

    def test_current_month_is_interim(self):
        spec = resolve_period('monthly', period='2026-03', now=MAR)
        self.assertTrue(spec.interim)
        self.assertLess(spec.end_ts, datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp())

    def test_future_month_rejected(self):
        with self.assertRaises(StatementError) as ctx:
            resolve_period('monthly', period='2026-04', now=MAR)
        self.assertEqual(ctx.exception.code, 'period_in_future')

    def test_custom_end_of_day_and_max_range(self):
        spec = resolve_period('custom', start='2026-01-01', end='2026-01-31', now=MAR)
        self.assertEqual(spec.period_id, '2026-01-01:2026-01-31')
        with self.assertRaises(StatementError) as ctx:
            resolve_period('custom', start='2024-01-01', end='2026-03-01', now=MAR)
        self.assertEqual(ctx.exception.code, 'range_too_long')

    def test_invalid_period_kind(self):
        with self.assertRaises(PeriodError):
            resolve_period('weekly')

    def test_observe_sets_running_balance(self):
        first = self.journal(amount='250.00', kind='open', posted_at=JAN)
        second = self.journal(amount='40.00', kind='withdraw', posted_at=FEB)
        self.assertEqual(money_str(first.balance), '250.00')
        self.assertEqual(money_str(second.balance), '210.00')
        self.assertEqual(second.direction, 'debit')

    def test_observe_is_idempotent_by_source(self):
        first = self.journal(amount='20.00', source_id='dep:1')
        second = self.journal(amount='20.00', source_id='dep:1')
        self.assertEqual(first.entry_id, second.entry_id)
        self.assertEqual(len(self.service.journal(account='1001')), 1)

    def test_observe_uses_supplied_balance(self):
        self.journal(amount='10.00', kind='deposit', balance='999.00')
        last = self.service.store.last_entry('1001')
        self.assertEqual(money_str(last.balance), '999.00')

    def test_monthly_statement_opening_and_totals(self):
        self.clock.ts = JAN
        self.journal(amount='250.00', kind='open', posted_at=JAN)
        self.clock.ts = FEB
        self.journal(amount='50.00', kind='deposit', posted_at=FEB)
        self.journal(amount='20.00', kind='withdraw', posted_at=FEB + 3600)
        self.clock.ts = MAR
        item, created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        self.assertTrue(created)
        self.assertEqual(item.occurrence_id, '1001:monthly:2026-02')
        self.assertEqual(money_str(item.opening_balance), '250.00')
        self.assertEqual(money_str(item.credits), '50.00')
        self.assertEqual(money_str(item.debits), '20.00')
        self.assertEqual(money_str(item.closing_balance), '280.00')
        self.assertEqual(item.entry_count, 2)

    def test_empty_period_still_generates(self):
        item, created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        self.assertTrue(created)
        self.assertEqual(item.entry_count, 0)
        self.assertEqual(money_str(item.opening_balance), '0.00')
        self.assertEqual(money_str(item.closing_balance), '0.00')

    def test_generate_is_idempotent(self):
        first, created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        second, created_again = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.statement_id, second.statement_id)

    def test_staff_force_regenerates(self):
        self.service.generate(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='1001', kind='monthly', period='2026-02',
        )
        self.journal(amount='15.00', kind='deposit', posted_at=FEB)
        item, created = self.service.generate(
            owner_userid='alice', actor='t1', actor_type='tier1',
            account='1001', kind='monthly', period='2026-02', force=True,
        )
        self.assertFalse(created)
        self.assertEqual(item.entry_count, 1)

    def test_customer_cannot_use_other_account(self):
        with self.assertRaises(StatementError) as ctx:
            self.service.generate(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='2002', kind='monthly', period='2026-02',
                own_accounts=['1001'],
            )
        self.assertEqual(ctx.exception.code, 'statement_forbidden')

    def test_ytd_includes_year_activity(self):
        self.journal(amount='100.00', kind='deposit', posted_at=JAN)
        self.journal(amount='25.00', kind='withdraw', posted_at=FEB)
        item, _created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='ytd', period='2026',
            own_accounts=['1001'],
        )
        self.assertEqual(item.period_id, '2026')
        self.assertEqual(item.entry_count, 2)
        self.assertTrue(item.interim)

    def test_official_request_and_staff_decide(self):
        generated, _created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        req = self.service.request(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', statement_id=generated.statement_id,
            delivery='mail', own_accounts=['1001'],
        )
        self.assertEqual(req.status, 'pending')
        with self.assertRaises(StatementError) as ctx:
            self.service.decide_request(actor='alice', actor_type='customer', request_id=req.request_id, approve=True)
        self.assertEqual(ctx.exception.code, 'statement_forbidden')
        decided = self.service.decide_request(actor='t1', actor_type='tier1', request_id=req.request_id, approve=True)
        self.assertEqual(decided.status, 'approved')

    def test_duplicate_official_request(self):
        generated, _created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        self.service.request(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', statement_id=generated.statement_id,
            own_accounts=['1001'],
        )
        with self.assertRaises(StatementError) as ctx:
            self.service.request(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', statement_id=generated.statement_id,
                own_accounts=['1001'],
            )
        self.assertEqual(ctx.exception.code, 'request_duplicate')

    def test_statement_limit(self):
        limited = StatementService(StatementPolicy(max_statements_per_account=1), MemoryStatementStore(), clock=self.clock)
        limited.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-01',
            own_accounts=['1001'],
        )
        with self.assertRaises(StatementError) as ctx:
            limited.generate(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', kind='monthly', period='2026-02',
                own_accounts=['1001'],
            )
        self.assertEqual(ctx.exception.code, 'statement_limit')

    def test_get_hides_foreign_statement(self):
        item, _created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', kind='monthly', period='2026-02',
            own_accounts=['1001'],
        )
        with self.assertRaises(StatementError) as ctx:
            self.service.get(
                owner_userid='bob', actor='bob', actor_type='customer',
                statement_id=item.statement_id, own_accounts=['2002'],
            )
        self.assertEqual(ctx.exception.code, 'statement_forbidden')

    def test_snapshot_lists_available_periods(self):
        self.journal(amount='10.00', kind='deposit', posted_at=JAN)
        snap = self.service.snapshot('alice')
        self.assertIn('1001', snap['accounts'])
        self.assertIn('2026-01', snap['accounts']['1001']['available_periods'])
        self.assertEqual(month_id(MAR), '2026-03')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteStatementStore(path)
            service = StatementService(StatementPolicy(), store, clock=self.clock)
            service.observe('1001', '30.00', 'deposit', userid='alice', posted_at=FEB, source_id='dep-sql')
            item, created = service.generate(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', kind='monthly', period='2026-02',
                own_accounts=['1001'],
            )
            again = StatementService(StatementPolicy(), SqliteStatementStore(path), clock=self.clock)
            loaded = again.store.get_statement(item.statement_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.entry_count, 1)
            self.assertTrue(created)
            self.assertEqual(again.store.get_by_source('dep-sql').amount, Decimal('30.00'))
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
