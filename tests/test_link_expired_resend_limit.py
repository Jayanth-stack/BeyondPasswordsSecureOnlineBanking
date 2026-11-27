"""Expired + exhausted resends cannot self-unlock; confirm of paused is invalid.

Unique vs still-open PRs #214/#213/#205 and master test_link_contracts:
those locked customer resend of expired with remaining quota, paused
fingerprint occupancy, customer resend_limit of a still-pending micro, and
staff resend of locked. They never locked that an expired micro whose
resend quota is already spent stays expired for the customer (staff can
still reopen), or that confirm_micro of a paused verified link is
invalid_status rather than already_verified.
"""
import unittest
from decimal import Decimal

from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkExpiredResendLimitTests(unittest.TestCase):
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
            max_resends=1,
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

    def test_customer_cannot_resend_expired_after_quota_staff_can(self):
        link = self._add()
        self.amounts[0] = (Decimal('0.03'), Decimal('0.88'))
        self.service.resend_challenge(
            link_id=link.link_id, actor='alice', actor_type='customer',
        )
        self.now[0] += 86400 + 1
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.03', amount2='0.88',
            )
        self.assertEqual(ctx.exception.code, 'link_expired')

        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'resend_limit')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'expired')

        self.amounts[0] = (Decimal('0.11'), Decimal('0.22'))
        resent = self.service.resend_challenge(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(resent.status, 'pending')
        verified = self.service.confirm_micro(
            link_id=link.link_id, actor='alice', actor_type='customer',
            amount1='0.11', amount2='0.22',
        )
        self.assertEqual(verified.status, 'verified')

    def test_confirm_of_paused_verified_is_invalid_status(self):
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
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'invalid_status')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'paused')


if __name__ == '__main__':
    unittest.main()
