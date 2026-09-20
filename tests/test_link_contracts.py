"""High-risk linked-ACH contracts missing from the original PR #68 tests.

IDOR, global trace idempotency, empty-account ownership bypass, HMAC-receipt
classification, amount/link limits, and staff force-verify of locked links.
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
    _classify_money_result,
    normalize_account,
    normalize_last4,
)


class LinkHelperContractTests(unittest.TestCase):
    def test_last4_keeps_only_the_trailing_digits(self):
        self.assertEqual(normalize_last4('021000021'), '0021')
        self.assertEqual(normalize_last4('xx7788yy'), '7788')

    def test_account_json_float_is_normalized(self):
        self.assertEqual(normalize_account(1001.0), '1001')
        self.assertEqual(normalize_account('1001.0'), '1001')

    def test_classify_treats_debit_strings_ok_and_receipt_dict_as_failed(self):
        self.assertEqual(_classify_money_result('Amount Debited'), 'ok')
        self.assertEqual(_classify_money_result('Success'), 'ok')
        self.assertEqual(_classify_money_result('Insufficient Balance'), 'nsf')
        receipt = generate_receipt(
            {'from_account': 10, 'to_account': 20, 'amount': 40.0, 'status': 'done'}
        )
        self.assertIsInstance(receipt, dict)
        self.assertIn('signature', receipt)
        self.assertEqual(_classify_money_result(receipt), 'failed')


class LinkIntegrityContractTests(unittest.TestCase):
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
            'bob': {
                'checkin': {'Account': 2001, 'Balance': 200},
                'savings': {'Account': 2002, 'Balance': 10},
                'credit': 'None',
            },
        }

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        policy = LinkPolicy(
            challenge_secret='unit-secret',
            pending_ttl_seconds=86400,
            prenote_wait_seconds=86400,
            max_attempts=3,
            max_resends=1,
            max_links=2,
            min_amount=Decimal('1.00'),
            max_amount=Decimal('100.00'),
        )
        self.service = LinkService(
            policy,
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

    def test_customer_cannot_confirm_or_move_someone_elses_link(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id, actor='bob', actor_type='customer',
                amount1='0.12', amount2='0.47',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')
        self._verify(link)
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='bob', actor='bob', actor_type='customer',
                link_id=link.link_id, amount='10.00', direction='push',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')
        self.assertEqual(self.debits, [])

    def test_trace_id_is_global_so_bob_can_replay_alices_movement(self):
        alice = self._verify(self._add())
        bob = self._verify(self._add(
            owner='bob', nickname='Ally', default='2001',
            routing_last4='0310', account_last4='1122',
        ), actor='bob')
        first, created = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=alice.link_id, amount='10.00', direction='push', trace_id='ach-shared',
        )
        self.assertTrue(created)
        replay, created_again = self.service.move(
            owner_userid='bob', actor='bob', actor_type='customer',
            link_id=bob.link_id, amount='40.00', direction='push', trace_id='ach-shared',
        )
        self.assertFalse(created_again)
        self.assertEqual(replay.movement_id, first.movement_id)
        self.assertEqual(replay.userid, 'alice')
        self.assertEqual(len(self.debits), 1)
        self.assertEqual(self.debits[0][0], '1001')

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        link = self._add(default='9999')
        self.assertEqual(link.default_account, '9999')
        credit_link = self._add(
            nickname='Visa', default='1003',
            routing_last4='4111', account_last4='0003',
        )
        self.assertEqual(credit_link.default_account, '1003')

    def test_hmac_receipt_from_debit_is_treated_as_failed_ach(self):
        link = self._verify(self._add())
        self.service.debit_fn = lambda account, amount, remark: generate_receipt({
            'from_account': account, 'amount': amount, 'status': 'done',
        })
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='10.00', direction='push', trace_id='rcpt-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        stored = self.service.store.get_movement_by_trace('rcpt-1')
        self.assertEqual(stored.status, 'failed')

    def test_push_return_credits_the_internal_account(self):
        link = self._verify(self._add())
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='15.00', direction='push', trace_id='out-1',
        )
        returned = self.service.return_movement(
            movement_id=movement.movement_id, actor='teller', actor_type='tier1',
            reason='unauthorized',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.credits[-1], ('1001', '15.00', 'ach returned from Chase Checking'))
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_push'], '0.00')
        self.assertEqual(snap['returned_ytd'], '15.00')

    def test_amount_and_link_limits(self):
        link = self._verify(self._add())
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='0.50', direction='push',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(LinkError) as ctx:
            self.service.move(
                owner_userid='alice', actor='alice', actor_type='customer',
                link_id=link.link_id, amount='100.01', direction='push',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        self._add(nickname='CapOne', routing_last4='0310', account_last4='1122')
        with self.assertRaises(LinkError) as ctx:
            self._add(nickname='Wells', routing_last4='1210', account_last4='9001')
        self.assertEqual(ctx.exception.code, 'link_limit')

    def test_resend_limit_then_staff_can_reset_locked_challenge(self):
        link = self._add()
        self.amounts[0] = (Decimal('0.03'), Decimal('0.88'))
        self.service.resend_challenge(
            link_id=link.link_id, actor='alice', actor_type='customer',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'resend_limit')
        for _ in range(3):
            try:
                self.service.confirm_micro(
                    link_id=link.link_id, actor='alice', actor_type='customer',
                    amount1='0.01', amount2='0.02',
                )
            except LinkError:
                pass
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'locked')
        with self.assertRaises(LinkError) as ctx:
            self.service.resend_challenge(
                link_id=link.link_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'link_locked')
        reset = self.service.resend_challenge(
            link_id=link.link_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(reset.status, 'pending')
        forced = self.service.force_verify(
            link_id=link.link_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(forced.status, 'verified')

    def test_staff_can_add_for_customer_customer_cannot_add_for_other(self):
        staff_link = self._add(actor='teller', actor_type='tier1', nickname='StaffAdded')
        self.assertEqual(staff_link.userid, 'alice')
        self.assertEqual(staff_link.actor, 'teller')
        with self.assertRaises(LinkError) as ctx:
            self._add(
                owner='bob', actor='alice', actor_type='customer',
                nickname='Stolen', default='2001',
                routing_last4='1110', account_last4='2222',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')

    def test_prenote_auto_accept_verifies_without_staff(self):
        self.service.policy.prenote_auto_accept = True
        link = self._add(method='prenote', nickname='Prenote')
        self.assertEqual(link.status, 'pending')
        self.now[0] += 86400
        changed = self.service.expire_due('alice')
        self.assertEqual(changed[0].status, 'verified')
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, 'verified')

    def test_pause_pending_fails_and_customer_cannot_settle(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor='alice', actor_type='customer', status='pause',
            )
        self.assertEqual(ctx.exception.code, 'link_not_verified')
        self._verify(link)
        movement, _ = self.service.move(
            owner_userid='alice', actor='alice', actor_type='customer',
            link_id=link.link_id, amount='10.00', direction='push', trace_id='settle-1',
        )
        with self.assertRaises(LinkError) as ctx:
            self.service.settle_movement(
                movement_id=movement.movement_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'link_forbidden')


if __name__ == '__main__':
    unittest.main()
