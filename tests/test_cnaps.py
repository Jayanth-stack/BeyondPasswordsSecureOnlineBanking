import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.cnaps import (
    AmountError,
    MemoryCnapsStore,
    CnapsError,
    CnapsPolicy,
    CnapsService,
    SqliteCnapsStore,
    ChineseResolution,
    CnClock,
    resident_id_ok,
    compose_e2e,
    compose_cn1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_chinese,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_chinese,
    parse_fx_rate,
    parse_money,
    parse_cn1,
    cnaps_ok,
    rate_str,
    resolve_cnaps,
    cn_holidays,
)

CST = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.137000'), Decimal('0.137000'))
        self.assertEqual(rate_str(Decimal('0.137000')), '0.137')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.137000')),
            Decimal('1370.00'),
        )
        previous = os.environ.get('CNAPS_FX_CNYUSD')
        os.environ['CNAPS_FX_CNYUSD'] = '0.137000'
        try:
            self.assertEqual(CnapsPolicy.from_env().cnyusd, Decimal('0.137000'))
        finally:
            if previous is None:
                os.environ.pop('CNAPS_FX_CNYUSD', None)
            else:
                os.environ['CNAPS_FX_CNYUSD'] = previous

    def test_cnaps_bank_and_account(self):
        self.assertTrue(cnaps_ok('104100000004', '6222021234567890'))
        self.assertTrue(cnaps_ok('102100099996', '9876543210987654'))
        self.assertTrue(cnaps_ok('105100000017', '1111222233334444'))
        self.assertFalse(cnaps_ok('000000000000', '6222021234567890'))
        self.assertFalse(cnaps_ok('104100000004', '1234'))
        self.assertEqual(normalize_bank_code('104-1000-00004'), '104100000004')
        with self.assertRaises(CnapsError) as ctx:
            normalize_bank_code('104100000005')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(CnapsError):
            normalize_bank_code('000')

    def test_resident_id_checksum(self):
        self.assertTrue(resident_id_ok('11010119900307002X'))
        self.assertTrue(resident_id_ok('11010119900307002X'))
        self.assertFalse(resident_id_ok('110101199003070021'))
        self.assertFalse(resident_id_ok('123'))

    def test_chinese_nfc_and_mask(self):
        self.assertEqual(normalize_chinese('张  伟'), '张 伟')
        self.assertEqual(normalize_chinese('张伟'), '张伟')
        self.assertEqual(mask_chinese('张伟'), '张***伟')
        with self.assertRaises(CnapsError) as ctx:
            normalize_chinese('')
        self.assertEqual(ctx.exception.code, 'invalid_chinese')
        with self.assertRaises(CnapsError):
            normalize_chinese('J')

    def test_phone_email_and_insta_proxy(self):
        self.assertEqual(normalize_email('Zhang@Example.cn'), 'zhang@example.cn')
        self.assertEqual(mask_email('zhang@example.cn'), 'z***@example.cn')
        with self.assertRaises(CnapsError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('13812345678'), '+8613812345678')
        self.assertEqual(normalize_phone('+86 138 1234 5678'), '+8613812345678')
        self.assertEqual(mask_phone('+8613812345678'), '+86****5678')
        with self.assertRaises(CnapsError):
            normalize_phone('01012345678')

    def test_e2e_and_ip1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('ibps', '20240614', 1), 'IBPS20240614000001')
        self.assertEqual(compose_e2e('beps', '20240614', 2), 'BEPS20240614000002')
        self.assertEqual(compose_e2e('cips', '20240614', 3), 'CIPS20240614000003')
        self.assertEqual(mask_e2e('IBPS20240614000001'), 'IBPS****0001')
        with self.assertRaises(CnapsError) as ctx:
            compose_e2e('ibps', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_cn1(
            rail='ibps', amount='10000.00', bank_code='104100000004', branch_code='',
            account_last4='7890', e2e_id='IBPS20240614000001', purpose='family',
            value_date='20240614', chinese_mask='张***伟', ofac_hit=False, sequence=1,
        )
        parsed = parse_cn1(line)
        self.assertEqual(parsed['clearing'], 'IBPS')
        self.assertEqual(parsed['currency'], 'CNY')
        with self.assertRaises(CnapsError) as ctx:
            parse_cn1('<?xml version="1.0"?><INST/>')
        self.assertEqual(ctx.exception.code, 'invalid_cn1')
        with self.assertRaises(CnapsError) as ctx:
            parse_cn1('1|PPAY|CNY|10.00|011||7890|IBPS20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(CnapsError) as ctx:
            parse_cn1('1|ZGN|CNY|10.00|011||7890|IBPS20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(CnapsError) as ctx:
            parse_cn1('1|IBPS|USD|10.00|011||7890|IBPS20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_chinese_inquiry_outcomes(self):
        hit = resolve_cnaps('', '', '张伟', bank_code='104100000004', account_number='6222021234567890')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_cnaps('phone', '13812345678', '张')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_cnaps('email', 'zhang@example.cn', '李娜')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_cnaps('phone', '13900001111', '张伟')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, ChineseResolution)
        self.assertNotIn('6222021234567890', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '104-****7890')
        self.assertEqual(close.to_dict()['proxy_mask'], '+86****5678')
        self.assertEqual(hit.to_dict()['registered_mask'], '张***伟')

    def test_cn_clock_ibps_247_beps_cutoff_cips(self):
        calendar = CnClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_peso_late = ts(2024, 6, 14, 16, 0)
        friday_pass_late = ts(2024, 6, 14, 17, 30)
        self.assertEqual(calendar.value_date(friday, rail='ibps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_peso_late, rail='ibps').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_peso_late, rail='ibps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='beps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_peso_late, rail='beps').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_peso_late, rail='beps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='cips').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_pass_late, rail='cips').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='beps').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 2, 10).date(), cn_holidays(2024))
        self.assertIn(datetime(2024, 2, 17).date(), cn_holidays(2024))
        self.assertIn(datetime(2024, 2, 19).date(), cn_holidays(2024))
        national = ts(2024, 2, 10, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='ibps').isoformat(), '2024-02-10')
        self.assertEqual(calendar.value_date(national, rail='beps').isoformat(), '2024-02-20')


class CnapsServiceTests(unittest.TestCase):
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

        policy = CnapsPolicy(
            outbound_fee=Decimal('0.00'),
            cips_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('50000.00'),
            cnyusd=Decimal('0.137000'),
        )
        self.service = CnapsService(
            policy,
            MemoryCnapsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=CnClock(),
        )

    def _add(self, nickname='Zhang Mobile', name='Zhang Wei', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            chinese_name=kwargs.pop('chinese_name', '张伟'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '13812345678'),
            bank_code=kwargs.pop('bank_code', '104100000004'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '6222021234567890'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_chinese_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('chinese_name', payload)
        self.assertEqual(payload['proxy_mask'], '+86****5678')
        self.assertEqual(payload['account_last4'], '7890')
        self.assertEqual(payload['chinese_mask'], '张***伟')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('13812345678', str(snap))
        self.assertNotIn('6222021234567890', str(snap))
        self.assertNotIn('张伟', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '1370.00')
        self.assertTrue(payment.e2e_id.startswith('IBPS20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('IBPS'))
        self.assertNotIn('13812345678', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '1370.00', 'cnaps to Zhang Mobile'))

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

    def test_beps_after_cutoff_queues_cips_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='ICBC RTGS', proxy_type='', proxy='',
            bank_code='102100099996', account_number='9876543210987654',
            chinese_name='李娜', legal_name='李娜',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='beps', trace_id='late-beps',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 17, 30)
        philpass, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='cips', trace_id='late-cips',
        )
        self.assertEqual(philpass.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-beps'], 'sent')
        self.assertEqual(statuses['late-cips'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('4521.00', usd_amounts)
        self.assertIn('5480.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='102100099996',
            account_number='9876543210987654', chinese_name='李娜',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='ibps', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(CnapsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '1370.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(CnapsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '6850.00')

    def test_chinese_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', chinese_name='王伟')
        with self.assertRaises(CnapsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='chinese-no',
            )
        self.assertEqual(ctx.exception.code, 'chinese_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='chinese-yes',
            chinese_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.chinese_outcome, 'match')

    def test_credit_account_pause_cnaps_cap_and_philpass_currency(self):
        bene = self._add()
        with self.assertRaises(CnapsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(CnapsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(CnapsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='cips', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(CnapsError) as ctx:
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
        with self.assertRaises(CnapsError) as ctx:
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
        with self.assertRaises(CnapsError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '1370.00')

    def test_sqlite_roundtrip_masks_chinese_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCnapsStore(path)
            service = CnapsService(
                CnapsPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=CnClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BOC Zhang', legal_name='Zhang Wei', chinese_name='张伟',
                default_account='1001', proxy_type='', proxy='',
                bank_code='104100000004', account_number='6222021234567890',
            )
            reloaded = SqliteCnapsStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '6222021234567890')
            self.assertEqual(reloaded.chinese_name, '张伟')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('chinese_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['chinese_mask'], '张***伟')
            self.assertEqual(reloaded.to_dict()['account_last4'], '7890')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
