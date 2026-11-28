"""Locked micro cannot self-unlock via confirm, even with the correct amounts.

Unique vs still-open PRs #216/#213/#207 and master test_link / test_link_contracts:
those locked confirm of paused as invalid_status, close of locked freeing the
fingerprint, force_verify of locked unlocking, the third wrong attempt locking,
and staff resend of locked. They never locked that a subsequent confirm with
the original correct amounts stays link_locked for customer and staff (staff
must resend or force_verify), or that the locked fingerprint still occupies
OPEN_STATUSES so the same last4 is link_duplicate.
"""
import unittest
from decimal import Decimal

from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkLockedConfirmTests(unittest.TestCase):
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

    def _lock(self, link):
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

    def test_confirm_of_locked_with_correct_amounts_stays_locked(self):
        link = self._add()
        self._lock(link)

        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='alice', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'link_locked')

        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='teller', actor_type='tier1',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'link_locked')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'locked')

        with self.assertRaises(LinkError) as ctx:
            self._add(nickname='Other Bank', routing_last4='0210', account_last4='7788')
        self.assertEqual(ctx.exception.code, 'link_duplicate')


if __name__ == '__main__':
    unittest.main()
