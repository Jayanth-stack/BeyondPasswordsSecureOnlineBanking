import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.swpay import (
    AmountError,
    MemorySwPayStore,
    SwPayError,
    SwPayPolicy,
    SwPayService,
    SqliteSwPayStore,
    SwPayResolution,
    SwClock,
    idnr_ok,
    compose_e2e,
    compose_sw1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_payee_name,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_payee_name,
    parse_fx_rate,
    parse_money,
    parse_sw1,
    swift_ok,
    rate_str,
    resolve_payee,
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
        self.assertEqual(parse_fx_rate('0.006700'), Decimal('0.006700'))
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.006700')),
            Decimal('67.00'),
        )
        self.assertEqual(parse_money('0.0067'), Decimal('0.01'))
        previous = os.environ.get('SWPAY_FX_EURUSD')
        os.environ['SWPAY_FX_EURUSD'] = '1.085000'
        try:
            self.assertEqual(SwPayPolicy.from_env().eurusd, Decimal('1.085000'))
        finally:
            if previous is None:
                os.environ.pop('SWPAY_FX_EURUSD', None)
            else:
                os.environ['SWPAY_FX_EURUSD'] = previous

    def test_iban_bic_and_account(self):
        self.assertTrue(swift_ok('COBADEFFXXX', 'DE89370400440532013000'))
        self.assertTrue(swift_ok('BNPAFRPPXXX', 'FR1420041010050500013M02606'))
        self.assertTrue(swift_ok('ABNANL2AXXX', 'NL91ABNA0417164300'))
        self.assertFalse(swift_ok('XXXXXX', '123'))
        self.assertFalse(swift_ok('COBADEFFXXX', 'DE00'))
        self.assertEqual(normalize_bank_code('COBADEFF'), 'COBADEFFXXX')
        with self.assertRaises(SwPayError) as ctx:
            normalize_bank_code('COBA0EFFXXX')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(SwPayError):
            normalize_bank_code('XX')

    def test_idnr_checksum(self):
        self.assertTrue(idnr_ok('12345678903'))
        self.assertFalse(idnr_ok('12345678904'))
        self.assertFalse(idnr_ok('123'))

    def test_payee_name_nfc_and_mask(self):
        self.assertEqual(normalize_payee_name('José  García'), 'José García')
        self.assertEqual(normalize_payee_name('José García'), 'José García')
        self.assertEqual(mask_payee_name('José García'), 'J***a')
        with self.assertRaises(SwPayError) as ctx:
            normalize_payee_name('')
        self.assertEqual(ctx.exception.code, 'invalid_payee_name')
        with self.assertRaises(SwPayError):
            normalize_payee_name('J')

    def test_phone_email_and_iban_proxy(self):
        self.assertEqual(normalize_email('Jose@Example.de'), 'jose@example.de')
        self.assertEqual(mask_email('jose@example.de'), 'j***@example.de')
        with self.assertRaises(SwPayError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('15112345678'), '+4915112345678')
        self.assertEqual(normalize_phone('+49 151 12345678'), '+4915112345678')
        self.assertEqual(mask_phone('+4915112345678'), '+49****5678')
        with self.assertRaises(SwPayError):
            normalize_phone('0301234567')

    def test_e2e_and_sw1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('inst', '20240614', 1), 'SGPI20240614000001')
        self.assertEqual(compose_e2e('mt', '20240614', 2), 'MT1320240614000002')
        self.assertEqual(compose_e2e('cov', '20240614', 3), 'MCOV20240614000003')
        self.assertEqual(mask_e2e('SGPI20240614000001'), 'SGPI****0001')
        with self.assertRaises(SwPayError) as ctx:
            compose_e2e('inst', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_sw1(
            rail='inst', amount='10000.00', bank_code='COBADEFFXXX', branch_code='',
            account_last4='3000', e2e_id='SGPI20240614000001', purpose='family',
            value_date='20240614', payee_mask='J***a', ofac_hit=False, sequence=1,
        )
        parsed = parse_sw1(line)
        self.assertEqual(parsed['clearing'], 'SGPI')
        self.assertEqual(parsed['currency'], 'EUR')
        with self.assertRaises(SwPayError) as ctx:
            parse_sw1('<?xml version="1.0"?><SWIFT/>')
        self.assertEqual(ctx.exception.code, 'invalid_sw1')
        with self.assertRaises(SwPayError) as ctx:
            parse_sw1('1|PPAY|EUR|10.00|COBADEFFXXX||3000|SGPI20240614000001|family|20240614|****|N|1|CHASUS33XXX')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(SwPayError) as ctx:
            parse_sw1('1|INST|EUR|10.00|COBADEFFXXX||3000|SGPI20240614000001|family|20240614|****|N|1|CHASUS33XXX')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(SwPayError) as ctx:
            parse_sw1('1|SGPI|XXX|10.00|COBADEFFXXX||3000|SGPI20240614000001|family|20240614|****|N|1|CHASUS33XXX')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_payee_name_inquiry_outcomes(self):
        hit = resolve_payee('', '', 'José García', bank_code='COBADEFFXXX', account_number='DE89370400440532013000')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_payee('phone', '15112345678', 'José')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_payee('email', 'jose@example.de', 'Marie Dupont')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_payee('phone', '15199999999', 'José García')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, SwPayResolution)
        self.assertNotIn('DE89370400440532013000', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], 'DE89****3000')
        self.assertEqual(close.to_dict()['proxy_mask'], '+49****5678')
        self.assertEqual(hit.to_dict()['registered_mask'], 'J***a')

    def test_sw_clock_inst_247_mt_cutoff_cov(self):
        calendar = SwClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_mt_late = ts(2024, 6, 14, 16, 0)
        friday_cov_late = ts(2024, 6, 14, 17, 30)
        self.assertEqual(calendar.value_date(friday, rail='inst').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_mt_late, rail='inst').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_mt_late, rail='inst')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='mt').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_mt_late, rail='mt').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_mt_late, rail='mt')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='cov').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_cov_late, rail='cov').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='mt').isoformat(), '2024-06-17')
        self.assertIn(datetime(2022, 1, 1).date(), target2_holidays(2022))
        self.assertIn(datetime(2022, 1, 3).date(), target2_holidays(2022))
        self.assertIn(datetime(2024, 3, 29).date(), target2_holidays(2024))
        national = ts(2022, 1, 1, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='inst').isoformat(), '2022-01-01')
        self.assertEqual(calendar.value_date(national, rail='mt').isoformat(), '2022-01-04')


class SwPayServiceTests(unittest.TestCase):
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

        policy = SwPayPolicy(
            outbound_fee=Decimal('0.00'),
            cov_fee=Decimal('45.00'),
            dual_control_threshold=Decimal('50000.00'),
            eurusd=Decimal('1.085000'),
        )
        self.service = SwPayService(
            policy,
            MemorySwPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SwClock(),
        )

    def _add(self, nickname='Jose Mobile', name='Jose Garcia', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            payee_name=kwargs.pop('payee_name', 'José García'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '15112345678'),
            bank_code=kwargs.pop('bank_code', 'COBADEFFXXX'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', 'DE89370400440532013000'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_payee_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('payee_name', payload)
        self.assertEqual(payload['proxy_mask'], '+49****5678')
        self.assertEqual(payload['account_last4'], '3000')
        self.assertEqual(payload['payee_mask'], 'J***a')
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
        self.assertTrue(payment.e2e_id.startswith('SGPI20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('SGPI'))
        self.assertNotIn('15112345678', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '10850.00', 'swift to Jose Mobile'))

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

    def test_mt_after_cutoff_queues_cov_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='BNP Cover', proxy_type='', proxy='',
            bank_code='BNPAFRPPXXX', account_number='FR1420041010050500013M02606',
            payee_name='Marie Dupont', legal_name='Marie Dupont',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='mt', trace_id='late-mt',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 17, 30)
        target, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='cov', trace_id='late-cov',
        )
        self.assertEqual(target.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-mt'], 'sent')
        self.assertEqual(statuses['late-cov'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('35805.00', usd_amounts)
        self.assertIn('43400.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='BNPAFRPPXXX',
            account_number='FR1420041010050500013M02606', payee_name='Marie Dupont',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='inst', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(SwPayError) as ctx:
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
        with self.assertRaises(SwPayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '54250.00')

    def test_payee_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', payee_name='Someone Else DE')
        with self.assertRaises(SwPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='payee-no',
            )
        self.assertEqual(ctx.exception.code, 'payee_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='payee-yes',
            payee_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.payee_outcome, 'match')

    def test_credit_account_pause_swpay_cap_and_cov_currency(self):
        bene = self._add()
        with self.assertRaises(SwPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(SwPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(SwPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='cov', currency='XXX',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(SwPayError) as ctx:
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
        with self.assertRaises(SwPayError) as ctx:
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
        with self.assertRaises(SwPayError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '10850.00')

    def test_sqlite_roundtrip_masks_payee_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSwPayStore(path)
            service = SwPayService(
                SwPayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=SwClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Commerz Jose', legal_name='Jose Garcia', payee_name='José García',
                default_account='1001', proxy_type='', proxy='',
                bank_code='COBADEFFXXX', account_number='DE89370400440532013000',
            )
            reloaded = SqliteSwPayStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, 'DE89370400440532013000')
            self.assertEqual(reloaded.payee_name, 'José García')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('payee_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['payee_mask'], 'J***a')
            self.assertEqual(reloaded.to_dict()['account_last4'], '3000')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
