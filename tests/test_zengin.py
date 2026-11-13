import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.zengin import (
    AmountError,
    JpClock,
    KanaResolution,
    MemoryZenginStore,
    SqliteZenginStore,
    ZenginError,
    ZenginPolicy,
    ZenginService,
    compose_e2e,
    compose_zgn1,
    convert_instructed_to_usd,
    jp_holidays,
    mask_e2e,
    mask_email,
    mask_kana,
    mask_phone,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_kana,
    normalize_phone,
    parse_fx_rate,
    parse_money,
    parse_zgn1,
    rate_str,
    resolve_zengin,
    zengin_ok,
)

JST = timezone(timedelta(hours=9))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=JST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.0067'), Decimal('0.0067'))
        self.assertEqual(rate_str(Decimal('0.0067')), '0.0067')
        self.assertEqual(convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.0067')), Decimal('67.00'))
        previous = os.environ.get('ZENGIN_FX_JPYUSD')
        os.environ['ZENGIN_FX_JPYUSD'] = '0.0067'
        try:
            self.assertEqual(ZenginPolicy.from_env().jpyusd, Decimal('0.0067'))
        finally:
            if previous is None:
                os.environ.pop('ZENGIN_FX_JPYUSD', None)
            else:
                os.environ['ZENGIN_FX_JPYUSD'] = previous

    def test_zengin_bank_branch_account(self):
        self.assertTrue(zengin_ok('0001', '001', '1234567'))
        self.assertTrue(zengin_ok('0005', '101', '0000001'))
        self.assertTrue(zengin_ok('0009', '201', '7654321'))
        self.assertFalse(zengin_ok('0000', '001', '1234567'))
        self.assertEqual(normalize_bank_code('1'), '0001')
        with self.assertRaises(ZenginError) as ctx:
            normalize_bank_code('0000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_kana_fold_and_mask(self):
        self.assertEqual(normalize_kana('やまだたろう'), 'ヤマダタロウ')
        self.assertEqual(normalize_kana('ヤマダ タロウ'), 'ヤマダタロウ')
        self.assertEqual(mask_kana('ヤマダタロウ'), 'ヤ***ウ')
        with self.assertRaises(ZenginError) as ctx:
            normalize_kana('')
        self.assertEqual(ctx.exception.code, 'invalid_kana')

    def test_phone_email_and_zengin_proxy(self):
        self.assertEqual(normalize_email('Taro@Example.JP'), 'taro@example.jp')
        self.assertEqual(mask_email('taro@example.jp'), 't***@example.jp')
        with self.assertRaises(ZenginError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('09011122222'), '+819011122222')
        self.assertEqual(normalize_phone('+81 90 1112 2222'), '+819011122222')
        self.assertEqual(mask_phone('+819011122222'), '+81****2222')
        with self.assertRaises(ZenginError):
            normalize_phone('0312345678')

    def test_e2e_and_zgn1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('moretime', '20240614', 1), 'MTS20240614000001')
        self.assertEqual(compose_e2e('zengin', '20240614', 2), 'ZGN20240614000002')
        self.assertEqual(compose_e2e('bojnet', '20240614', 3), 'BOJ20240614000003')
        self.assertEqual(mask_e2e('MTS20240614000001'), 'MTS****0001')
        with self.assertRaises(ZenginError) as ctx:
            compose_e2e('moretime', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_zgn1(
            rail='moretime', amount='10000.00', bank_code='0001', branch_code='001',
            account_last4='4567', e2e_id='MTS20240614000001', purpose='family',
            value_date='20240614', kana_mask='ヤ***ウ', ofac_hit=False, sequence=1,
        )
        parsed = parse_zgn1(line)
        self.assertEqual(parsed['clearing'], 'MTS')
        self.assertEqual(parsed['currency'], 'JPY')
        with self.assertRaises(ZenginError) as ctx:
            parse_zgn1('<?xml version="1.0"?><ZGN/>')
        self.assertEqual(ctx.exception.code, 'invalid_zgn1')
        with self.assertRaises(ZenginError) as ctx:
            parse_zgn1('1|FAST|JPY|10.00|0001|001|4567|MTS20240614000001|family|20240614|****|N|1|0398')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(ZenginError) as ctx:
            parse_zgn1('1|FEDWIRE|JPY|10.00|0001|001|4567|MTS20240614000001|family|20240614|****|N|1|0398')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(ZenginError) as ctx:
            parse_zgn1('1|ZGN|USD|10.00|0001|001|4567|ZGN20240614000001|family|20240614|****|N|1|0398')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_kana_inquiry_outcomes(self):
        hit = resolve_zengin('', '', 'ヤマダタロウ', bank_code='0001', branch_code='001', account_number='1234567')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_zengin('phone', '09011122222', 'ヤマダ')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_zengin('email', 'taro@example.jp', 'サトウハナコ')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_zengin('phone', '08099988888', 'ヤマダ')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, KanaResolution)
        self.assertNotIn('1234567', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '0001-001-****4567')
        self.assertEqual(close.to_dict()['proxy_mask'], '+81****2222')

    def test_jp_clock_moretime_247_zengin_cutoff_bojnet(self):
        calendar = JpClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_late = ts(2024, 6, 14, 16, 0)
        friday_night = ts(2024, 6, 14, 19, 30)
        self.assertEqual(calendar.value_date(friday, rail='moretime').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='moretime').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_late, rail='moretime')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='zengin').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='zengin').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_late, rail='zengin')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='bojnet').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_night, rail='bojnet').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='zengin').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 5, 3).date(), jp_holidays(2024))
        constitution = ts(2024, 5, 3, 11, 0)
        self.assertEqual(calendar.value_date(constitution, rail='moretime').isoformat(), '2024-05-03')
        self.assertEqual(calendar.value_date(constitution, rail='zengin').isoformat(), '2024-05-07')
        self.assertIn(datetime(2024, 1, 8).date(), jp_holidays(2024))


class ZenginServiceTests(unittest.TestCase):
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

        policy = ZenginPolicy(
            outbound_fee=Decimal('0.00'),
            bojnet_fee=Decimal('2500.00'),
            dual_control_threshold=Decimal('1000000.00'),
            jpyusd=Decimal('0.0067'),
        )
        self.service = ZenginService(
            policy,
            MemoryZenginStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=JpClock(),
        )

    def _add(self, nickname='Yamada Mobile', name='Yamada Taro', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            kana_name=kwargs.pop('kana_name', 'ヤマダタロウ'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '09011122222'),
            bank_code=kwargs.pop('bank_code', '0001'),
            branch_code=kwargs.pop('branch_code', '001'),
            account_number=kwargs.pop('account_number', '1234567'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_kana_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('kana_name', payload)
        self.assertEqual(payload['proxy_mask'], '+81****2222')
        self.assertEqual(payload['account_last4'], '4567')
        self.assertEqual(payload['kana_mask'], 'ヤ***ウ')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('09011122222', str(snap))
        self.assertNotIn('1234567', str(snap))
        self.assertNotIn('ヤマダタロウ', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '67.00')
        self.assertTrue(payment.e2e_id.startswith('MTS20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('MTS'))
        self.assertNotIn('09011122222', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '67.00', 'zengin to Yamada Mobile'))

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

    def test_zengin_after_cutoff_queues_bojnet_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='MUFG RTGS', proxy_type='', proxy='',
            bank_code='0005', branch_code='101', account_number='0000001',
            kana_name='サトウハナコ', legal_name='Sato Hanako',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', rail='zengin', trace_id='late-zgn',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 19, 30)
        boj, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80000.00', rail='bojnet', trace_id='late-boj',
        )
        self.assertEqual(boj.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-zgn'], 'sent')
        self.assertEqual(statuses['late-boj'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('335.00', usd_amounts)
        self.assertIn('536.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='0005', branch_code='101',
            account_number='0000001', kana_name='サトウハナコ',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='moretime', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(ZenginError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '67.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='1000000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(ZenginError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '6700.00')

    def test_kana_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', kana_name='スズキイチロウ')
        with self.assertRaises(ZenginError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='kana-no',
            )
        self.assertEqual(ctx.exception.code, 'kana_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='kana-yes',
            kana_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.kana_outcome, 'match')

    def test_credit_account_pause_moretime_cap_and_bojnet_currency(self):
        bene = self._add()
        with self.assertRaises(ZenginError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(ZenginError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(ZenginError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='bojnet', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(ZenginError) as ctx:
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
        with self.assertRaises(ZenginError) as ctx:
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
        with self.assertRaises(ZenginError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '67.00')

    def test_sqlite_roundtrip_masks_kana_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteZenginStore(path)
            service = ZenginService(
                ZenginPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=JpClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Mizuho Taro', legal_name='Yamada Taro', kana_name='ヤマダタロウ',
                default_account='1001', proxy_type='', proxy='',
                bank_code='0001', branch_code='001', account_number='1234567',
            )
            reloaded = SqliteZenginStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '1234567')
            self.assertEqual(reloaded.kana_name, 'ヤマダタロウ')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('kana_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['kana_mask'], 'ヤ***ウ')
            self.assertEqual(reloaded.to_dict()['account_last4'], '4567')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
