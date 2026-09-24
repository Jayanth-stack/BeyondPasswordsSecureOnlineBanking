import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.sdd import (
    AmountError,
    EurUsdBook,
    MemorySddStore,
    SddError,
    SddPolicy,
    SddService,
    SqliteSddStore,
    Target2Calendar,
    compose_creditor_identifier,
    compose_pain008,
    compute_fee,
    easter_gregorian,
    iban_check_digit_ok,
    mask_iban,
    money_str,
    normalize_creditor_identifier,
    normalize_iban,
    parse_money,
    target2_holidays,
)

CEST = timezone(timedelta(hours=2))
DE_IBAN = 'DE89370400440532013000'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_iban_mod97_and_sepa_mask(self):
        self.assertTrue(iban_check_digit_ok(DE_IBAN))
        self.assertEqual(normalize_iban('de89 3704 0044 0532 0130 00'), DE_IBAN)
        self.assertEqual(mask_iban(DE_IBAN), 'DE****3000')
        with self.assertRaises(SddError) as ctx:
            normalize_iban('DE89370400440532013001')
        self.assertEqual(ctx.exception.code, 'invalid_iban')
        with self.assertRaises(SddError) as ctx:
            normalize_iban('US64SVBKUS6S3300958879')
        self.assertEqual(ctx.exception.code, 'not_sepa_country')

    def test_creditor_identifier_epc_check(self):
        ident = compose_creditor_identifier('DE', '09999999999', 'ZZZ')
        self.assertEqual(ident, 'DE98ZZZ09999999999')
        self.assertEqual(normalize_creditor_identifier(ident), ident)
        with self.assertRaises(SddError) as ctx:
            normalize_creditor_identifier('DE00ZZZ09999999999')
        self.assertEqual(ctx.exception.code, 'invalid_creditor_id')

    def test_pain008_is_direct_debit_not_credit(self):
        payload = compose_pain008(
            msg_id='SDD20240614000001',
            created=datetime(2024, 6, 14, 11, 0, tzinfo=CEST),
            collection_date='2024-06-14',
            amount_eur=Decimal('40.00'),
            sequence='FRST',
            scheme='core',
            creditor_name='ALICE',
            creditor_identifier='DE98ZZZ09999999999',
            debtor_name='ADA LOVELACE',
            debtor_iban=DE_IBAN,
            umr='UMR-1',
            signed_on='2024-06-01',
            end_to_end_id='E2E1',
            pmtinf_id='PINF1',
            instr_id='INSTR1',
            tx_id='TX1',
            purpose='rent',
        )
        self.assertEqual(payload['PmtInf']['PmtMtd'], 'DD')
        self.assertEqual(payload['PmtInf']['DrctDbtTxInf']['SeqTp'], 'FRST')
        self.assertEqual(payload['PmtInf']['DrctDbtTxInf']['LclInstrm'], 'CORE')
        self.assertEqual(payload['PmtInf']['DrctDbtTxInf']['InstdAmt']['Ccy'], 'EUR')

    def test_fee_core_b2b_or_waived(self):
        self.assertEqual(compute_fee('core', Decimal('8.00'), Decimal('12.00')), Decimal('8.00'))
        self.assertEqual(compute_fee('b2b', Decimal('8.00'), Decimal('12.00')), Decimal('12.00'))
        self.assertEqual(compute_fee('core', Decimal('8.00'), Decimal('12.00'), waived=True), Decimal('0.00'))

    def test_target2_weekend_easter_and_cutoff(self):
        calendar = Target2Calendar(cutoff_hour=16, tz_offset_hours=2)
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        self.assertIn(datetime(2024, 3, 29).date(), target2_holidays(2024))
        friday = ts(2024, 6, 14, 15, 0)
        after = ts(2024, 6, 14, 16, 30)
        self.assertEqual(calendar.value_date(friday).isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(after).isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(after)['after_cutoff'])
        good_friday = ts(2024, 3, 29, 10, 0)
        self.assertEqual(calendar.value_date(good_friday).isoformat(), '2024-04-02')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday).isoformat(), '2024-06-17')
        self.assertEqual(calendar.earliest_collection_date(friday, 5).isoformat(), '2024-06-21')

    def test_eurusd_quote(self):
        book = EurUsdBook(rate=Decimal('1.08'))
        self.assertEqual(book.quote(Decimal('100.00')), Decimal('108.00'))


class SddServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def accounts_fn(userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = SddPolicy(
            core_fee=Decimal('8.00'),
            b2b_fee=Decimal('12.00'),
            dual_control_threshold=Decimal('10000.00'),
            core_first_lead_days=0,
            core_recurring_lead_days=0,
            b2b_lead_days=0,
            eurusd=Decimal('1.08'),
        )
        self.service = SddService(
            policy,
            MemorySddStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            fx_book=EurUsdBook(rate=Decimal('1.08')),
        )

    def _debtor(self, nickname='Tenant', name='Ada Lovelace', **kwargs):
        return self.service.add_debtor(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            iban=kwargs.pop('iban', DE_IBAN),
            city=kwargs.pop('city', 'Berlin'),
            country=kwargs.pop('country', 'DE'),
            bic=kwargs.pop('bic', 'COBADEFFXXX'),
            default_account=kwargs.pop('default_account', '1001'),
            default_scheme=kwargs.pop('default_scheme', 'core'),
            **kwargs,
        )

    def _mandate(self, debtor=None, **kwargs):
        debtor = debtor or self._debtor()
        return self.service.create_mandate(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            debtor_id=debtor.debtor_id,
            umr=kwargs.pop('umr', 'RENT-ADA-001'),
            scheme=kwargs.pop('scheme', 'core'),
            sequence=kwargs.pop('sequence', 'FRST'),
            activate=kwargs.pop('activate', True),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_iban(self):
        debtor = self._debtor()
        payload = debtor.to_dict()
        self.assertNotIn('iban', payload)
        self.assertEqual(payload['iban_last4'], '3000')
        self.assertEqual(payload['iban_masked'], 'DE****3000')
        mandate = self._mandate(debtor)
        snap = self.service.snapshot('alice')
        self.assertNotIn('iban', snap['debtors'][0])
        row, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='100.00', trace_id='c1',
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'sent')
        self.assertTrue(row.msgid.startswith('SDD20240614'))
        self.assertNotIn('iban', row.to_dict())
        self.assertEqual(self.credits, [])

    def test_same_day_collect_is_idempotent_by_trace(self):
        mandate = self._mandate()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.collection_id, again.collection_id)
        settled = self.service.settle_collection(
            collection_id=first.collection_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(settled.status, 'settled')
        self.assertEqual(self.credits[0], ('1001', '43.20', 'sdd from Tenant'))
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '8.00')

    def test_lead_time_queues_until_collection_date(self):
        self.service.policy.core_first_lead_days = 5
        mandate = self._mandate()
        row, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='50.00', trace_id='late',
        )
        self.assertEqual(row.status, 'queued')
        self.assertEqual(row.collection_date, '2024-06-21')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 21, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'settled')
        self.assertEqual(self.credits[0][1], '54.00')

    def test_ofac_hold_blocks_until_staff_override(self):
        mandate = self._mandate(self._debtor(nickname='Blocked', name='Blocked Person'))
        row, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(row.status, 'held')
        self.assertTrue(row.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(SddError) as ctx:
            self.service.release_collection(collection_id=row.collection_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            collection_id=row.collection_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(self.credits, [])

    def test_dual_control_requires_a_different_employee(self):
        mandate = self._mandate()
        row, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            mandate_id=mandate.mandate_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(SddError) as ctx:
            self.service.release_collection(collection_id=row.collection_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_collection(
            collection_id=row.collection_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')

    def test_credit_account_pause_archive_and_mandate_gate(self):
        mandate = self._mandate()
        with self.assertRaises(SddError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                mandate_id=mandate.mandate_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        debtor_id = mandate.debtor_id
        paused = self.service.set_debtor_status(
            debtor_id=debtor_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(SddError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                mandate_id=mandate.mandate_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'debtor_paused')
        closed = self.service.set_debtor_status(
            debtor_id=debtor_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(SddError) as ctx:
            self.service.set_debtor_status(
                debtor_id=debtor_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_sequence_advances_and_rejects_rcur_before_frst(self):
        mandate = self._mandate(sequence='FRST')
        with self.assertRaises(SddError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                mandate_id=mandate.mandate_id, amount='20.00', sequence='RCUR',
            )
        self.assertEqual(ctx.exception.code, 'invalid_sequence')
        first, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='20.00', trace_id='frst',
        )
        self.service.settle_collection(collection_id=first.collection_id, actor='teller', actor_type='tier1')
        reloaded = self.service.get_mandate(mandate_id=mandate.mandate_id, actor='alice', actor_type='customer')
        self.assertEqual(reloaded.sequence, 'RCUR')
        again, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='20.00', sequence='RCUR', trace_id='rcur',
        )
        self.assertEqual(again.sequence, 'RCUR')

    def test_core_refund_reverses_credit_b2b_does_not(self):
        mandate = self._mandate()
        row, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='60.00', trace_id='ref1',
        )
        self.service.settle_collection(collection_id=row.collection_id, actor='teller', actor_type='tier2')
        refunded = self.service.request_refund(
            collection_id=row.collection_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(refunded.status, 'refunded')
        self.assertEqual(self.debits[-1][1], '64.80')

        b2b_debtor = self._debtor(
            nickname='BizCo', name='Ada Lovelace',
            iban='NL91ABNA0417164300', city='Amsterdam', country='NL',
            bic='ABNANL2A', default_scheme='b2b',
        )
        b2b = self._mandate(b2b_debtor, umr='B2B-1', scheme='b2b')
        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=b2b.mandate_id, amount='15.00', trace_id='b2b1',
        )
        self.service.settle_collection(collection_id=other.collection_id, actor='teller', actor_type='tier2')
        with self.assertRaises(SddError) as ctx:
            self.service.request_refund(
                collection_id=other.collection_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'scheme_no_refund')

    def test_customer_can_cancel_queued_not_sent(self):
        self.service.policy.core_first_lead_days = 2
        mandate = self._mandate()
        row, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='22.00', trace_id='q1',
        )
        self.assertEqual(row.status, 'queued')
        cancelled = self.service.cancel_collection(
            collection_id=row.collection_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.service.policy.core_first_lead_days = 0
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            mandate_id=mandate.mandate_id, amount='22.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'sent')
        with self.assertRaises(SddError) as ctx:
            self.service.cancel_collection(collection_id=live.collection_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_sqlite_roundtrip_masks_iban(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSddStore(path)
            service = SddService(
                SddPolicy(core_first_lead_days=0, core_recurring_lead_days=0, b2b_lead_days=0),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                credit_fn=lambda account, amount, remark: 'Success',
                calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            )
            debtor = service.add_debtor(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Tenant', legal_name='Ada Lovelace', iban=DE_IBAN,
                city='Berlin', country='DE', default_account='1001',
            )
            reloaded = SqliteSddStore(path).get_debtor(debtor.debtor_id)
            self.assertEqual(reloaded.iban, DE_IBAN)
            self.assertNotIn('iban', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['iban_last4'], '3000')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
