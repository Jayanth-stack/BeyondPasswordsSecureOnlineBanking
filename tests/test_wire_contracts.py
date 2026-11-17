"""High-risk Fedwire contracts missing from the original PR #73 tests.

IDOR, global trace idempotency, HMAC-receipt classification, empty-account
ownership bypass, staff-only fee waiver, OFAC+dual-control ordering, and
recall of a collected fee.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.crypto_receipt import generate_receipt
from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    _classify_money_result,
)


ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireHelperContractTests(unittest.TestCase):
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


class WireIntegrityContractTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
        self.debits = []
        self.credits = []
        self.accounts = {
            'alice': {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            },
            'bob': {
                'checkin': {'Account': 2001, 'Balance': 2000},
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

        self.service = WireService(
            WirePolicy(
                outbound_fee=Decimal('25.00'),
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: self.accounts.get(userid, {}),
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, owner='alice', actor=None, actor_type='customer', **kwargs):
        return self.service.add_beneficiary(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop('nickname', 'Chase Checking'),
            legal_name=kwargs.pop('legal_name', 'Ada Lovelace'),
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default_account', '1001' if owner == 'alice' else '2001'),
        )

    def test_customer_cannot_send_or_cancel_someone_elses_wire(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='bob', actor='bob', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        self.assertEqual(self.debits, [])

        self.now[0] = ts(2024, 6, 14, 18, 0)
        queued, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='q-alice',
        )
        self.assertEqual(queued.status, 'queued')
        with self.assertRaises(WireError) as ctx:
            self.service.get_wire(wire_id=queued.wire_id, actor='bob', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        with self.assertRaises(WireError) as ctx:
            self.service.cancel_wire(wire_id=queued.wire_id, actor='bob', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        stored = self.service.store.get_wire(queued.wire_id)
        self.assertEqual(stored.status, 'queued')

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice = self._add()
        bob = self._add(
            owner='bob', nickname='Ally', account_number='11223344',
            default_account='2001',
        )
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=alice.beneficiary_id, amount='40.00', trace_id='wire-shared',
        )
        self.assertTrue(created)
        replay, created_again = self.service.originate(
            owner_userid='bob', actor='bob', actor_type='customer',
            beneficiary_id=bob.beneficiary_id, amount='90.00', trace_id='wire-shared',
        )
        self.assertFalse(created_again)
        self.assertEqual(replay.wire_id, first.wire_id)
        self.assertEqual(replay.userid, 'alice')
        self.assertEqual(replay.amount, '40.00')
        self.assertEqual(len([row for row in self.debits if row[1] == '40.00']), 1)
        self.assertFalse(any(row[0] == '2001' for row in self.debits))

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default_account='9999')
        self.assertEqual(bene.default_account, '9999')
        credit_bene = self._add(
            nickname='Visa', account_number='41111111', default_account='1003',
        )
        self.assertEqual(credit_bene.default_account, '1003')

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        bene = self._add()
        self.service.debit_fn = lambda account, amount, remark: generate_receipt({
            'from_account': account, 'amount': amount, 'status': 'done',
        })
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='rcpt-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        stored = self.service.store.get_wire_by_trace('rcpt-1')
        self.assertEqual(stored.status, 'failed')
        self.assertEqual(stored.imad, '')

    def test_customer_waive_flag_is_ignored_staff_waive_skips_fee_debit(self):
        bene = self._add()
        customer_wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00',
            trace_id='cust-waive', waive_fee=True,
        )
        self.assertEqual(customer_wire.fee, '25.00')
        self.assertEqual(customer_wire.fee_status, 'collected')
        self.assertEqual(
            [row[1] for row in self.debits],
            ['40.00', '25.00'],
        )

        self.debits.clear()
        staff_wire, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='40.00',
            trace_id='staff-waive', waive_fee=True,
        )
        self.assertEqual(staff_wire.fee, '0.00')
        self.assertEqual(staff_wire.fee_status, 'waived')
        self.assertEqual([row[1] for row in self.debits], ['40.00'])

    def test_ofac_hold_on_high_value_still_requires_dual_control(self):
        bene = self._add(nickname='Blocked', legal_name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='ofac-hv',
        )
        self.assertEqual(wire.status, 'held')
        self.assertTrue(wire.ofac_hit)
        self.assertEqual(self.debits, [])
        overridden = self.service.override_ofac(
            wire_id=wire.wire_id, actor='maker', actor_type='tier1', note='cleared',
        )
        self.assertEqual(overridden.status, 'pending_release')
        self.assertEqual(self.debits, [])
        released = self.service.release_wire(
            wire_id=wire.wire_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_fee_nsf_keeps_principal_wire_sent(self):
        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            if 'fee' in remark:
                return 'Insufficient Balance'
            return 'Amount Debited'

        self.service.debit_fn = debit_fn
        bene = self._add()
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='fee-nsf',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(wire.fee_status, 'nsf')
        self.assertTrue(wire.imad)
        self.assertEqual(self.debits[0][1], '40.00')
        self.assertEqual(self.debits[1][1], '25.00')

    def test_recall_credits_collected_fee(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='60.00', trace_id='fee-recall',
        )
        self.assertEqual(wire.fee_status, 'collected')
        recalled = self.service.recall_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '60.00')
        self.assertEqual(self.credits[1][1], '25.00')
        self.assertIn('fee recalled', self.credits[1][2])

    def test_staff_can_add_for_customer_customer_cannot_add_for_other(self):
        staff_bene = self._add(actor='teller', actor_type='tier1', nickname='StaffAdded')
        self.assertEqual(staff_bene.userid, 'alice')
        self.assertEqual(staff_bene.actor, 'teller')
        with self.assertRaises(WireError) as ctx:
            self._add(
                owner='bob', actor='alice', actor_type='customer',
                nickname='Stolen', default_account='2001', account_number='99990001',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')

    def test_queued_fee_can_be_waived_before_transmit(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33.00', trace_id='q-fee',
        )
        self.assertEqual(wire.status, 'queued')
        waived = self.service.waive_fee(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(waived.fee, '0.00')
        self.assertEqual(waived.fee_status, 'waived')
        with self.assertRaises(WireError) as ctx:
            self.service.waive_fee(
                wire_id=wire.wire_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual([row[1] for row in self.debits], ['33.00'])


if __name__ == '__main__':
    unittest.main()
