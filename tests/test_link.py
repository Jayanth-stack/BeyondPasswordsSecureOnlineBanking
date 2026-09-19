import os
import tempfile
import unittest
from decimal import Decimal

from utility.link import (
    AmountError,
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
    SqliteLinkStore,
    amounts_match,
    canonical_challenge_pair,
    challenge_digest,
    compose_micro_deposits,
    money_str,
    parse_money,
)


class ChallengeEngineTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_compose_micro_deposits_are_distinct_cents(self):
        seq = iter([12, 12, 12, 47])

        def rng(low, high):
            return next(seq)

        first, second = compose_micro_deposits(rng=rng)
        self.assertEqual((first, second), (Decimal('0.12'), Decimal('0.47')))

    def test_compose_forces_distinct_when_rng_stuck(self):
        first, second = compose_micro_deposits(rng=lambda low, high: 10)
        self.assertNotEqual(first, second)
        self.assertEqual(first, Decimal('0.10'))

    def test_digest_is_order_independent(self):
        secret = 'unit-secret'
        a = Decimal('0.12')
        b = Decimal('0.47')
        left = challenge_digest('abc', a, b, secret)
        right = challenge_digest('abc', b, a, secret)
        self.assertEqual(left, right)
        self.assertTrue(amounts_match(left, 'abc', b, a, secret))
        self.assertFalse(amounts_match(left, 'abc', a, Decimal('0.13'), secret))

    def test_canonical_pair_rejects_equal_amounts(self):
        with self.assertRaises(LinkError) as ctx:
            canonical_challenge_pair(Decimal('0.10'), Decimal('0.10'))
        self.assertEqual(ctx.exception.code, 'invalid_amount')


class LinkServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.debits = []
        self.credits = []
        self.amounts = [(Decimal('0.12'), Decimal('0.47'))]

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def accounts_fn(userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 500},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = LinkPolicy(
            challenge_secret='unit-secret',
            pending_ttl_seconds=86400,
            prenote_wait_seconds=86400,
            max_attempts=3,
            max_resends=2,
        )
        self.service = LinkService(
            policy,
            MemoryLinkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            amount_fn=lambda: self.amounts[0],
        )

    def _add(self, nickname='Chase Checking', default='1001', **kwargs):
        return self.service.add_link(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            default_account=default,
            routing_last4=kwargs.pop('routing_last4', '0210'),
            account_last4=kwargs.pop('account_last4', '7788'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_digest_or_amounts(self):
        link = self._add()
        payload = link.to_dict()
        self.assertNotIn('challenge_digest', payload)
        self.assertNotIn('amount1', payload)
        self.assertNotIn('deposit1', payload)
        snap = self.service.snapshot('alice')
        self.assertNotIn('challenge_digest', snap['links'][0])
        self.assertTrue(link.challenge_digest)

    def test_confirm_is_order_independent_and_enables_push(self):
        link = self._add()
        verified = self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.47', amount2='0.12',
        )
        self.assertEqual(verified.status, 'verified')
        movement, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='75.00', direction='push', trace_id='ach-1',
        )
        self.assertTrue(created)
        self.assertEqual(movement.status, 'sent')
        self.assertEqual(self.debits, [('1001', '75.00', 'ach to Chase Checking')])
        again, created_again = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='75.00', direction='push', trace_id='ach-1',
        )
        self.assertFalse(created_again)
        self.assertEqual(again.movement_id, movement.movement_id)
        self.assertEqual(len(self.debits), 1)

    def test_wrong_amounts_lock_after_max_attempts(self):
        link = self._add()
        for _ in range(2):
            with self.assertRaises(LinkError) as ctx:
                self.service.confirm_micro(
                    link_id=link.link_id, actor='alice', actor_type='customer',
                    amount1='0.01', amount2='0.02',
                )
            self.assertEqual(ctx.exception.code, 'amounts_incorrect')
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.01', amount2='0.02',
            )
        self.assertEqual(ctx.exception.code, 'link_locked')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'locked')

    def test_unverified_cannot_move(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='10.00', direction='pull',
            )
        self.assertEqual(ctx.exception.code, 'link_not_verified')
        self.assertEqual(self.credits, [])

    def test_credit_account_cannot_originate(self):
        with self.assertRaises(LinkError) as ctx:
            self._add(default='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_foreign_account_rejected(self):
        with self.assertRaises(LinkError) as ctx:
            self._add(default='9999')
        self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_duplicate_nickname_and_fingerprint(self):
        self._add()
        with self.assertRaises(LinkError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'link_duplicate')
        with self.assertRaises(LinkError) as ctx:
            self._add(nickname='Other Bank', routing_last4='0210', account_last4='7788')
        self.assertEqual(ctx.exception.code, 'link_duplicate')

    def test_prenote_cannot_self_confirm_staff_wait_then_accept(self):
        link = self._add(method='prenote')
        self.assertEqual(link.challenge_digest, '')
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'prenote_pending')
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(link_id=link.link_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'too_soon')
        self.now[0] += 86400
        accepted = self.service.accept_prenote(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(accepted.status, 'verified')

    def test_customer_cannot_accept_prenote(self):
        link = self._add(method='prenote')
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(link_id=link.link_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'link_forbidden')

    def test_pending_micro_expires_and_blocks_confirm(self):
        link = self._add()
        self.now[0] += 86400 + 1
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'link_expired')

    def test_resend_issues_new_digest(self):
        link = self._add()
        old = link.challenge_digest
        self.amounts[0] = (Decimal('0.03'), Decimal('0.88'))
        resent = self.service.resend_challenge(
            link_id=link.link_id, actor='alice', actor_type='customer',
        )
        self.assertNotEqual(resent.challenge_digest, old)
        self.assertEqual(resent.resends, 1)
        verified = self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.03', amount2='0.88',
        )
        self.assertEqual(verified.status, 'verified')

    def test_nsf_push_does_not_count_ytd(self):
        link = self._add()
        self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.12', amount2='0.47',
        )

        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='40.00', direction='push', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_push'], '0.00')

    def test_pull_credits_and_staff_return_debits(self):
        link = self._add()
        self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.12', amount2='0.47',
        )
        movement, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='20.00', direction='pull', trace_id='in-1',
        )
        self.assertTrue(created)
        self.assertEqual(self.credits, [('1001', '20.00', 'ach from Chase Checking')])
        returned = self.service.return_movement(
            movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            reason='unauthorized',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[-1][0], '1001')
        self.assertEqual(self.debits[-1][1], '20.00')

    def test_settle_blocks_return(self):
        link = self._add()
        self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.12', amount2='0.47',
        )
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='15.00', direction='push', trace_id='s1',
        )
        settled = self.service.settle_movement(
            movement_id=movement.movement_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(settled.status, 'settled')
        with self.assertRaises(LinkError) as ctx:
            self.service.return_movement(
                movement_id=movement.movement_id, actor='teller', actor_type='tier2',
            )
        self.assertEqual(ctx.exception.code, 'already_settled')

    def test_pause_blocks_move_close_is_terminal(self):
        link = self._add()
        self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.12', amount2='0.47',
        )
        paused = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='5.00', direction='push',
            )
        self.assertEqual(ctx.exception.code, 'link_paused')
        closed = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='close',
        )
        self.assertEqual(closed.status, 'closed')
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_closed')

    def test_sqlite_roundtrip_preserves_digest(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteLinkStore(path)
            service = LinkService(
                LinkPolicy(challenge_secret='unit-secret'),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                amount_fn=lambda: (Decimal('0.12'), Decimal('0.47')),
            )
            link = service.add_link(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', default_account='1001',
                routing_last4='1110', account_last4='2222',
            )
            digest = link.challenge_digest
            reloaded = SqliteLinkStore(path).get_link(link.link_id)
            self.assertEqual(reloaded.challenge_digest, digest)
            self.assertNotIn('challenge_digest', reloaded.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
