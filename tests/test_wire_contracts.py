"""High-risk Fedwire contracts missing from the original PR #73 tests.

IDOR, global trace idempotency, empty-account ownership bypass, HMAC-receipt
classification, amount/beneficiary limits, fee-NSF vs principal send, and
cutoff vs dual-control release.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import tempfile
import unittest

import tests  # noqa: F401

from utility.crypto_receipt import generate_receipt
from utility.wire import (
    AmountError,
    MemoryWireStore,
    SqliteWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    _classify_money_result,
    last4,
    normalize_aba,
    normalize_account,
    normalize_purpose,
    parse_money,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireHelperContractTests(unittest.TestCase):
    def test_last4_keeps_only_the_trailing_digits(self):
        self.assertEqual(last4('77881234'), '1234')
        self.assertEqual(last4('xx99'), '99')
        self.assertEqual(last4('0210000217788'), '7788')

    def test_account_json_float_is_normalized(self):
        self.assertEqual(normalize_account(1001.0), '1001')
        self.assertEqual(normalize_account('1001.0'), '1001')

    def test_ten_digit_aba_is_rejected(self):
        with self.assertRaises(WireError) as ctx:
            normalize_aba('0210000210')
        self.assertEqual(ctx.exception.code, 'invalid_aba')

    def test_parse_money_rejects_non_finite(self):
        with self.assertRaises(AmountError):
            parse_money('Infinity')
        with self.assertRaises(AmountError):
            parse_money('NaN')
        with self.assertRaises(AmountError):
            parse_money('-1')

    def test_purpose_aliases_and_unknown(self):
        self.assertEqual(normalize_purpose('salary'), 'payroll')
        self.assertEqual(normalize_purpose('gift'), 'family')
        with self.assertRaises(WireError) as ctx:
            normalize_purpose('crypto')
        self.assertEqual(ctx.exception.code, 'invalid_purpose')

    def test_classify_treats_debit_strings_ok_and_receipt_dict_as_failed(self):
        self.assertEqual(_classify_money_result('Amount Debited'), 'ok')
        self.assertEqual(_classify_money_result('Success'), 'ok')
        self.assertEqual(_classify_money_result('Insufficient Balance'), 'nsf')
        receipt = generate_receipt(
            {'from_account': 10, 'amount': 40.0, 'status': 'done'}
        )
        self.assertIsInstance(receipt, dict)
        self.assertIn('signature', receipt)
        self.assertEqual(_classify_money_result(receipt), 'failed')


class WireIntegrityContractTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []
        self.accounts = {
            'alice': {
                'checkin': {'Account': 1001, 'Balance': 5000},
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

        policy = WirePolicy(
            outbound_fee=Decimal('25.00'),
            dual_control_threshold=Decimal('10000.00'),
            min_amount=Decimal('10.00'),
            max_amount=Decimal('100.00'),
            max_beneficiaries=2,
            max_wires=3,
        )
        self.service = WireService(
            policy,
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
            legal_name=kwargs.pop('name', 'Ada Lovelace'),
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default', '1001'),
        )

    def test_customer_cannot_originate_or_cancel_someone_elses_wire(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='bob', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='bob', actor='bob', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        self.assertEqual(self.debits, [])

        self.now[0] = ts(2024, 6, 14, 18, 0)
        queued, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='q-idor',
        )
        with self.assertRaises(WireError) as ctx:
            self.service.cancel_wire(
                wire_id=queued.wire_id, actor='bob', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        stored = self.service.store.get_wire(queued.wire_id)
        self.assertEqual(stored.status, 'queued')

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice = self._add()
        bob = self._add(
            owner='bob', nickname='Ally', default='2001',
            account_number='11223344',
        )
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=alice.beneficiary_id, amount='20.00', trace_id='wire-shared',
        )
        self.assertTrue(created)
        replay, created_again = self.service.originate(
            owner_userid='bob', actor='bob', actor_type='customer',
            beneficiary_id=bob.beneficiary_id, amount='40.00', trace_id='wire-shared',
        )
        self.assertFalse(created_again)
        self.assertEqual(replay.wire_id, first.wire_id)
        self.assertEqual(replay.userid, 'alice')
        self.assertEqual(replay.amount, '20.00')
        principal = [row for row in self.debits if row[1] == '20.00']
        self.assertEqual(len(principal), 1)
        self.assertEqual(principal[0][0], '1001')
        self.assertFalse(any(row[0] == '2001' for row in self.debits))

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default='9999')
        self.assertEqual(bene.default_account, '9999')
        credit = self._add(
            nickname='Visa', default='1003', account_number='41110003',
        )
        self.assertEqual(credit.default_account, '1003')
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=credit.beneficiary_id, amount='20.00',
            internal_account='1003', trace_id='credit-ok',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(self.debits[0][0], '1003')

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        bene = self._add()
        self.service.debit_fn = lambda account, amount, remark: generate_receipt({
            'from_account': account, 'amount': amount, 'status': 'done',
        })
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='rcpt-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        stored = self.service.store.get_wire_by_trace('rcpt-1')
        self.assertEqual(stored.status, 'failed')
        self.assertEqual(stored.imad, '')

    def test_fee_nsf_still_sends_principal(self):
        bene = self._add()

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            if 'wire fee' in remark:
                return 'Insufficient Balance'
            return 'Amount Debited'

        self.service.debit_fn = debit_fn
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='fee-nsf',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(wire.fee_status, 'nsf')
        self.assertTrue(wire.imad)
        self.assertEqual(self.debits[0][1], '20.00')
        self.assertEqual(self.debits[1][1], '25.00')

    def test_customer_waive_is_ignored_staff_waive_skips_fee_debit(self):
        bene = self._add()
        customer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00',
            trace_id='no-waive', waive_fee=True,
        )
        self.assertEqual(customer.fee, '25.00')
        self.assertEqual(customer.fee_status, 'collected')
        fee_debits = [row for row in self.debits if row[1] == '25.00']
        self.assertEqual(len(fee_debits), 1)

        staff, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='20.00',
            trace_id='staff-waive', waive_fee=True,
        )
        self.assertEqual(staff.fee, '0.00')
        self.assertEqual(staff.fee_status, 'waived')
        self.assertEqual(len([row for row in self.debits if row[1] == '25.00']), 1)

    def test_recall_refunds_collected_fee(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='fee-back',
        )
        self.assertEqual(wire.fee_status, 'collected')
        recalled = self.service.recall_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0], ('1001', '20.00', 'wire recalled from Chase Checking'))
        self.assertEqual(self.credits[1][0], '1001')
        self.assertEqual(self.credits[1][1], '25.00')
        self.assertIn('wire fee recalled', self.credits[1][2])

    def test_amount_and_beneficiary_limits(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='9.99',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='100.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='SameBank', account_number='77881234')
        self.assertEqual(ctx.exception.code, 'beneficiary_duplicate')
        self._add(nickname='CapOne', account_number='11223344')
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='Wells', account_number='99887766')
        self.assertEqual(ctx.exception.code, 'beneficiary_limit')
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='Chase Checking', account_number='55667788')
        self.assertEqual(ctx.exception.code, 'beneficiary_duplicate')

    def test_unowned_internal_account_is_rejected(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='2001',
            )
        self.assertEqual(ctx.exception.code, 'invalid_account')
        self.assertEqual(self.debits, [])

    def test_ofac_override_of_high_value_goes_pending_not_sent(self):
        self.service.policy.max_amount = Decimal('100000.00')
        bene = self._add(nickname='Blocked', name='Blocked Person')
        held, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='ofac-hv',
        )
        self.assertEqual(held.status, 'held')
        self.assertEqual(self.debits, [])
        pending = self.service.override_ofac(
            wire_id=held.wire_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(pending.status, 'pending_release')
        self.assertEqual(self.debits, [])
        released = self.service.release_wire(
            wire_id=pending.wire_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_after_cutoff_release_transmits_pending_but_requeues_queued(self):
        self.service.policy.max_amount = Decimal('100000.00')
        bene = self._add()
        pending, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='pending-late',
        )
        self.assertEqual(pending.status, 'pending_release')
        self.now[0] = ts(2024, 6, 14, 18, 0)
        released = self.service.release_wire(
            wire_id=pending.wire_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'sent')
        self.assertTrue(released.imad.startswith('20240614'))

        queued, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='still-late',
        )
        self.assertEqual(queued.status, 'queued')
        still = self.service.release_wire(
            wire_id=queued.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(still.status, 'queued')
        principals = [row for row in self.debits if row[1] == '20.00']
        self.assertEqual(principals, [])

    def test_sqlite_trace_is_global_and_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteWireStore(path)
            service = WireService(
                WirePolicy(
                    outbound_fee=Decimal('0.00'),
                    min_amount=Decimal('10.00'),
                    max_amount=Decimal('100.00'),
                ),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {
                    'alice': self.accounts['alice'],
                    'bob': self.accounts['bob'],
                }.get(userid, {}),
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
            )
            alice = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', aba='021000021',
                account_number='99887766', street='1 Federal St', city='New York',
                state='NY', postal='10004', default_account='1001',
            )
            bob = service.add_beneficiary(
                owner_userid='bob', actor='bob', actor_type='customer',
                nickname='Ally', legal_name='Bob Builder', aba='021000021',
                account_number='11223344', street='2 Federal St', city='New York',
                state='NY', postal='10004', default_account='2001',
            )
            first, created = service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=alice.beneficiary_id, amount='20.00', trace_id='sql-shared',
            )
            self.assertTrue(created)
            replay, created_again = service.originate(
                owner_userid='bob', actor='bob', actor_type='customer',
                beneficiary_id=bob.beneficiary_id, amount='40.00', trace_id='sql-shared',
            )
            reloaded = SqliteWireStore(path).get_wire_by_trace('sql-shared')
            self.assertFalse(created_again)
            self.assertEqual(replay.wire_id, first.wire_id)
            self.assertEqual(reloaded.userid, 'alice')
            self.assertEqual(reloaded.amount, '20.00')
            self.assertNotIn('account_number', reloaded.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
