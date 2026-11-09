"""High-risk Fedwire contracts missing from the original PR #73 tests.

IDOR, global trace replay, HMAC-receipt classification, fee-NSF (principal
already sent), staff-only fee waiver, preview side effects, and observed
holiday clocks. Isolated Flask tests in test_wire_routes.py never hit these.
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
    compose_imad,
    normalize_purpose,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireHelperContractTests(unittest.TestCase):
    def test_classify_treats_debit_strings_ok_and_receipt_dict_as_failed(self):
        self.assertEqual(_classify_money_result('Amount Debited'), 'ok')
        self.assertEqual(_classify_money_result('done'), 'ok')
        self.assertEqual(_classify_money_result('Insufficient Balance'), 'nsf')
        receipt = generate_receipt(
            {'from_account': 10, 'to_account': 20, 'amount': 40.0, 'status': 'done'}
        )
        self.assertIsInstance(receipt, dict)
        self.assertIn('signature', receipt)
        self.assertEqual(_classify_money_result(receipt), 'failed')

    def test_purpose_aliases_and_rejects_unknown(self):
        self.assertEqual(normalize_purpose('salary'), 'payroll')
        self.assertEqual(normalize_purpose('mortgage'), 'loan')
        with self.assertRaises(WireError) as ctx:
            normalize_purpose('crypto')
        self.assertEqual(ctx.exception.code, 'invalid_purpose')

    def test_thanksgiving_and_saturday_july4_are_observed(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        thanksgiving = ts(2024, 11, 28, 11, 0)
        self.assertIn(datetime(2024, 11, 28).date(), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(thanksgiving).isoformat(), '2024-11-29')
        # 4 July 2020 was Saturday → observed Friday 3 July, then weekend.
        self.assertIn(datetime(2020, 7, 3).date(), us_fed_holidays(2020))
        friday_observed = ts(2020, 7, 3, 11, 0)
        self.assertEqual(calendar.value_date(friday_observed).isoformat(), '2020-07-06')


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

        policy = WirePolicy(
            outbound_fee=Decimal('25.00'),
            dual_control_threshold=Decimal('10000.00'),
            min_amount=Decimal('10.00'),
            max_amount=Decimal('5000.00'),
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
            legal_name=kwargs.pop('legal_name', 'Ada Lovelace'),
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default_account', '1001' if owner == 'alice' else '2001'),
        )

    def test_customer_cannot_add_or_originate_someone_elses_beneficiary(self):
        with self.assertRaises(WireError) as ctx:
            self._add(owner='bob', actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='bob', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='bob', actor='bob', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        self.assertEqual(self.debits, [])

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
        self.assertEqual(self.debits[0][0], '1001')

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default_account='9999')
        self.assertEqual(bene.default_account, '9999')
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00',
            internal_account='1003',
        )
        self.assertTrue(created)
        self.assertEqual(wire.internal_account, '1003')

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        bene = self._add()

        def receipt_debit(account, amount, remark):
            self.debits.append((account, amount, remark))
            return generate_receipt({
                'from_account': account, 'amount': amount, 'status': 'done',
            })

        self.service.debit_fn = receipt_debit
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='rcpt-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        stored = self.service.store.get_wire_by_trace('rcpt-1')
        self.assertEqual(stored.status, 'failed')
        self.assertEqual(stored.imad, '')
        self.assertEqual(len(self.debits), 1)

    def test_amount_bounds(self):
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
                beneficiary_id=bene.beneficiary_id, amount='5000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        self.assertEqual(self.debits, [])

    def test_fee_nsf_still_sends_principal(self):
        bene = self._add()
        calls = []

        def mixed(account, amount, remark):
            calls.append((account, amount, remark))
            self.debits.append((account, amount, remark))
            if 'fee' in (remark or ''):
                return 'Insufficient Balance'
            return 'Amount Debited'

        self.service.debit_fn = mixed
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='fee-nsf',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(wire.fee_status, 'nsf')
        self.assertTrue(wire.imad)
        self.assertEqual(calls[0][1], '40.00')
        self.assertEqual(calls[1][1], '25.00')

    def test_staff_only_fee_waiver_customer_preview_is_ignored(self):
        bene = self._add()
        preview = self.service.preview(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', waive_fee=True,
        )
        self.assertEqual(preview['fee'], '25.00')
        self.assertEqual(preview['total'], '65.00')
        self.assertEqual(self.debits, [])
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00',
            trace_id='no-waive', waive_fee=True,
        )
        self.assertEqual(wire.fee, '25.00')
        self.assertEqual(wire.fee_status, 'collected')
        self.now[0] = ts(2024, 6, 14, 18, 0)
        queued, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='q-waive',
        )
        self.assertEqual(queued.status, 'queued')
        with self.assertRaises(WireError) as ctx:
            self.service.waive_fee(
                wire_id=queued.wire_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        waived = self.service.waive_fee(
            wire_id=queued.wire_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(waived.fee_status, 'waived')
        self.assertEqual(waived.fee, '0.00')

    def test_same_day_imad_sequences_are_unique(self):
        bene = self._add()
        first, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='imad-1',
        )
        second, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='41.00', trace_id='imad-2',
        )
        self.assertNotEqual(first.imad, second.imad)
        self.assertEqual(first.imad, compose_imad('20240614', 'KONOHA01', 1))
        self.assertEqual(second.imad, compose_imad('20240614', 'KONOHA01', 2))

    def test_recall_credits_principal_and_collected_fee(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='60.00', trace_id='recall-fee',
        )
        self.assertEqual(wire.fee_status, 'collected')
        recalled = self.service.recall_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0], ('1001', '60.00', 'wire recalled from Chase Checking'))
        self.assertEqual(self.credits[1][0], '1001')
        self.assertEqual(self.credits[1][1], '25.00')
        self.assertIn('wire fee recalled', self.credits[1][2])

    def test_hmac_receipt_from_credit_fails_recall_after_principal_moves(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='15.00', trace_id='recall-rcpt',
        )

        def receipt_credit(account, amount, remark):
            self.credits.append((account, amount, remark))
            return generate_receipt({
                'to_account': account, 'amount': amount, 'status': 'done',
            })

        self.service.credit_fn = receipt_credit
        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(
                wire_id=wire.wire_id, actor='teller', actor_type='tier2',
            )
        self.assertEqual(ctx.exception.code, 'recall_failed')
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, 'sent')
        self.assertEqual(len(self.credits), 1)


if __name__ == '__main__':
    unittest.main()
