import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.kftc import (
    AmountError,
    HangulResolution,
    KrClock,
    KftcError,
    KftcPolicy,
    KftcService,
    MemoryKftcStore,
    SqliteKftcStore,
    compose_e2e,
    compose_kft1,
    convert_instructed_to_usd,
    kftc_ok,
    kr_holidays,
    mask_e2e,
    mask_email,
    mask_hangul,
    mask_phone,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_hangul,
    normalize_phone,
    parse_fx_rate,
    parse_kft1,
    parse_money,
    rate_str,
    resolve_kftc,
    rrn_check_digit_ok,
)

KST = timezone(timedelta(hours=9))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=KST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.000720'), Decimal('0.000720'))
        self.assertEqual(rate_str(Decimal('0.000720')), '0.00072')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.000720')),
            Decimal('7.20'),
        )
        previous = os.environ.get('KFTC_FX_KRWUSD')
        os.environ['KFTC_FX_KRWUSD'] = '0.000720'
        try:
            self.assertEqual(KftcPolicy.from_env().krwusd, Decimal('0.000720'))
        finally:
            if previous is None:
                os.environ.pop('KFTC_FX_KRWUSD', None)
            else:
                os.environ['KFTC_FX_KRWUSD'] = previous

    def test_kftc_bank_and_account(self):
        self.assertTrue(kftc_ok('004', '12345678901'))
        self.assertTrue(kftc_ok('088', '98765432109'))
        self.assertTrue(kftc_ok('039', '10000000001'))
        self.assertFalse(kftc_ok('000', '12345678901'))
        self.assertFalse(kftc_ok('004', '1234'))
        self.assertEqual(normalize_bank_code('4'), '004')
        with self.assertRaises(KftcError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_rrn_checksum(self):
        self.assertTrue(rrn_check_digit_ok('7901011234567'))
        self.assertFalse(rrn_check_digit_ok('7901011234560'))
        self.assertFalse(rrn_check_digit_ok('123'))

    def test_hangul_nfc_and_mask(self):
        self.assertEqual(normalize_hangul('김 민수'), '김민수')
        self.assertEqual(normalize_hangul('김민수'), '김민수')
        self.assertEqual(mask_hangul('김민수'), '김***수')
        with self.assertRaises(KftcError) as ctx:
            normalize_hangul('')
        self.assertEqual(ctx.exception.code, 'invalid_hangul')
        with self.assertRaises(KftcError):
            normalize_hangul('Kim')

    def test_phone_email_and_kftc_proxy(self):
        self.assertEqual(normalize_email('Minsu@Example.KR'), 'minsu@example.kr')
        self.assertEqual(mask_email('minsu@example.kr'), 'm***@example.kr')
        with self.assertRaises(KftcError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('01012345678'), '+821012345678')
        self.assertEqual(normalize_phone('+82 10 1234 5678'), '+821012345678')
        self.assertEqual(mask_phone('+821012345678'), '+82****5678')
        with self.assertRaises(KftcError):
            normalize_phone('0212345678')

    def test_e2e_and_kft1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('open', '20240614', 1), 'OPEN20240614000001')
        self.assertEqual(compose_e2e('kftc', '20240614', 2), 'EFTC20240614000002')
        self.assertEqual(compose_e2e('bokwire', '20240614', 3), 'BOKW20240614000003')
        self.assertEqual(mask_e2e('OPEN20240614000001'), 'OPEN****0001')
        with self.assertRaises(KftcError) as ctx:
            compose_e2e('open', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_kft1(
            rail='open', amount='10000.00', bank_code='004', branch_code='',
            account_last4='8901', e2e_id='OPEN20240614000001', purpose='family',
            value_date='20240614', hangul_mask='김***수', ofac_hit=False, sequence=1,
        )
        parsed = parse_kft1(line)
        self.assertEqual(parsed['clearing'], 'OPEN')
        self.assertEqual(parsed['currency'], 'KRW')
        with self.assertRaises(KftcError) as ctx:
            parse_kft1('<?xml version="1.0"?><KFT/>')
        self.assertEqual(ctx.exception.code, 'invalid_kft1')
        with self.assertRaises(KftcError) as ctx:
            parse_kft1('1|FAST|KRW|10.00|004||8901|OPEN20240614000001|family|20240614|****|N|1|039')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(KftcError) as ctx:
            parse_kft1('1|ZGN|KRW|10.00|004||8901|OPEN20240614000001|family|20240614|****|N|1|039')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(KftcError) as ctx:
            parse_kft1('1|OPEN|USD|10.00|004||8901|OPEN20240614000001|family|20240614|****|N|1|039')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_hangul_inquiry_outcomes(self):
        hit = resolve_kftc('', '', '김민수', bank_code='004', account_number='12345678901')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_kftc('phone', '01012345678', '김민')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_kftc('email', 'minsu@example.kr', '이서연')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_kftc('phone', '01099998888', '김민')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, HangulResolution)
        self.assertNotIn('12345678901', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '004-****8901')
        self.assertEqual(close.to_dict()['proxy_mask'], '+82****5678')

    def test_kr_clock_open_247_kftc_cutoff_bokwire(self):
        calendar = KrClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_bok_late = ts(2024, 6, 14, 18, 0)
        friday_kftc_late = ts(2024, 6, 14, 22, 30)
        self.assertEqual(calendar.value_date(friday, rail='open').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_kftc_late, rail='open').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_kftc_late, rail='open')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='kftc').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_kftc_late, rail='kftc').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_kftc_late, rail='kftc')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='bokwire').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_bok_late, rail='bokwire').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='kftc').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 3, 1).date(), kr_holidays(2024))
        march1 = ts(2024, 3, 1, 11, 0)
        self.assertEqual(calendar.value_date(march1, rail='open').isoformat(), '2024-03-01')
        self.assertEqual(calendar.value_date(march1, rail='kftc').isoformat(), '2024-03-04')
        self.assertIn(datetime(2024, 5, 6).date(), kr_holidays(2024))


class KftcServiceTests(unittest.TestCase):
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

        policy = KftcPolicy(
            outbound_fee=Decimal('0.00'),
            bokwire_fee=Decimal('3000.00'),
            dual_control_threshold=Decimal('10000000.00'),
            krwusd=Decimal('0.000720'),
        )
        self.service = KftcService(
            policy,
            MemoryKftcStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=KrClock(),
        )

    def _add(self, nickname='Minsu Mobile', name='Kim Minsu', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            hangul_name=kwargs.pop('hangul_name', '김민수'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '01012345678'),
            bank_code=kwargs.pop('bank_code', '004'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '12345678901'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_hangul_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('hangul_name', payload)
        self.assertEqual(payload['proxy_mask'], '+82****5678')
        self.assertEqual(payload['account_last4'], '8901')
        self.assertEqual(payload['hangul_mask'], '김***수')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('01012345678', str(snap))
        self.assertNotIn('12345678901', str(snap))
        self.assertNotIn('김민수', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '7.20')
        self.assertTrue(payment.e2e_id.startswith('OPEN20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('OPEN'))
        self.assertNotIn('01012345678', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '7.20', 'kftc to Minsu Mobile'))

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

    def test_kftc_after_cutoff_queues_bokwire_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 22, 30)
        bene = self._add(
            nickname='Shinhan RTGS', proxy_type='', proxy='',
            bank_code='088', account_number='98765432109',
            hangul_name='이서연', legal_name='Lee Seoyeon',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', rail='kftc', trace_id='late-kftc',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bok, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80000.00', rail='bokwire', trace_id='late-bok',
        )
        self.assertEqual(bok.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-kftc'], 'sent')
        self.assertEqual(statuses['late-bok'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('36.00', usd_amounts)
        self.assertIn('57.60', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='088',
            account_number='98765432109', hangul_name='이서연',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='open', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(KftcError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '7.20')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(KftcError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '7200.00')

    def test_hangul_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', hangul_name='박지훈')
        with self.assertRaises(KftcError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hangul-no',
            )
        self.assertEqual(ctx.exception.code, 'hangul_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hangul-yes',
            hangul_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.hangul_outcome, 'match')

    def test_credit_account_pause_open_cap_and_bokwire_currency(self):
        bene = self._add()
        with self.assertRaises(KftcError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(KftcError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(KftcError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='bokwire', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(KftcError) as ctx:
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
        with self.assertRaises(KftcError) as ctx:
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
        with self.assertRaises(KftcError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '7.20')

    def test_sqlite_roundtrip_masks_hangul_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteKftcStore(path)
            service = KftcService(
                KftcPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=KrClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='KB Minsu', legal_name='Kim Minsu', hangul_name='김민수',
                default_account='1001', proxy_type='', proxy='',
                bank_code='004', account_number='12345678901',
            )
            reloaded = SqliteKftcStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '12345678901')
            self.assertEqual(reloaded.hangul_name, '김민수')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('hangul_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['hangul_mask'], '김***수')
            self.assertEqual(reloaded.to_dict()['account_last4'], '8901')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
