import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.canpay import (
    AmountError,
    MemoryCanPayStore,
    CanPayError,
    CanPayPolicy,
    CanPayService,
    SqliteCanPayStore,
    CanPayResolution,
    CaClock,
    sin_ok,
    compose_e2e,
    compose_ca1,
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
    parse_ca1,
    cpa_ok,
    rate_str,
    resolve_payee,
    payments_canada_holidays,
)

ET = timezone(timedelta(hours=-5))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.730000'), Decimal('0.730000'))
        self.assertEqual(rate_str(Decimal('0.730000')), '0.73')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.730000')),
            Decimal('7300.00'),
        )
        previous = os.environ.get('CANPAY_FX_CADUSD')
        os.environ['CANPAY_FX_CADUSD'] = '0.730000'
        try:
            self.assertEqual(CanPayPolicy.from_env().cadusd, Decimal('0.730000'))
        finally:
            if previous is None:
                os.environ.pop('CANPAY_FX_CADUSD', None)
            else:
                os.environ['CANPAY_FX_CADUSD'] = previous

    def test_canpay_bank_and_account(self):
        self.assertTrue(cpa_ok('000100016', '1234567'))
        self.assertTrue(cpa_ok('000300004', '9876543210'))
        self.assertTrue(cpa_ok('000200006', '1122334'))
        self.assertFalse(cpa_ok('XXXXXX', '123'))
        self.assertFalse(cpa_ok('000100016', 'DE00'))
        self.assertEqual(normalize_bank_code('0001-00016'), '000100016')
        with self.assertRaises(CanPayError) as ctx:
            normalize_bank_code('000199999')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(CanPayError):
            normalize_bank_code('XX')

    def test_sin_checksum(self):
        self.assertTrue(sin_ok('046454286'))
        self.assertTrue(sin_ok('046454286'))
        self.assertFalse(sin_ok('046454287'))
        self.assertFalse(sin_ok('123'))

    def test_payee_name_nfc_and_mask(self):
        self.assertEqual(normalize_payee_name('Jean-Pierre  Tremblay'), 'Jean-Pierre Tremblay')
        self.assertEqual(normalize_payee_name('Jean-Pierre Tremblay'), 'Jean-Pierre Tremblay')
        self.assertEqual(mask_payee_name('Jean-Pierre Tremblay'), 'J***y')
        with self.assertRaises(CanPayError) as ctx:
            normalize_payee_name('')
        self.assertEqual(ctx.exception.code, 'invalid_payee_name')
        with self.assertRaises(CanPayError):
            normalize_payee_name('J')

    def test_phone_email_and_iban_proxy(self):
        self.assertEqual(normalize_email('Jean@Example.ca'), 'jean@example.ca')
        self.assertEqual(mask_email('jean@example.ca'), 'j***@example.ca')
        with self.assertRaises(CanPayError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('4165551234'), '+14165551234')
        self.assertEqual(normalize_phone('+1 416 555 1234'), '+14165551234')
        self.assertEqual(mask_phone('+14165551234'), '+1****1234')
        with self.assertRaises(CanPayError):
            normalize_phone('2125551234')

    def test_e2e_and_ca1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('inst', '20240614', 1), 'IETC20240614000001')
        self.assertEqual(compose_e2e('eft', '20240614', 2), 'CPA520240614000002')
        self.assertEqual(compose_e2e('lynx', '20240614', 3), 'LYNX20240614000003')
        self.assertEqual(mask_e2e('IETC20240614000001'), 'IETC****0001')
        with self.assertRaises(CanPayError) as ctx:
            compose_e2e('inst', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_ca1(
            rail='inst', amount='10000.00', bank_code='000100016', branch_code='',
            account_last4='4567', e2e_id='IETC20240614000001', purpose='family',
            value_date='20240614', payee_mask='J***y', ofac_hit=False, sequence=1,
        )
        parsed = parse_ca1(line)
        self.assertEqual(parsed['clearing'], 'IETC')
        self.assertEqual(parsed['currency'], 'CAD')
        with self.assertRaises(CanPayError) as ctx:
            parse_ca1('<?xml version="1.0"?><INST/>')
        self.assertEqual(ctx.exception.code, 'invalid_ca1')
        with self.assertRaises(CanPayError) as ctx:
            parse_ca1('1|PPAY|CAD|10.00|011||7890|IETC20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(CanPayError) as ctx:
            parse_ca1('1|INST|CAD|10.00|011||7890|IETC20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(CanPayError) as ctx:
            parse_ca1('1|IETC|USD|10.00|011||7890|IETC20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_payee_name_inquiry_outcomes(self):
        hit = resolve_payee('', '', 'Jean-Pierre Tremblay', bank_code='000100016', account_number='1234567')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_payee('phone', '4165551234', 'Jean-Pierre')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_payee('email', 'jean@example.ca', 'François Côté')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_payee('phone', '6045551234', 'Jean-Pierre Tremblay')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, CanPayResolution)
        self.assertNotIn('1234567', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '0001****4567')
        self.assertEqual(close.to_dict()['proxy_mask'], '+1****1234')
        self.assertEqual(hit.to_dict()['registered_mask'], 'J***y')

    def test_ca_clock_inst_247_eft_cutoff_lynx(self):
        calendar = CaClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_sct_late = ts(2024, 6, 14, 16, 0)
        friday_pass_late = ts(2024, 6, 14, 18, 30)
        self.assertEqual(calendar.value_date(friday, rail='inst').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_sct_late, rail='inst').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_sct_late, rail='inst')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='eft').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_sct_late, rail='eft').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_sct_late, rail='eft')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='lynx').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_pass_late, rail='lynx').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='eft').isoformat(), '2024-06-17')
        self.assertIn(datetime(2022, 1, 1).date(), payments_canada_holidays(2022))
        self.assertIn(datetime(2022, 1, 3).date(), payments_canada_holidays(2022))
        self.assertIn(datetime(2024, 3, 29).date(), payments_canada_holidays(2024))
        national = ts(2022, 1, 1, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='inst').isoformat(), '2022-01-01')
        self.assertEqual(calendar.value_date(national, rail='eft').isoformat(), '2022-01-04')


class CanPayServiceTests(unittest.TestCase):
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

        policy = CanPayPolicy(
            outbound_fee=Decimal('0.00'),
            lynx_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('50000.00'),
            cadusd=Decimal('0.730000'),
        )
        self.service = CanPayService(
            policy,
            MemoryCanPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=CaClock(),
        )

    def _add(self, nickname='Jean Mobile', name='Jean Pierre Tremblay', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            payee_name=kwargs.pop('payee_name', 'Jean-Pierre Tremblay'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '4165551234'),
            bank_code=kwargs.pop('bank_code', '000100016'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '1234567'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_payee_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('payee_name', payload)
        self.assertEqual(payload['proxy_mask'], '+1****1234')
        self.assertEqual(payload['account_last4'], '4567')
        self.assertEqual(payload['payee_mask'], 'J***y')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('4165551234', str(snap))
        self.assertNotIn('1234567', str(snap))
        self.assertNotIn('Jean-Pierre Tremblay', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '7300.00')
        self.assertTrue(payment.e2e_id.startswith('IETC20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('IETC'))
        self.assertNotIn('4165551234', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '7300.00', 'interac to Jean Mobile'))

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

    def test_eft_after_cutoff_queues_lynx_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='RBC Lynx', proxy_type='', proxy='',
            bank_code='000300004', account_number='9876543210',
            payee_name='François Côté', legal_name='François Côté',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='eft', trace_id='late-sct',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 18, 30)
        target, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='lynx', trace_id='late-t2',
        )
        self.assertEqual(target.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-sct'], 'sent')
        self.assertEqual(statuses['late-t2'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('24090.00', usd_amounts)
        self.assertIn('29200.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='000300004',
            account_number='9876543210', payee_name='François Côté',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='inst', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(CanPayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '7300.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(CanPayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '36500.00')

    def test_payee_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', payee_name='Someone Else CA')
        with self.assertRaises(CanPayError) as ctx:
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

    def test_credit_account_pause_canpay_cap_and_lynx_currency(self):
        bene = self._add()
        with self.assertRaises(CanPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(CanPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(CanPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='lynx', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(CanPayError) as ctx:
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
        with self.assertRaises(CanPayError) as ctx:
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
        with self.assertRaises(CanPayError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '7300.00')

    def test_sqlite_roundtrip_masks_payee_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCanPayStore(path)
            service = CanPayService(
                CanPayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=CaClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BMO Jean', legal_name='Jean Pierre Tremblay', payee_name='Jean-Pierre Tremblay',
                default_account='1001', proxy_type='', proxy='',
                bank_code='000100016', account_number='1234567',
            )
            reloaded = SqliteCanPayStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '1234567')
            self.assertEqual(reloaded.payee_name, 'Jean-Pierre Tremblay')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('payee_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['payee_mask'], 'J***y')
            self.assertEqual(reloaded.to_dict()['account_last4'], '4567')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
