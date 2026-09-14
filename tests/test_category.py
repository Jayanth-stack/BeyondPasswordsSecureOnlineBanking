import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from utility.category import (
    AccountError,
    AmountError,
    CategoryError,
    CategoryPolicy,
    CategoryService,
    MemoryCategoryStore,
    PeriodError,
    SqliteCategoryStore,
    month_id,
    money_str,
    normalize_account,
    parse_money,
    resolve_month,
)


MAR = datetime(2026, 3, 5, 12, tzinfo=timezone.utc).timestamp()
FEB = datetime(2026, 2, 10, 12, tzinfo=timezone.utc).timestamp()
JAN = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, ts=MAR):
        self.ts = ts

    def __call__(self):
        return self.ts


class CategoryUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(MAR)
        self.service = CategoryService(CategoryPolicy(), MemoryCategoryStore(), clock=self.clock)

    def observe(self, account='1001', amount='40.00', kind='withdraw', **kwargs):
        kwargs.setdefault('userid', 'alice')
        kwargs.setdefault('posted_at', FEB)
        return self.service.observe(account, amount, kind, **kwargs)

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

    def test_taxonomy_is_seeded(self):
        ids = {item.category_id for item in self.service.list_categories_for('alice')}
        self.assertIn('groceries', ids)
        self.assertIn('uncategorized', ids)
        self.assertIn('income', ids)

    def test_withdraw_defaults_to_cash(self):
        item = self.observe(kind='withdraw', description='atm')
        self.assertEqual(item.category_id, 'cash')
        self.assertEqual(item.direction, 'debit')

    def test_deposit_defaults_to_income(self):
        item = self.observe(kind='deposit', amount='250.00')
        self.assertEqual(item.category_id, 'income')
        self.assertEqual(item.direction, 'credit')

    def test_keyword_rule_beats_kind_default(self):
        item = self.observe(kind='withdraw', description='Walmart grocery run')
        self.assertEqual(item.category_id, 'groceries')

    def test_merchant_tag_beats_keyword(self):
        self.service.set_merchant_tag(
            actor='alice', actor_type='customer',
            merchant='walmart', category_id='shopping',
        )
        item = self.observe(kind='withdraw', description='walmart grocery', merchant='walmart')
        self.assertEqual(item.category_id, 'shopping')

    def test_explicit_category_beats_merchant(self):
        self.service.set_merchant_tag(
            actor='alice', actor_type='customer',
            merchant='walmart', category_id='shopping',
        )
        item = self.observe(
            kind='withdraw', description='walmart', merchant='walmart',
            category_id='groceries',
        )
        self.assertEqual(item.category_id, 'groceries')
        self.assertTrue(item.manual)

    def test_observe_is_idempotent_by_source(self):
        first = self.observe(amount='20.00', source_id='wd:1')
        second = self.observe(amount='20.00', source_id='wd:1', description='walmart')
        self.assertEqual(first.movement_id, second.movement_id)
        self.assertEqual(first.category_id, 'cash')
        self.assertEqual(len(self.service.store.list_movements(userid='alice')), 1)

    def test_customer_keyword_rule_beats_system_keyword(self):
        self.service.add_rule(
            actor='alice', actor_type='customer',
            keyword='walmart', category_id='shopping',
        )
        item = self.observe(kind='withdraw', description='walmart')
        self.assertEqual(item.category_id, 'shopping')

    def test_custom_category_and_recategorize(self):
        custom = self.service.create_category(
            actor='alice', actor_type='customer', label='Pets',
        )
        self.assertEqual(custom.kind, 'custom')
        item = self.observe(kind='withdraw', description='vet bill')
        updated = self.service.recategorize(
            actor='alice', actor_type='customer',
            movement_id=item.movement_id, category_id=custom.category_id,
            own_accounts=['1001'],
        )
        self.assertEqual(updated.category_id, custom.category_id)
        self.assertTrue(updated.manual)

    def test_custom_category_duplicate_and_limit(self):
        self.service.create_category(actor='alice', actor_type='customer', label='Pets')
        with self.assertRaises(CategoryError) as ctx:
            self.service.create_category(actor='alice', actor_type='customer', label='Pets')
        self.assertEqual(ctx.exception.code, 'category_duplicate')
        limited = CategoryService(
            CategoryPolicy(max_custom_categories=1), MemoryCategoryStore(), clock=self.clock,
        )
        limited.create_category(actor='alice', actor_type='customer', label='One')
        with self.assertRaises(CategoryError) as ctx:
            limited.create_category(actor='alice', actor_type='customer', label='Two')
        self.assertEqual(ctx.exception.code, 'category_limit')

    def test_customer_cannot_archive_system_category(self):
        with self.assertRaises(CategoryError) as ctx:
            self.service.archive_category(
                actor='alice', actor_type='customer', category_id='groceries',
            )
        self.assertEqual(ctx.exception.code, 'category_system')

    def test_budget_spent_warning_and_exceeded(self):
        self.service.set_budget(
            actor='alice', actor_type='customer',
            category_id='cash', amount='100.00',
        )
        self.observe(amount='80.00', kind='withdraw', description='atm')
        snap = self.service.snapshot('alice', period='2026-02')
        cash = next(row for row in snap['budgets'] if row['category_id'] == 'cash')
        self.assertEqual(cash['status'], 'warning')
        self.assertEqual(cash['spent'], '80.00')
        self.observe(amount='30.00', kind='withdraw', source_id='wd:2')
        snap = self.service.snapshot('alice', period='2026-02')
        cash = next(row for row in snap['budgets'] if row['category_id'] == 'cash')
        self.assertEqual(cash['status'], 'exceeded')
        self.assertEqual(cash['remaining'], '0.00')

    def test_credits_do_not_count_toward_budget(self):
        self.service.set_budget(
            actor='alice', actor_type='customer',
            category_id='income', amount='50.00',
        )
        self.observe(amount='200.00', kind='deposit')
        snap = self.service.snapshot('alice', period='2026-02')
        income = next(row for row in snap['budgets'] if row['category_id'] == 'income')
        self.assertEqual(income['spent'], '0.00')
        self.assertEqual(income['status'], 'ok')
        self.assertEqual(snap['totals']['credit'], '200.00')

    def test_clear_budget(self):
        self.service.set_budget(
            actor='alice', actor_type='customer',
            category_id='cash', amount='50.00',
        )
        self.service.clear_budget(
            actor='alice', actor_type='customer', category_id='cash',
        )
        self.assertEqual(self.service.snapshot('alice')['budgets'], [])

    def test_budget_out_of_range(self):
        with self.assertRaises(CategoryError) as ctx:
            self.service.set_budget(
                actor='alice', actor_type='customer',
                category_id='cash', amount='0.50',
            )
        self.assertEqual(ctx.exception.code, 'budget_out_of_range')

    def test_future_period_rejected(self):
        with self.assertRaises(CategoryError) as ctx:
            resolve_month('2026-04', now=MAR)
        self.assertEqual(ctx.exception.code, 'period_in_future')
        with self.assertRaises(PeriodError):
            resolve_month('2026-13', now=MAR)

    def test_customer_cannot_use_foreign_account(self):
        item = self.observe(account='1001')
        with self.assertRaises(CategoryError) as ctx:
            self.service.recategorize(
                actor='alice', actor_type='customer',
                movement_id=item.movement_id, category_id='groceries',
                own_accounts=['2002'],
            )
        self.assertEqual(ctx.exception.code, 'category_forbidden')

    def test_staff_can_set_budget_for_customer(self):
        item = self.service.set_budget(
            actor='t1', actor_type='tier1', customer_id='alice',
            category_id='dining', amount='75.00',
        )
        self.assertEqual(item.userid, 'alice')
        self.assertEqual(money_str(item.amount), '75.00')

    def test_staff_missing_customer_id(self):
        with self.assertRaises(CategoryError) as ctx:
            self.service.set_budget(
                actor='t1', actor_type='tier1',
                category_id='dining', amount='75.00',
            )
        self.assertEqual(ctx.exception.code, 'missing_customer_id')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCategoryStore(path)
            service = CategoryService(CategoryPolicy(), store, clock=self.clock)
            service.observe(
                '1001', '16.00', 'withdraw', userid='alice',
                description='netflix', posted_at=FEB, source_id='wd:sql',
            )
            service.set_budget(
                actor='alice', actor_type='customer',
                category_id='entertainment', amount='20.00',
            )
            again = CategoryService(CategoryPolicy(), SqliteCategoryStore(path), clock=self.clock)
            snap = again.snapshot('alice', period='2026-02')
            self.assertEqual(snap['totals']['debit'], '16.00')
            self.assertEqual(snap['movements'][0]['category_id'], 'entertainment')
            self.assertEqual(snap['budgets'][0]['status'], 'warning')
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass

    def test_month_id(self):
        self.assertEqual(month_id(MAR), '2026-03')


if __name__ == '__main__':
    unittest.main()
