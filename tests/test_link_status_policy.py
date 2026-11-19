"""Linked-ACH policy/status contracts still untested on master.

Unique vs open coverage PRs #199/#197/#195/#193/#191/#189: those lock
Fedwire gates and ACH GET pause/settle, not customer_manage/move,
allow_credit, max_movements, return HMAC, or status aliases.
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
    normalize_direction,
    normalize_method,
    normalize_return_reason,
)


class LinkAliasTests(unittest.TestCase):
    def test_method_direction_and_return_reason_aliases(self):
        self.assertEqual(normalize_method('microdeposit'), 'micro')
        self.assertEqual(normalize_method('zero'), 'prenote')
        with self.assertRaises(LinkError) as ctx:
            normalize_method('wire')
        self.assertEqual(ctx.exception.code, 'invalid_method')

        self.assertEqual(normalize_direction('send'), 'push')
        self.assertEqual(normalize_direction('inbound'), 'pull')
        with self.assertRaises(LinkError) as ctx:
            normalize_direction('wire')
        self.assertEqual(ctx.exception.code, 'invalid_direction')

        self.assertEqual(normalize_return_reason('r10'), 'unauthorized')
        self.assertEqual(normalize_return_reason('stop_payment'), 'stop')
        with self.assertRaises(LinkError) as ctx:
            normalize_return_reason('chargeback')
        self.assertEqual(ctx.exception.code, 'invalid_reason')


class LinkStatusPolicyTests(unittest.TestCase):
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

    def test_customer_manage_false_blocks_add_staff_can_still_add(self):
        self.service.policy.customer_manage = False
        with self.assertRaises(LinkError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'link_forbidden')
        staff = self._add(actor='teller', actor_type='tier1', nickname='StaffAdded')
        self.assertEqual(staff.userid, 'alice')
        self.assertEqual(staff.actor, 'teller')

    def test_customer_move_false_blocks_push_staff_can_still_move(self):
        link = self._verify(self._add())
        self.service.policy.customer_move = False
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='10.00', direction='push', trace_id='cust-1',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')
        self.assertEqual(self.debits, [])
        moved, created = self.service.move(
            owner_userid='alice', actor='teller', actor_type='tier1',
            link_id=link.link_id, amount='10.00', direction='push', trace_id='staff-1',
        )
        self.assertTrue(created)
        self.assertEqual(moved.status, 'sent')
        self.assertEqual(self.debits[-1][0], '1001')

    def test_allow_credit_lets_credit_account_originate(self):
        self.service.policy.allow_credit = True
        link = self._verify(self._add(
            nickname='Visa', default='1003',
            routing_last4='4111', account_last4='0003',
        ))
        movement, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='10.00', direction='push', trace_id='cc-1',
        )
        self.assertTrue(created)
        self.assertEqual(self.debits[-1], ('1003', '10.00', 'ach to Visa'))

    def test_max_movements_blocks_new_origination(self):
        link = self._verify(self._add())
        self.service.policy.max_movements = 1
        first, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='10.00', direction='push', trace_id='cap-1',
        )
        self.assertTrue(created)
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='11.00', direction='push', trace_id='cap-2',
            )
        self.assertEqual(ctx.exception.code, 'movement_limit')
        self.assertEqual(len(self.debits), 1)

    def test_status_aliases_and_already_active(self):
        link = self._verify(self._add())
        held = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='hold',
        )
        self.assertEqual(held.status, 'paused')
        resumed = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='unpause',
        )
        self.assertEqual(resumed.status, 'verified')
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor='alice', actor_type='customer', status='activate',
            )
        self.assertEqual(ctx.exception.code, 'already_active')
        archived = self.service.set_link_status(
            link_id=link.link_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(archived.status, 'closed')

    def test_terminal_states_reject_confirm_and_second_return(self):
        link = self._verify(self._add())
        with self.assertRaises(LinkError) as ctx:
            self._verify(link)
        self.assertEqual(ctx.exception.code, 'already_verified')
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='15.00', direction='push', trace_id='ret-1',
        )
        returned = self.service.return_movement(
            movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            reason='r10',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(returned.reason, 'unauthorized')
        with self.assertRaises(LinkError) as ctx:
            self.service.return_movement(
                movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_returned')

    def test_hmac_receipt_from_credit_fails_return_after_debit(self):
        link = self._verify(self._add())
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='15.00', direction='push', trace_id='hmac-ret',
        )
        self.assertEqual(len(self.debits), 1)
        self.service.credit_fn = lambda account, amount, remark: generate_receipt({
            'to_account': account, 'amount': amount, 'status': 'done',
        })
        with self.assertRaises(LinkError) as ctx:
            self.service.return_movement(
                movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'return_failed')
        stored = self.service.store.get_movement(movement.movement_id)
        self.assertEqual(stored.status, 'sent')
        self.assertEqual(len(self.debits), 1)

    def test_prenote_without_auto_accept_stays_pending_after_expire_due(self):
        self.service.policy.prenote_auto_accept = False
        link = self._add(method='zero', nickname='Prenote', routing_last4='1210', account_last4='9001')
        self.assertEqual(link.method, 'prenote')
        self.assertEqual(link.status, 'pending')
        self.now[0] += 86400 + 1
        changed = self.service.expire_due('alice')
        self.assertEqual(changed, [])
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'pending')
