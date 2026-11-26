"""Customer resend of expired, and paused fingerprint occupancy.

Unique vs still-open PRs #213/#211/#207/#205 and master test_link_contracts:
those locked fingerprint occupancy of locked/expired, close of pending,
force_verify of locked/expired, resend of verified/prenote/closed, and
resend_limit plus staff resend of locked. They never locked that a customer
can self-unlock an expired micro by resending, or that a paused verified
link still occupies the routing+account last4.
"""
import unittest
from decimal import Decimal

from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkExpiredResendPauseTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.amounts = [(Decimal('0.12'), Decimal('0.47'))]

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
            debit_fn=lambda account, amount, remark: 'Amount Debited',
            credit_fn=lambda account, amount, remark: 'Success',
            accounts_fn=accounts_fn,
            amount_fn=lambda: self.amounts[0],
        )

    def _add(self, **kwargs):
        return self.service.add_link(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=kwargs.pop('nickname', 'Chase Checking'),
            default_account=kwargs.pop('default_account', '1001'),
            routing_last4=kwargs.pop('routing_last4', '0210'),
            account_last4=kwargs.pop('account_last4', '7788'),
            **kwargs,
        )

    def test_customer_resend_of_expired_reopens_pending(self):
        link = self._add()
        self.now[0] += 86400 + 1
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'link_expired')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'expired')

        with self.assertRaises(LinkError) as ctx:
            self._add(nickname='Chase Copy')
        self.assertEqual(ctx.exception.code, 'link_duplicate')

        self.amounts[0] = (Decimal('0.03'), Decimal('0.88'))
        resent = self.service.resend_challenge(
            link_id=link.link_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(resent.status, 'pending')
        self.assertEqual(resent.attempts, 0)

        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'amounts_incorrect')

        verified = self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.03', amount2='0.88',
        )
        self.assertEqual(verified.status, 'verified')

    def test_paused_verified_occupies_fingerprint_until_close(self):
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
            self._add(nickname='Chase Copy')
        self.assertEqual(ctx.exception.code, 'link_duplicate')

        closed = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='close',
        )
        self.assertEqual(closed.status, 'closed')
        relinked = self._add(nickname='Chase Copy')
        self.assertEqual(relinked.status, 'pending')
        self.assertEqual(relinked.account_last4, '7788')


if __name__ == '__main__':
    unittest.main()
