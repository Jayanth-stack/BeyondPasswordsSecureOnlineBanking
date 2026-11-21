"""Linked-ACH resend / closed / method-mismatch contracts still untested on master.

Unique vs still-open coverage PRs #203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock force-verify of expired/prenote-wait/paused/rejected, snapshot GET
expire, pending GET force-verify IDOR, and customer_manage/move — not resend
of verified/prenote/closed, force-verify of already-verified/closed, or
accept/reject of a micro as if it were a prenote.
"""
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkResendClosedGateTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.debits = []
        self.credits = []
        self.amounts = [(Decimal('0.12'), Decimal('0.47'))]
        self.accounts = {
            'alice': {
                'checkin': {'Account': 1001, 'Balance': 500},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            },
        }

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = LinkService(
            LinkPolicy(
                challenge_secret='unit-secret',
                pending_ttl_seconds=86400,
                prenote_wait_seconds=86400,
                max_attempts=3,
                max_resends=1,
                max_links=8,
                min_amount=Decimal('1.00'),
                max_amount=Decimal('100.00'),
            ),
            MemoryLinkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: self.accounts.get(userid, {}),
            amount_fn=lambda: self.amounts[0],
        )

    def _add(self, owner='alice', actor=None, actor_type='customer', **kwargs):
        return self.service.add_link(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop('nickname', 'Chase Checking'),
            default_account=kwargs.pop('default', '1001'),
            routing_last4=kwargs.pop('routing_last4', '0210'),
            account_last4=kwargs.pop('account_last4', '7788'),
            **kwargs,
        )

    def _verify(self, link, actor='alice', actor_type='customer'):
        return self.service.confirm_micro(
            link_id=link.link_id,
            actor=actor,
            actor_type=actor_type,
            amount1='0.12',
            amount2='0.47',
        )

    def test_resend_of_verified_is_already_verified(self):
        link = self._verify(self._add())
        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'already_verified')

    def test_resend_of_prenote_is_prenote_pending(self):
        link = self._add(
            method='prenote', nickname='Prenote', routing_last4='1210', account_last4='9001',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'prenote_pending')

    def test_resend_of_closed_is_already_closed(self):
        link = self._verify(self._add())
        closed = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='close',
        )
        self.assertEqual(closed.status, 'closed')
        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'already_closed')

    def test_force_verify_of_verified_is_already_verified(self):
        link = self._verify(self._add())
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_verified')

    def test_force_verify_of_closed_is_already_closed(self):
        link = self._verify(self._add())
        self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='close',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_closed')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'closed')

    def test_accept_and_reject_prenote_of_micro_is_invalid_method(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'invalid_method')
        with self.assertRaises(LinkError) as ctx:
            self.service.reject_prenote(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'invalid_method')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'pending')

    def test_customer_cannot_reject_prenote(self):
        link = self._add(
            method='prenote', nickname='Prenote', routing_last4='1210', account_last4='9001',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.reject_prenote(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'pending')


if __name__ == '__main__':
    unittest.main()
