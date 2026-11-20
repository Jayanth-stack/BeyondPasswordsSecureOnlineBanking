import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.sepa import (
    AmountError,
    MemorySepaStore,
    SepaError,
    SepaPolicy,
    SepaService,
    SqliteSepaStore,
    SepaResolution,
    EuClock,
    idnr_ok,
    compose_e2e,
    compose_se1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_sepa_name,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_sepa_name,
    parse_fx_rate,
    parse_money,
    parse_se1,
    sepa_ok,
    rate_str,
    resolve_sepa,
    target2_holidays,
)

CET = timezone(timedelta(hours=1))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('1.085000'), Decimal('1.085000'))
        self.assertEqual(rate_str(Decimal('1.085000')), '1.085')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('1.085000')),
            Decimal('10850.00'),
        )
        previous = os.environ.get('SEPA_FX_EURUSD')
        os.environ['SEPA_FX_EURUSD'] = '1.085000'
        try:
            self.assertEqual(SepaPolicy.from_env().eurusd, Decimal('1.085000'))
        finally:
            if previous is None:
                os.environ.pop('SEPA_FX_EURUSD', None)
            else:
                os.environ['SEPA_FX_EURUSD'] = previous

    def test_sepa_bank_and_account(self):
        self.assertTrue(sepa_ok('COBADEFFXXX', 'DE89370400440532013000'))
        self.assertTrue(sepa_ok('BNPAFRPPXXX', 'FR1420041010050500013M02606'))
        self.assertTrue(sepa_ok('ABNANL2AXXX', 'NL91ABNA0417164300'))
        self.assertFalse(sepa_ok('XXXXXX', 'DE89370400440532013999'))
        self.assertFalse(sepa_ok('COBADEFFXXX', 'DE00'))
        self.assertEqual(normalize_bank_code('COBA-DEFF-XXX'), 'COBADEFFXXX')
        with self.assertRaises(SepaError) as ctx:
            normalize_bank_code('COBA12FFXXX')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(SepaError):
            normalize_bank_code('XX')

    def test_idnr_checksum(self):
        self.assertTrue(idnr_ok('12345678903'))
        self.assertTrue(idnr_ok('12345678903'))
        self.assertFalse(idnr_ok('12345678900'))
        self.assertFalse(idnr_ok('123'))

    def test_sepa_name_nfc_and_mask(self):
        self.assertEqual(normalize_sepa_name('José  García'), 'José García')
        self.assertEqual(normalize_sepa_name('José García'), 'José García')
        self.assertEqual(mask_sepa_name('José García'), 'J***a')
        with self.assertRaises(SepaError) as ctx:
            normalize_sepa_name('')
        self.assertEqual(ctx.exception.code, 'invalid_sepa_name')
        with self.assertRaises(SepaError):
            normalize_sepa_name('J')

    def test_phone_email_and_iban_proxy(self):
        self.assertEqual(normalize_email('Jose@Example.de'), 'jose@example.de')
        self.assertEqual(mask_email('jose@example.de'), 'j***@example.de')
        with self.assertRaises(SepaError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('15112345678'), '+4915112345678')
        self.assertEqual(normalize_phone('+49 151 1234 5678'), '+4915112345678')
        self.assertEqual(mask_phone('+4915112345678'), '+49****5678')
        with self.assertRaises(SepaError):
            normalize_phone('03012345678')

    def test_e2e_and_se1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('inst', '20240614', 1), 'SCTI20240614000001')
        self.assertEqual(compose_e2e('sct', '20240614', 2), 'SCT120240614000002')
        self.assertEqual(compose_e2e('t2', '20240614', 3), 'TARG20240614000003')
        self.assertEqual(mask_e2e('SCTI20240614000001'), 'SCTI****0001')
        with self.assertRaises(SepaError) as ctx:
            compose_e2e('inst', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_se1(
            rail='inst', amount='10000.00', bank_code='COBADEFFXXX', branch_code='',
            account_last4='3000', e2e_id='SCTI20240614000001', purpose='family',
            value_date='20240614', sepa_mask='J***a', ofac_hit=False, sequence=1,
        )
        parsed = parse_se1(line)
        self.assertEqual(parsed['clearing'], 'SCTI')
        self.assertEqual(parsed['currency'], 'EUR')
        with self.assertRaises(SepaError) as ctx:
            parse_se1('<?xml version="1.0"?><INST/>')
        self.assertEqual(ctx.exception.code, 'invalid_se1')
        with self.assertRaises(SepaError) as ctx:
            parse_se1('1|PPAY|EUR|10.00|011||7890|SCTI20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(SepaError) as ctx:
            parse_se1('1|INST|EUR|10.00|011||7890|SCTI20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(SepaError) as ctx:
            parse_se1('1|SCTI|USD|10.00|011||7890|SCTI20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_sepa_name_inquiry_outcomes(self):
        hit = resolve_sepa('', '', 'José García', bank_code='COBADEFFXXX', account_number='DE89370400440532013000')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_sepa('phone', '15112345678', 'José')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_sepa('email', 'jose@example.de', 'Marie Dupont')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_sepa('phone', '15100001111', 'José García')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, SepaResolution)
        self.assertNotIn('DE89370400440532013000', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], 'DE89****3000')
        self.assertEqual(close.to_dict()['proxy_mask'], '+49****5678')
        self.assertEqual(hit.to_dict()['registered_mask'], 'J***a')

    def test_eu_clock_inst_247_sct_cutoff_t2(self):
        calendar = EuClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_sct_late = ts(2024, 6, 14, 16, 0)
        friday_pass_late = ts(2024, 6, 14, 17, 30)
        self.assertEqual(calendar.value_date(friday, rail='inst').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_sct_late, rail='inst').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_sct_late, rail='inst')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='sct').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_sct_late, rail='sct').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_sct_late, rail='sct')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='t2').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_pass_late, rail='t2').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='sct').isoformat(), '2024-06-17')
        self.assertIn(datetime(2022, 1, 1).date(), target2_holidays(2022))
        self.assertIn(datetime(2022, 1, 3).date(), target2_holidays(2022))
        self.assertIn(datetime(2024, 3, 29).date(), target2_holidays(2024))
        national = ts(2022, 1, 1, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='inst').isoformat(), '2022-01-01')
        self.assertEqual(calendar.value_date(national, rail='sct').isoformat(), '2022-01-04')


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
            outbound_fee=Decimal('0.00'),
            t2_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('50000.00'),
            eurusd=Decimal('1.085000'),
        )
        self.service = SepaService(
            policy,
            MemorySepaStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=EuClock(),
        )

    def _add(self, nickname='Jose Mobile', name='Jose Garcia', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            sepa_name=kwargs.pop('sepa_name', 'José García'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '15112345678'),
            bank_code=kwargs.pop('bank_code', 'COBADEFFXXX'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', 'DE89370400440532013000'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_chinese_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('sepa_name', payload)
        self.assertEqual(payload['proxy_mask'], '+49****5678')
        self.assertEqual(payload['account_last4'], '3000')
        self.assertEqual(payload['sepa_mask'], 'J***a')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('15112345678', str(snap))
        self.assertNotIn('DE89370400440532013000', str(snap))
        self.assertNotIn('José García', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '10850.00')
        self.assertTrue(payment.e2e_id.startswith('SCTI20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('SCTI'))
        self.assertNotIn('15112345678', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '10850.00', 'sepa to Jose Mobile'))

    def test_same_day_send_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len(self.debits), 1)

    def test_sct_after_cutoff_queues_t2_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='BNP RTGS', proxy_type='', proxy='',
            bank_code='BNPAFRPPXXX', account_number='FR1420041010050500013M02606',
            sepa_name='Marie Dupont', legal_name='Marie Dupont',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='sct', trace_id='late-sct',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 17, 30)
        target, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='t2', trace_id='late-t2',
        )
        self.assertEqual(target.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-sct'], 'sent')
        self.assertEqual(statuses['late-t2'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('35805.00', usd_amounts)
        self.assertIn('43400.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='BNPAFRPPXXX',
            account_number='FR1420041010050500013M02606', sepa_name='Marie Dupont',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='inst', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(SepaError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '10850.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(SepaError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '54250.00')

    def test_sepa_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', sepa_name='Pierre Martin')
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='chinese-no',
            )
        self.assertEqual(ctx.exception.code, 'sepa_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='chinese-yes',
            sepa_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.sepa_outcome, 'match')

    def test_credit_account_pause_sepa_cap_and_target_currency(self):
        bene = self._add()
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='t2', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')

    def test_nsf_does_not_assign_e2e(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(SepaError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].e2e_id, '')

    def test_customer_cannot_cancel_completed_staff_can_recall(self):
        bene = self._add()
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'completed')
        with self.assertRaises(SepaError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '10850.00')

    def test_sqlite_roundtrip_masks_chinese_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSepaStore(path)
            service = SepaService(
                SepaPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=EuClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='COBA Jose', legal_name='Jose Garcia', sepa_name='José García',
                default_account='1001', proxy_type='', proxy='',
                bank_code='COBADEFFXXX', account_number='DE89370400440532013000',
            )
            reloaded = SqliteSepaStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, 'DE89370400440532013000')
            self.assertEqual(reloaded.sepa_name, 'José García')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('sepa_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['sepa_mask'], 'J***a')
            self.assertEqual(reloaded.to_dict()['account_last4'], '3000')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
