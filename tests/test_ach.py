import os
import tempfile
import unittest
from decimal import Decimal

from utility.ach import (
    AchError,
    AchPolicy,
    AchService,
    AmountError,
    MemoryAchStore,
    SqliteAchStore,
    compute_splits,
    money_str,
    parse_money,
)


class AllocationEngineTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_percent_split_sums_to_amount(self):
        splits = compute_splits('100.00', [
            {'account': '1001', 'kind': 'percent', 'value': '40'},
            {'account': '1002', 'kind': 'remainder'},
        ])
        self.assertEqual(splits[0], {'account': '1001', 'amount': '40.00', 'kind': 'percent'})
        self.assertEqual(splits[1], {'account': '1002', 'amount': '60.00', 'kind': 'remainder'})
        total = sum(Decimal(row['amount']) for row in splits)
        self.assertEqual(total, Decimal('100.00'))

    def test_fixed_then_percent_then_remainder(self):
        splits = compute_splits('1000.00', [
            {'account': '1003', 'kind': 'fixed', 'value': '200'},
            {'account': '1002', 'kind': 'percent', 'value': '10'},
            {'account': '1001', 'kind': 'remainder'},
        ])
        by_acct = {row['account']: row['amount'] for row in splits}
        self.assertEqual(by_acct['1003'], '200.00')
        self.assertEqual(by_acct['1002'], '100.00')
        self.assertEqual(by_acct['1001'], '700.00')

    def test_odd_cents_go_to_remainder(self):
        splits = compute_splits('10.00', [
            {'account': '1001', 'kind': 'percent', 'value': '33.33'},
            {'account': '1002', 'kind': 'remainder'},
        ])
        self.assertEqual(splits[0]['amount'], '3.33')
        self.assertEqual(splits[1]['amount'], '6.67')
        self.assertEqual(sum(Decimal(row['amount']) for row in splits), Decimal('10.00'))

    def test_hundred_percent_absorbs_rounding(self):
        splits = compute_splits('10.00', [
            {'account': '1001', 'kind': 'percent', 'value': '33.33'},
            {'account': '1002', 'kind': 'percent', 'value': '66.67'},
        ])
        self.assertEqual(sum(Decimal(row['amount']) for row in splits), Decimal('10.00'))

    def test_incomplete_without_remainder(self):
        with self.assertRaises(AchError) as ctx:
            compute_splits('100', [
                {'account': '1001', 'kind': 'percent', 'value': '40'},
            ])
        self.assertEqual(ctx.exception.code, 'allocation_incomplete')

    def test_percent_over_100(self):
        with self.assertRaises(AchError) as ctx:
            compute_splits('100', [
                {'account': '1001', 'kind': 'percent', 'value': '60'},
                {'account': '1002', 'kind': 'percent', 'value': '50'},
            ])
        self.assertEqual(ctx.exception.code, 'percent_over_100')

    def test_duplicate_account_rejected(self):
        with self.assertRaises(AchError) as ctx:
            compute_splits('100', [
                {'account': '1001', 'kind': 'percent', 'value': '40'},
                {'account': '1001', 'kind': 'remainder'},
            ])
        self.assertEqual(ctx.exception.code, 'duplicate_account')


class AchServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.credits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def accounts_fn(userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        self.service = AchService(
            AchPolicy(),
            MemoryAchStore(),
            clock=lambda: self.now[0],
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
        )

    def _add(self, nickname='Payroll', default='1001', **kwargs):
        return self.service.add_source(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            default_account=default,
            **kwargs,
        )

    def test_default_plan_credits_one_account(self):
        source = self._add(company_id='ACME01')
        inbound, created = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='250.00', source_id=source.source_id, trace_id='trace-1',
        )
        self.assertTrue(created)
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(inbound.splits, [
            {'account': '1001', 'amount': '250.00', 'kind': 'remainder'},
        ])
        self.assertEqual(self.credits, [('1001', '250.00', 'payroll from Payroll')])

    def test_paycheck_split_and_idempotent_trace(self):
        source = self._add(company_id='ACME01')
        self.service.set_allocation(
            source_id=source.source_id, actor='alice', actor_type='customer',
            legs=[
                {'account': '1002', 'kind': 'percent', 'value': '20'},
                {'account': '1001', 'kind': 'remainder'},
            ],
        )
        inbound, created = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='1000.00', company_id='ACME01', trace_id='pay-0415',
        )
        self.assertTrue(created)
        self.assertEqual(
            {row['account']: row['amount'] for row in inbound.splits},
            {'1002': '200.00', '1001': '800.00'},
        )
        again, created_again = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='1000.00', company_id='ACME01', trace_id='pay-0415',
        )
        self.assertFalse(created_again)
        self.assertEqual(again.inbound_id, inbound.inbound_id)
        self.assertEqual(len(self.credits), 2)

    def test_customer_cannot_post(self):
        source = self._add()
        with self.assertRaises(AchError) as ctx:
            self.service.post_inbound(
                owner_userid='alice', actor='alice', actor_type='customer',
                amount='10', source_id=source.source_id,
            )
        self.assertEqual(ctx.exception.code, 'ach_forbidden')

    def test_pause_blocks_post_unless_forced(self):
        source = self._add()
        self.service.set_status(
            source_id=source.source_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(AchError) as ctx:
            self.service.post_inbound(
                owner_userid='alice', actor='teller', actor_type='tier1',
                amount='10', source_id=source.source_id,
            )
        self.assertEqual(ctx.exception.code, 'source_paused')
        inbound, created = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='10', source_id=source.source_id, force=True, trace_id='forced',
        )
        self.assertTrue(created)
        self.assertEqual(inbound.status, 'posted')

    def test_duplicate_nickname(self):
        self._add()
        with self.assertRaises(AchError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'source_duplicate')

    def test_foreign_account_rejected(self):
        with self.assertRaises(AchError) as ctx:
            self._add(default='9999')
        self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_hundred_percent_to_default_account(self):
        source = self._add()
        updated = self.service.set_allocation(
            source_id=source.source_id, actor='alice', actor_type='customer',
            legs=[{'account': '1001', 'kind': 'percent', 'value': '100'}],
        )
        self.assertEqual(len(updated.legs), 1)
        inbound, _ = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='75.00', source_id=source.source_id, trace_id='all-checking',
        )
        self.assertEqual(inbound.splits, [
            {'account': '1001', 'amount': '75.00', 'kind': 'percent'},
        ])

    def test_preview_matches_post(self):
        source = self._add()
        self.service.set_allocation(
            source_id=source.source_id, actor='alice', actor_type='customer',
            legs=[{'account': '1002', 'kind': 'percent', 'value': '15'}],
        )
        preview = self.service.preview('200.00', source=self.service.store.get_source(source.source_id))
        inbound, _ = self.service.post_inbound(
            owner_userid='alice', actor='teller', actor_type='tier1',
            amount='200.00', source_id=source.source_id, trace_id='p1',
        )
        self.assertEqual(preview, inbound.splits)

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteAchStore(path)
            service = AchService(
                AchPolicy(), store, clock=lambda: self.now[0],
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 0},
                    'savings': 'None',
                    'credit': 'None',
                },
            )
            source = service.add_source(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Work', default_account='1001', company_id='ZZ9',
            )
            inbound, _ = service.post_inbound(
                owner_userid='alice', actor='teller', actor_type='tier1',
                amount='12.00', source_id=source.source_id, trace_id='sql-1',
            )
            reloaded = SqliteAchStore(path)
            found = reloaded.get_source(source.source_id)
            self.assertIsNotNone(found)
            self.assertEqual(found.nickname, 'Work')
            posted = reloaded.get_inbound_by_trace('sql-1')
            self.assertEqual(posted.inbound_id, inbound.inbound_id)
            self.assertEqual(posted.amount, '12.00')
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
