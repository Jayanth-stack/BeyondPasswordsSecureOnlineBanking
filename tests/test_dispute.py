import os
import tempfile
import unittest
from decimal import Decimal

from utility.dispute import (
    AmountError,
    DisputeError,
    DisputePolicy,
    DisputeService,
    MemoryDisputeStore,
    SqliteDisputeStore,
    money_str,
    parse_money,
)


class Ledger:
    def __init__(self, balances=None):
        self.balances = {str(k): Decimal(str(v)) for k, v in (balances or {}).items()}
        self.credits = []
        self.debits = []

    def credit(self, account, amount, remark):
        value = Decimal(str(amount))
        self.balances[str(account)] = self.balances.get(str(account), Decimal('0')) + value
        self.credits.append((str(account), str(amount), remark))
        return True

    def debit(self, account, amount, remark):
        value = Decimal(str(amount))
        current = self.balances.get(str(account), Decimal('0'))
        if current < value:
            return False
        self.balances[str(account)] = current - value
        self.debits.append((str(account), str(amount), remark))
        return True


class DisputeServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_700_000_000.0]
        self.ledger = Ledger({'1001': '50.00'})
        self.service = DisputeService(
            DisputePolicy(),
            MemoryDisputeStore(),
            clock=lambda: self.now[0],
            credit_fn=self.ledger.credit,
            debit_fn=self.ledger.debit,
        )

    def _debit(self, source_id='wd:1001:40', amount='40.00', kind='withdraw'):
        return self.service.observe(
            '1001', amount, kind, direction='debit',
            userid='alice', description='atm', source_id=source_id,
        )

    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_observe_is_idempotent_by_source_id(self):
        first = self._debit()
        self.now[0] += 10
        second = self._debit()
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(len(self.service.store.list_movements('alice')), 1)

    def test_credits_are_not_disputable(self):
        credit = self.service.observe(
            '1001', '20', 'deposit', direction='credit',
            userid='alice', source_id='dep:1',
        )
        self.assertFalse(credit.disputable)
        with self.assertRaises(DisputeError) as ctx:
            self.service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id='dep:1',
            )
        self.assertEqual(ctx.exception.code, 'not_disputable')

    def test_open_investigate_provisional_uphold(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40', reason='unauthorized', evidence='stolen card',
        )
        self.assertEqual(opened.status, 'open')
        self.assertEqual(opened.claimed_amount, '40.00')

        investigating = self.service.investigate(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(investigating.status, 'investigating')

        credited = self.service.grant_provisional(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(credited.status, 'provisionally_credited')
        self.assertEqual(credited.credit_status, 'provisional')
        self.assertEqual(self.ledger.balances['1001'], Decimal('90.00'))
        self.assertEqual(len(self.ledger.credits), 1)

        upheld = self.service.decide(
            dispute_id=opened.dispute_id, actor='mgr', actor_type='tier2',
            decision='uphold', note='fraud confirmed',
        )
        self.assertEqual(upheld.status, 'upheld')
        self.assertEqual(upheld.credit_status, 'final')
        self.assertEqual(self.ledger.balances['1001'], Decimal('90.00'))
        self.assertEqual(self.ledger.debits, [])

    def test_deny_claws_back_provisional_credit(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        self.service.grant_provisional(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
        )
        denied = self.service.decide(
            dispute_id=opened.dispute_id, actor='mgr', actor_type='tier2',
            decision='deny',
        )
        self.assertEqual(denied.status, 'denied')
        self.assertEqual(denied.credit_status, 'clawed')
        self.assertEqual(self.ledger.balances['1001'], Decimal('50.00'))
        self.assertEqual(len(self.ledger.debits), 1)

    def test_deny_without_credit_does_not_move_money(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        denied = self.service.decide(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
            decision='deny',
        )
        self.assertEqual(denied.status, 'denied')
        self.assertEqual(denied.credit_status, 'none')
        self.assertEqual(self.ledger.credits, [])
        self.assertEqual(self.ledger.debits, [])

    def test_uphold_without_prior_credit_posts_final(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        upheld = self.service.decide(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
            decision='uphold',
        )
        self.assertEqual(upheld.status, 'upheld')
        self.assertEqual(upheld.credit_status, 'final')
        self.assertEqual(self.ledger.balances['1001'], Decimal('90.00'))

    def test_customer_can_withdraw_open_case(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        withdrawn = self.service.withdraw(
            dispute_id=opened.dispute_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(withdrawn.status, 'withdrawn')
        again = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        self.assertEqual(again.status, 'open')

    def test_cannot_withdraw_after_provisional(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        self.service.grant_provisional(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
        )
        with self.assertRaises(DisputeError) as ctx:
            self.service.withdraw(
                dispute_id=opened.dispute_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'credit_already_granted')

    def test_duplicate_source_rejected(self):
        self._debit()
        self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        with self.assertRaises(DisputeError) as ctx:
            self.service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id='wd:1001:40',
            )
        self.assertEqual(ctx.exception.code, 'dispute_duplicate')

    def test_window_expired(self):
        self._debit()
        self.now[0] += 91 * 24 * 3600
        with self.assertRaises(DisputeError) as ctx:
            self.service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id='wd:1001:40',
            )
        self.assertEqual(ctx.exception.code, 'window_expired')

    def test_claimed_amount_cannot_exceed_posted(self):
        self._debit()
        with self.assertRaises(DisputeError) as ctx:
            self.service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id='wd:1001:40', claimed_amount='50',
            )
        self.assertEqual(ctx.exception.code, 'credit_exceeds_dispute')

    def test_provisional_cannot_exceed_claim(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40', claimed_amount='10',
        )
        with self.assertRaises(DisputeError) as ctx:
            self.service.grant_provisional(
                dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
                amount='11',
            )
        self.assertEqual(ctx.exception.code, 'credit_exceeds_dispute')

    def test_customer_cannot_grant_or_decide(self):
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        with self.assertRaises(DisputeError) as ctx:
            self.service.grant_provisional(
                dispute_id=opened.dispute_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'dispute_forbidden')
        with self.assertRaises(DisputeError) as ctx:
            self.service.decide(
                dispute_id=opened.dispute_id, actor='alice', actor_type='customer',
                decision='uphold',
            )
        self.assertEqual(ctx.exception.code, 'dispute_forbidden')

    def test_clawback_failed_keeps_provisional(self):
        self.ledger.balances['1001'] = Decimal('0')
        self._debit()
        opened = self.service.open_dispute(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id='wd:1001:40',
        )
        self.service.grant_provisional(
            dispute_id=opened.dispute_id, actor='teller', actor_type='tier1',
        )
        self.ledger.balances['1001'] = Decimal('0')
        with self.assertRaises(DisputeError) as ctx:
            self.service.decide(
                dispute_id=opened.dispute_id, actor='mgr', actor_type='tier2',
                decision='deny',
            )
        self.assertEqual(ctx.exception.code, 'clawback_failed')
        stored = self.service.store.get_dispute(opened.dispute_id)
        self.assertEqual(stored.status, 'provisionally_credited')
        self.assertEqual(stored.credit_status, 'clawback_failed')
        self.ledger.balances['1001'] = Decimal('40.00')
        retried = self.service.retry_clawback(
            dispute_id=opened.dispute_id, actor='mgr', actor_type='tier2',
        )
        self.assertEqual(retried.credit_status, 'clawed')
        self.assertEqual(retried.status, 'denied')

    def test_open_limit(self):
        policy = DisputePolicy(max_open=1)
        service = DisputeService(policy, MemoryDisputeStore(), clock=lambda: self.now[0])
        service.observe('1001', '5', 'withdraw', direction='debit', userid='alice', source_id='a')
        service.observe('1001', '6', 'withdraw', direction='debit', userid='alice', source_id='b')
        service.open_dispute(owner_userid='alice', actor='alice', actor_type='customer', source_id='a')
        with self.assertRaises(DisputeError) as ctx:
            service.open_dispute(owner_userid='alice', actor='alice', actor_type='customer', source_id='b')
        self.assertEqual(ctx.exception.code, 'dispute_limit')

    def test_foreign_account_forbidden_when_own_set(self):
        self._debit()
        with self.assertRaises(DisputeError) as ctx:
            self.service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id='wd:1001:40', own_accounts=['9999'],
            )
        self.assertEqual(ctx.exception.code, 'dispute_forbidden')

    def test_observe_transfer_splits_legs(self):
        posted = self.service.observe_transfer(
            '1001', '2002', '12.50', from_userid='alice', to_userid='bob', source_id='xfer:1',
        )
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0].kind, 'transfer_out')
        self.assertTrue(posted[0].disputable)
        self.assertEqual(posted[1].kind, 'transfer_in')
        self.assertFalse(posted[1].disputable)

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteDisputeStore(path)
            service = DisputeService(
                DisputePolicy(), store, clock=lambda: self.now[0],
                credit_fn=self.ledger.credit, debit_fn=self.ledger.debit,
            )
            service.observe('1001', '15', 'withdraw', direction='debit', userid='alice', source_id='wd:sql')
            opened = service.open_dispute(
                owner_userid='alice', actor='alice', actor_type='customer', source_id='wd:sql',
            )
            other = DisputeService(DisputePolicy(), SqliteDisputeStore(path), clock=lambda: self.now[0])
            loaded = other.store.get_dispute(opened.dispute_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.amount, '15.00')
            snapshot = other.snapshot('alice')
            self.assertEqual(snapshot['open_count'], 1)
        finally:
            os.remove(path)
            for suffix in ('-wal', '-shm'):
                extra = path + suffix
                if os.path.exists(extra):
                    os.remove(extra)

    def test_snapshot_hides_expired_challengeable(self):
        self._debit()
        self.now[0] += 91 * 24 * 3600
        snapshot = self.service.snapshot('alice')
        self.assertEqual(snapshot['challengeable'], [])


if __name__ == '__main__':
    unittest.main()
