import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.sepa import (
    AmountError,
    EurUsdBook,
    MemorySepaStore,
    SepaError,
    SepaPolicy,
    SepaService,
    SqliteSepaStore,
    Target2Calendar,
    compose_end_to_end_id,
    compose_pain001,
    compute_fee,
    easter_gregorian,
    iban_check_digit_ok,
    mask_iban,
    money_str,
    normalize_bic,
    normalize_creditor_ref,
    normalize_iban,
    parse_money,
    rf_check_digit_ok,
    target2_holidays,
)

CEST = timezone(timedelta(hours=2))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


DE_IBAN = 'DE89370400440532013000'
NL_IBAN = 'NL91ABNA0417164300'
RF_REF = 'RF18539007547034'


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')

    def test_iban_checksum_and_sepa_zone(self):
        self.assertTrue(iban_check_digit_ok(DE_IBAN))
        self.assertEqual(normalize_iban('DE89 3704 0044 0532 0130 00'), DE_IBAN)
        self.assertEqual(mask_iban(DE_IBAN), 'DE****3000')
        with self.assertRaises(SepaError) as ctx:
            normalize_iban('DE89370400440532013001')
        self.assertEqual(ctx.exception.code, 'invalid_iban')
        with self.assertRaises(SepaError) as ctx:
            normalize_iban('US64SVBKUS6S330095838953')
        self.assertEqual(ctx.exception.code, 'not_sepa_country')
        with self.assertRaises(SepaError) as ctx:
            normalize_iban('DE89')
        self.assertEqual(ctx.exception.code, 'invalid_iban')

    def test_bic_pads_8_to_11_and_rejects_non_sepa(self):
        self.assertEqual(normalize_bic('DEUTDEFF'), 'DEUTDEFFXXX')
        self.assertEqual(normalize_bic('NWBKGB2LXXX'), 'NWBKGB2LXXX')
        with self.assertRaises(SepaError) as ctx:
            normalize_bic('CHASUS33')
        self.assertEqual(ctx.exception.code, 'invalid_bic')
        with self.assertRaises(SepaError) as ctx:
            normalize_bic('DEUT0EFF')
        self.assertEqual(ctx.exception.code, 'invalid_bic')

    def test_iso11649_rf_reference(self):
        self.assertTrue(rf_check_digit_ok(RF_REF))
        self.assertEqual(normalize_creditor_ref('RF18 5390 0754 7034'), RF_REF)
        with self.assertRaises(SepaError) as ctx:
            normalize_creditor_ref('RF00539007547034')
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        self.assertEqual(normalize_creditor_ref(''), '')

    def test_pain001_and_end_to_end(self):
        self.assertEqual(compose_end_to_end_id('sct', '20240614', 1), 'SCT20240614000001')
        self.assertEqual(compose_end_to_end_id('sct_inst', '20240614', 2), 'INST20240614000002')
        payload = compose_pain001(
            msg_id='MSG1', instr_id='INS1', end_to_end_id='SCT20240614000001',
            scheme='sct_inst', amount_eur='100.00', creditor_name='Ada',
            iban=DE_IBAN, bic='DEUTDEFFXXX', remittance='invoice 9', purpose='goods',
        )
        self.assertEqual(payload['SvcLvl'], 'SEPA')
        self.assertEqual(payload['LclInstrm'], 'INST')
        self.assertEqual(payload['ChrgBr'], 'SLEV')
        self.assertEqual(payload['InstdAmt']['Ccy'], 'EUR')
        self.assertEqual(payload['Purp'], 'GDDS')
        self.assertEqual(payload['CdtrAcct']['IBAN'], DE_IBAN)
        structured = compose_pain001(
            msg_id='MSG1', instr_id='INS1', end_to_end_id='E2E',
            scheme='sct', amount_eur='10.00', creditor_name='Ada',
            iban=DE_IBAN, creditor_ref=RF_REF,
        )
        self.assertEqual(structured['RmtInf']['Strd']['CdtrRefInf']['Ref'], RF_REF)
        self.assertNotIn('Ustrd', structured['RmtInf'])

    def test_fee_by_scheme_or_waived(self):
        self.assertEqual(compute_fee('sct'), Decimal('15.00'))
        self.assertEqual(compute_fee('sct_inst'), Decimal('25.00'))
        self.assertEqual(compute_fee('sct', waived=True), Decimal('0.00'))

    def test_eurusd_quote(self):
        book = EurUsdBook(Decimal('1.080000'))
        quote = book.quote(Decimal('100.00'))
        self.assertEqual(quote.debit_usd, '108.00')
        self.assertEqual(quote.currency, 'EUR')

    def test_target2_cutoff_weekend_easter_and_instant_247(self):
        calendar = Target2Calendar(cutoff_hour=16, tz_offset_hours=2)
        friday_before = ts(2024, 6, 14, 15, 0)
        friday_after = ts(2024, 6, 14, 16, 30)
        self.assertEqual(calendar.value_date(friday_before).isoformat(), '2024-06-17')
        self.assertEqual(calendar.value_date(friday_after).isoformat(), '2024-06-18')
        self.assertTrue(calendar.snapshot(friday_after)['after_cutoff'])
        self.assertEqual(calendar.value_date(friday_after, scheme='sct_inst').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_after, scheme='sct_inst')['after_cutoff'])
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, scheme='sct_inst').isoformat(), '2024-06-15')
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        holidays = target2_holidays(2024)
        self.assertIn(datetime(2024, 3, 29).date(), holidays)
        self.assertIn(datetime(2024, 5, 1).date(), holidays)
        may1 = ts(2024, 5, 1, 10, 0)
        self.assertEqual(calendar.value_date(may1).isoformat(), '2024-05-03')


class SepaServiceTests(unittest.TestCase):
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

        policy = SepaPolicy(
            sct_fee=Decimal('15.00'),
            instant_fee=Decimal('25.00'),
            dual_control_threshold=Decimal('10000.00'),
            eurusd=Decimal('1.080000'),
        )
        self.service = SepaService(
            policy,
            MemorySepaStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            fx=EurUsdBook(Decimal('1.080000')),
        )

    def _add(self, nickname='Berlin Rent', name='Ada Lovelace', **kwargs):
        return self.service.add_creditor(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            iban=kwargs.pop('iban', DE_IBAN),
            bic=kwargs.pop('bic', 'DEUTDEFF'),
            city=kwargs.pop('city', 'Berlin'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_iban(self):
        creditor = self._add()
        payload = creditor.to_dict()
        self.assertNotIn('iban', payload)
        self.assertEqual(payload['iban_masked'], 'DE****3000')
        self.assertEqual(payload['bic'], 'DEUTDEFFXXX')
        snap = self.service.snapshot('alice')
        self.assertNotIn('iban', snap['creditors'][0])
        transfer, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='100.00', trace_id='s1',
        )
        self.assertTrue(created)
        self.assertNotIn('iban', transfer.to_dict())
        self.assertEqual(transfer.status, 'sent')
        self.assertTrue(transfer.end_to_end_id.startswith('SCT20240617'))
        self.assertEqual(self.debits[0], ('1001', '108.00', 'sepa to Berlin Rent'))
        self.assertEqual(self.debits[1][1], '15.00')

    def test_same_day_send_is_idempotent_by_trace(self):
        creditor = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.transfer_id, again.transfer_id)
        self.assertEqual(len([row for row in self.debits if row[2].startswith('sepa to')]), 1)

    def test_sct_after_cutoff_queues_until_value_date(self):
        self.now[0] = ts(2024, 6, 14, 16, 30)
        creditor = self._add()
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='50.00', trace_id='late',
        )
        self.assertEqual(transfer.status, 'queued')
        self.assertEqual(transfer.value_date, '20240618')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 18, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0][2], 'sepa to Berlin Rent')

    def test_instant_settles_24x7_and_is_irrevocable(self):
        self.now[0] = ts(2024, 6, 15, 22, 0)
        creditor = self._add(nickname='Payroll NL', iban=NL_IBAN, bic='ABNANL2A', city='Amsterdam')
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='80.00', scheme='instant',
            trace_id='inst',
        )
        self.assertEqual(transfer.status, 'completed')
        self.assertEqual(transfer.scheme, 'sct_inst')
        self.assertTrue(transfer.end_to_end_id.startswith('INST'))
        self.assertTrue(transfer.tx_id)
        self.assertEqual(self.debits[0][1], '86.40')
        with self.assertRaises(SepaError) as ctx:
            self.service.recall_transfer(
                transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_instant_cap_and_bic_country_mismatch(self):
        with self.assertRaises(SepaError) as ctx:
            self._add(iban=DE_IBAN, bic='NWBKGB2L')
        self.assertEqual(ctx.exception.code, 'bic_country_mismatch')
        creditor = self._add()
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                creditor_id=creditor.creditor_id, amount='100000.01', scheme='sct_inst',
            )
        self.assertEqual(ctx.exception.code, 'instant_amount_exceeded')

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        creditor = self._add(nickname='Blocked', name='Blocked Person')
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(transfer.status, 'held')
        self.assertTrue(transfer.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(SepaError) as ctx:
            self.service.release_transfer(
                transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(self.debits[0][1], '86.40')

    def test_dual_control_uses_usd_equivalent(self):
        creditor = self._add()
        # 10000 EUR * 1.08 = 10800 USD >= 10000 dual-control
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            creditor_id=creditor.creditor_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(transfer.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(SepaError) as ctx:
            self.service.release_transfer(
                transfer_id=transfer.transfer_id, actor='maker', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_transfer(
            transfer_id=transfer.transfer_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10800.00')

    def test_credit_account_pause_archive_and_sct_recall(self):
        creditor = self._add()
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                creditor_id=creditor.creditor_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_creditor_status(
            creditor_id=creditor.creditor_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                creditor_id=creditor.creditor_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'creditor_paused')
        self.service.set_creditor_status(
            creditor_id=creditor.creditor_id, actor='alice', actor_type='customer', status='resume',
        )
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            creditor_id=creditor.creditor_id, amount='20.00', trace_id='r1',
        )
        recalled = self.service.recall_transfer(
            transfer_id=sent.transfer_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '21.60')
        archived = self.service.set_creditor_status(
            creditor_id=creditor.creditor_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(archived.status, 'archived')

    def test_nsf_does_not_assign_end_to_end(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient funds'

        self.service.debit_fn = nsf
        creditor = self._add()
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                creditor_id=creditor.creditor_id, amount='30.00', trace_id='nsf',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['transfer'].end_to_end_id, '')

    def test_sqlite_roundtrip_and_duplicate_iban(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSepaStore(path)
            service = SepaService(
                SepaPolicy(eurusd=Decimal('1.080000')),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            )
            first = service.add_creditor(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Rent', legal_name='Ada Lovelace', iban=DE_IBAN,
                bic='DEUTDEFF', city='Berlin', default_account='1001',
            )
            with self.assertRaises(SepaError) as ctx:
                service.add_creditor(
                    owner_userid='alice', actor='alice', actor_type='customer',
                    nickname='Other', legal_name='Ada Lovelace', iban=DE_IBAN,
                    city='Berlin', default_account='1001',
                )
            self.assertEqual(ctx.exception.code, 'creditor_duplicate')
            sent, _ = service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                creditor_id=first.creditor_id, amount='10.00', trace_id='sql-1',
            )
            loaded = SqliteSepaStore(path).get_transfer(sent.transfer_id)
            self.assertEqual(loaded.iban_masked, 'DE****3000')
            self.assertEqual(loaded.amount, '10.00')
        finally:
            os.remove(path)


if __name__ == '__main__':
    unittest.main()
