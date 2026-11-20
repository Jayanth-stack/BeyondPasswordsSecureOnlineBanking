"""Linked-ACH force-verify / kill-switch contracts still untested on master.

Unique vs open coverage PRs #201/#199/#197/#195/#193/#191/#189/#174:
those lock snapshot expire, pending GET force-verify IDOR, pause/settle HTTP,
and customer_manage/move — not link_disabled, expired/prenote-wait/paused
force-verify bypasses, rejected→already_closed, pull-return HMAC, or NSF settle.
"""
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.crypto_receipt import generate_receipt
from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkForceVerifyGateTests(unittest.TestCase):
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
                max_movements=80,
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

    def test_disabled_blocks_customer_and_staff(self):
        self.service.policy.enabled = False
        with self.assertRaises(LinkError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'link_disabled')
        with self.assertRaises(LinkError) as ctx:
            self._add(actor='teller', actor_type='tier1', nickname='StaffAdded')
        self.assertEqual(ctx.exception.code, 'link_disabled')

    def test_force_verify_resurrects_expired_micro(self):
        link = self._add()
        self.now[0] += 86400 + 1
        with self.assertRaises(LinkError) as ctx:
            self._verify(link)
        self.assertEqual(ctx.exception.code, 'link_expired')
        forced = self.service.force_verify(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(forced.status, 'verified')
        movement, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='10.00', direction='push', trace_id='exp-1',
        )
        self.assertTrue(created)
        self.assertEqual(movement.status, 'sent')
        self.assertEqual(self.debits[-1][0], '1001')

    def test_force_verify_skips_prenote_wait(self):
        link = self._add(
            method='prenote', nickname='Prenote', routing_last4='1210', account_last4='9001',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'too_soon')
        forced = self.service.force_verify(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(forced.status, 'verified')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'verified')

    def test_force_verify_unpauses_without_resume(self):
        link = self._verify(self._add())
        paused = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor='alice', actor_type='customer', status='pause',
            )
        self.assertEqual(ctx.exception.code, 'already_paused')
        forced = self.service.force_verify(
            link_id=link.link_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(forced.status, 'verified')

    def test_force_verify_of_rejected_prenote_is_already_closed(self):
        link = self._add(
            method='prenote', nickname='Prenote', routing_last4='1210', account_last4='9001',
        )
        rejected = self.service.reject_prenote(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(rejected.status, 'rejected')
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_closed')
        with self.assertRaises(LinkError) as ctx:
            self.service.enforce_link(
                link_id=link.link_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_rejected')

    def test_hmac_receipt_from_debit_fails_pull_return_after_credit(self):
        link = self._verify(self._add())
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='20.00', direction='pull', trace_id='hmac-pull',
        )
        self.assertEqual(self.credits[-1], ('1001', '20.00', 'ach from Chase Checking'))
        self.service.debit_fn = lambda account, amount, remark: generate_receipt({
            'from_account': account, 'amount': amount, 'status': 'done',
        })
        with self.assertRaises(LinkError) as ctx:
            self.service.return_movement(
                movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'return_failed')
        stored = self.service.store.get_movement(movement.movement_id)
        self.assertEqual(stored.status, 'sent')
        self.assertEqual(len(self.credits), 1)

    def test_nsf_settle_is_not_returnable_second_settle_is_already_settled(self):
        link = self._verify(self._add())
        self.service.debit_fn = lambda account, amount, remark: 'Insufficient Balance'
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='10.00', direction='push', trace_id='nsf-settle',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        nsf = self.service.store.get_movement_by_trace('nsf-settle')
        with self.assertRaises(LinkError) as ctx:
            self.service.settle_movement(
                movement_id=nsf.movement_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'not_returnable')

        self.service.debit_fn = lambda account, amount, remark: (
            self.debits.append((account, amount, remark)) or 'Amount Debited'
        )
        sent, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='11.00', direction='push', trace_id='ok-settle',
        )
        settled = self.service.settle_movement(
            movement_id=sent.movement_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(settled.status, 'settled')
        with self.assertRaises(LinkError) as ctx:
            self.service.settle_movement(
                movement_id=sent.movement_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_settled')


if __name__ == '__main__':
    unittest.main()
