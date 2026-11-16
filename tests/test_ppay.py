import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.ppay import (
    AmountError,
    MemoryPpayStore,
    PpayError,
    PpayPolicy,
    PpayService,
    SqlitePpayStore,
    ThaiResolution,
    ThClock,
    citizen_check_digit_ok,
    compose_e2e,
    compose_pp1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_thai,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_thai,
    parse_fx_rate,
    parse_money,
    parse_pp1,
    ppay_ok,
    rate_str,
    resolve_ppay,
    th_holidays,
)

ICT = timezone(timedelta(hours=7))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ICT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.028500'), Decimal('0.028500'))
        self.assertEqual(rate_str(Decimal('0.028500')), '0.0285')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.028500')),
            Decimal('285.00'),
        )
        previous = os.environ.get('PPAY_FX_THBUSD')
        os.environ['PPAY_FX_THBUSD'] = '0.028500'
        try:
            self.assertEqual(PpayPolicy.from_env().thbusd, Decimal('0.028500'))
        finally:
            if previous is None:
                os.environ.pop('PPAY_FX_THBUSD', None)
            else:
                os.environ['PPAY_FX_THBUSD'] = previous

    def test_bot_bank_and_account(self):
        self.assertTrue(ppay_ok('014', '1234567890'))
        self.assertTrue(ppay_ok('004', '9876543210'))
        self.assertTrue(ppay_ok('025', '1111222233'))
        self.assertFalse(ppay_ok('000', '1234567890'))
        self.assertFalse(ppay_ok('014', '1234'))
        self.assertEqual(normalize_bank_code('14'), '014')
        with self.assertRaises(PpayError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_citizen_checksum(self):
        self.assertTrue(citizen_check_digit_ok('1234567890121'))
        self.assertFalse(citizen_check_digit_ok('1234567890120'))
        self.assertFalse(citizen_check_digit_ok('123'))

    def test_thai_nfc_and_mask(self):
        self.assertEqual(normalize_thai('สมชาย  ใจดี'), 'สมชาย ใจดี')
        self.assertEqual(normalize_thai('สมชาย ใจดี'), 'สมชาย ใจดี')
        self.assertEqual(mask_thai('สมชาย ใจดี'), 'ส***ด')
        with self.assertRaises(PpayError) as ctx:
            normalize_thai('')
        self.assertEqual(ctx.exception.code, 'invalid_thai')
        with self.assertRaises(PpayError):
            normalize_thai('Somchai')

    def test_phone_email_and_ppay_proxy(self):
        self.assertEqual(normalize_email('Somchai@Example.co.th'), 'somchai@example.co.th')
        self.assertEqual(mask_email('somchai@example.co.th'), 's***@example.co.th')
        with self.assertRaises(PpayError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('0812345678'), '+66812345678')
        self.assertEqual(normalize_phone('+66 81 234 5678'), '+66812345678')
        self.assertEqual(mask_phone('+66812345678'), '+66****5678')
        with self.assertRaises(PpayError):
            normalize_phone('0212345678')

    def test_e2e_and_pp1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('promptpay', '20240614', 1), 'PPAY20240614000001')
        self.assertEqual(compose_e2e('smart', '20240614', 2), 'SMRT20240614000002')
        self.assertEqual(compose_e2e('bahtnet', '20240614', 3), 'BAHT20240614000003')
        self.assertEqual(mask_e2e('PPAY20240614000001'), 'PPAY****0001')
        with self.assertRaises(PpayError) as ctx:
            compose_e2e('promptpay', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_pp1(
            rail='promptpay', amount='10000.00', bank_code='014', branch_code='',
            account_last4='7890', e2e_id='PPAY20240614000001', purpose='family',
            value_date='20240614', thai_mask='ส***ด', ofac_hit=False, sequence=1,
        )
        parsed = parse_pp1(line)
        self.assertEqual(parsed['clearing'], 'PPAY')
        self.assertEqual(parsed['currency'], 'THB')
        with self.assertRaises(PpayError) as ctx:
            parse_pp1('<?xml version="1.0"?><PPAY/>')
        self.assertEqual(ctx.exception.code, 'invalid_pp1')
        with self.assertRaises(PpayError) as ctx:
            parse_pp1('1|FAST|THB|10.00|014||7890|PPAY20240614000001|family|20240614|****|N|1|014')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(PpayError) as ctx:
            parse_pp1('1|ZGN|THB|10.00|014||7890|PPAY20240614000001|family|20240614|****|N|1|014')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(PpayError) as ctx:
            parse_pp1('1|PPAY|USD|10.00|014||7890|PPAY20240614000001|family|20240614|****|N|1|014')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_thai_inquiry_outcomes(self):
        hit = resolve_ppay('', '', 'สมชาย ใจดี', bank_code='014', account_number='1234567890')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_ppay('phone', '0812345678', 'สมชาย')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_ppay('email', 'somchai@example.co.th', 'วิชัย ทองดี')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_ppay('phone', '0899998888', 'สมชาย')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, ThaiResolution)
        self.assertNotIn('1234567890', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '014-****7890')
        self.assertEqual(close.to_dict()['proxy_mask'], '+66****5678')

    def test_th_clock_promptpay_247_smart_cutoff_bahtnet(self):
        calendar = ThClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_smart_late = ts(2024, 6, 14, 15, 45)
        friday_baht_late = ts(2024, 6, 14, 16, 30)
        self.assertEqual(calendar.value_date(friday, rail='promptpay').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_smart_late, rail='promptpay').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_smart_late, rail='promptpay')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='smart').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_smart_late, rail='smart').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_smart_late, rail='smart')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='bahtnet').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_baht_late, rail='bahtnet').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='smart').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 4, 13).date(), th_holidays(2024))
        self.assertIn(datetime(2024, 4, 16).date(), th_holidays(2024))
        songkran = ts(2024, 4, 13, 11, 0)
        self.assertEqual(calendar.value_date(songkran, rail='promptpay').isoformat(), '2024-04-13')
        self.assertEqual(calendar.value_date(songkran, rail='smart').isoformat(), '2024-04-17')
        self.assertIn(datetime(2024, 4, 8).date(), th_holidays(2024))


class PpayServiceTests(unittest.TestCase):
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

        policy = PpayPolicy(
            outbound_fee=Decimal('0.00'),
            bahtnet_fee=Decimal('150.00'),
            dual_control_threshold=Decimal('500000.00'),
            thbusd=Decimal('0.028500'),
        )
        self.service = PpayService(
            policy,
            MemoryPpayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=ThClock(),
        )

    def _add(self, nickname='Somchai Mobile', name='Somchai Jaidee', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            thai_name=kwargs.pop('thai_name', 'สมชาย ใจดี'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '0812345678'),
            bank_code=kwargs.pop('bank_code', '014'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '1234567890'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_thai_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('thai_name', payload)
        self.assertEqual(payload['proxy_mask'], '+66****5678')
        self.assertEqual(payload['account_last4'], '7890')
        self.assertEqual(payload['thai_mask'], 'ส***ด')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('0812345678', str(snap))
        self.assertNotIn('1234567890', str(snap))
        self.assertNotIn('สมชาย ใจดี', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '285.00')
        self.assertTrue(payment.e2e_id.startswith('PPAY20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('PPAY'))
        self.assertNotIn('0812345678', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '285.00', 'promptpay to Somchai Mobile'))

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

    def test_smart_after_cutoff_queues_bahtnet_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 15, 45)
        bene = self._add(
            nickname='KBank RTGS', proxy_type='', proxy='',
            bank_code='004', account_number='9876543210',
            thai_name='สมหญิง รักไทย', legal_name='Somying Rakthai',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', rail='smart', trace_id='late-smart',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 16, 30)
        baht, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80000.00', rail='bahtnet', trace_id='late-baht',
        )
        self.assertEqual(baht.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-smart'], 'sent')
        self.assertEqual(statuses['late-baht'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('1425.00', usd_amounts)
        self.assertIn('2280.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='004',
            account_number='9876543210', thai_name='สมหญิง รักไทย',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='promptpay', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(PpayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '285.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='500000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(PpayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '14250.00')

    def test_thai_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', thai_name='วิชัย ทองดี')
        with self.assertRaises(PpayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='thai-no',
            )
        self.assertEqual(ctx.exception.code, 'thai_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='thai-yes',
            thai_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.thai_outcome, 'match')

    def test_credit_account_pause_promptpay_cap_and_bahtnet_currency(self):
        bene = self._add()
        with self.assertRaises(PpayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(PpayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='2000000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(PpayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='bahtnet', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(PpayError) as ctx:
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
        with self.assertRaises(PpayError) as ctx:
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
        with self.assertRaises(PpayError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '285.00')

    def test_sqlite_roundtrip_masks_thai_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePpayStore(path)
            service = PpayService(
                PpayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=ThClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='SCB Somchai', legal_name='Somchai Jaidee', thai_name='สมชาย ใจดี',
                default_account='1001', proxy_type='', proxy='',
                bank_code='014', account_number='1234567890',
            )
            reloaded = SqlitePpayStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '1234567890')
            self.assertEqual(reloaded.thai_name, 'สมชาย ใจดี')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('thai_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['thai_mask'], 'ส***ด')
            self.assertEqual(reloaded.to_dict()['account_last4'], '7890')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
