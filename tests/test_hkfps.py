import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.hkfps import (
    AmountError,
    FpsError,
    FpsPolicy,
    FpsService,
    HkClock,
    MemoryFpsStore,
    SqliteFpsStore,
    compose_e2e,
    compose_fps1,
    compose_hkid,
    convert_instructed_to_usd,
    hk_holidays,
    hkid_check_ok,
    mask_e2e,
    mask_email,
    mask_hkid,
    mask_phone,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_fpsid,
    normalize_hkid,
    normalize_phone,
    parse_fps1,
    parse_money,
    resolve_fps,
)

HKT = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=HKT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(convert_instructed_to_usd(Decimal('100.00'), Decimal('0.13')), Decimal('13.00'))

    def test_hkid_checksum_and_mask(self):
        self.assertTrue(hkid_check_ok('A1234563'))
        self.assertTrue(hkid_check_ok('A123456(3)'))
        self.assertEqual(compose_hkid('A', '123456'), 'A1234563')
        self.assertEqual(normalize_hkid('a-123456-(3)'), 'A1234563')
        self.assertEqual(mask_hkid('A1234563'), 'A****563')
        with self.assertRaises(FpsError) as ctx:
            normalize_hkid('A1234560')
        self.assertEqual(ctx.exception.code, 'invalid_hkid')

    def test_fpsid_email_phone_and_hkma_bank(self):
        self.assertEqual(normalize_fpsid('1234567'), '1234567')
        with self.assertRaises(FpsError) as ctx:
            normalize_fpsid('0000000')
        self.assertEqual(ctx.exception.code, 'invalid_fpsid')
        self.assertEqual(normalize_email('Ada@Example.HK'), 'ada@example.hk')
        self.assertEqual(mask_email('ada@example.hk'), 'a***@example.hk')
        with self.assertRaises(FpsError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('91234567'), '+85291234567')
        self.assertEqual(normalize_phone('+852 9123 4567'), '+85291234567')
        self.assertEqual(mask_phone('+85291234567'), '+852****4567')
        with self.assertRaises(FpsError):
            normalize_phone('21234567')
        self.assertEqual(normalize_bank_code('4'), '004')
        with self.assertRaises(FpsError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_e2e_and_fps1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('fps', '20240614', 1), 'FPS20240614000001')
        self.assertEqual(compose_e2e('chats', '20240614', 2), 'CHATS20240614000002')
        self.assertEqual(mask_e2e('FPS20240614000001'), 'FPS****0001')
        with self.assertRaises(FpsError) as ctx:
            compose_e2e('fps', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_fps1(
            rail='fps', amount='10.00', bank_code='004', account_last4='4567',
            e2e_id='FPS20240614000001', purpose='family', value_date='20240614',
            proxy_type='phone', proxy_mask='+852****4567', ofac_hit=False, sequence=1,
        )
        parsed = parse_fps1(line)
        self.assertEqual(parsed['clearing'], 'FPS')
        self.assertEqual(parsed['currency'], 'HKD')
        with self.assertRaises(FpsError) as ctx:
            parse_fps1('<?xml version="1.0"?><FPS/>')
        self.assertEqual(ctx.exception.code, 'invalid_fps1')
        with self.assertRaises(FpsError) as ctx:
            parse_fps1('1|FAST|HKD|10.00|004|4567|FPS20240614000001|family|20240614|none|****|N|1|099')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(FpsError) as ctx:
            parse_fps1('1|FEDWIRE|HKD|10.00|004|4567|FPS20240614000001|family|20240614|none|****|N|1|099')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(FpsError) as ctx:
            parse_fps1('1|CHATS|CNY|10.00|004|4567|CHATS20240614000001|family|20240614|none|****|N|1|099')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_confirmation_of_payee_outcomes(self):
        hit = resolve_fps('hkid', 'A1234563', 'Ada Lovelace')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_fps('phone', '91234567', 'Lovelace')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_fps('email', 'ada@example.hk', 'Blocked Person')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_fps('phone', '98887777', 'Ada')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertNotIn('A1234563', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], 'A****563')
        self.assertEqual(close.to_dict()['proxy_mask'], '+852****4567')

    def test_hk_clock_fps_247_chats_cutoff_ecg_next_day(self):
        calendar = HkClock(chats_cutoff_hour=18, chats_cutoff_minute=0)
        friday = ts(2024, 6, 14, 16, 0)
        friday_late = ts(2024, 6, 14, 18, 30)
        self.assertEqual(calendar.value_date(friday, rail='fps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='fps').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_late, rail='fps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='chats').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='chats').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_late, rail='chats')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='ecg').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='chats').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 7, 1).date(), hk_holidays(2024))
        establishment = ts(2024, 7, 1, 11, 0)
        self.assertEqual(calendar.value_date(establishment, rail='fps').isoformat(), '2024-07-01')
        self.assertEqual(calendar.value_date(establishment, rail='chats').isoformat(), '2024-07-02')
        self.assertTrue(calendar.is_cny_night(friday_late))
        self.assertFalse(calendar.is_cny_night(friday))


class FpsServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
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

        policy = FpsPolicy(
            outbound_fee=Decimal('0.00'),
            chats_fee=Decimal('200.00'),
            dual_control_threshold=Decimal('10000.00'),
            hkdusd=Decimal('0.13'),
            cnyusd=Decimal('0.14'),
        )
        self.service = FpsService(
            policy,
            MemoryFpsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=HkClock(chats_cutoff_hour=18, chats_cutoff_minute=0),
        )

    def _add(self, nickname='Ada Mobile', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '91234567'),
            bank_code=kwargs.pop('bank_code', '004'),
            account_number=kwargs.pop('account_number', '123456789012'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_proxy_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['proxy_mask'], '+852****4567')
        self.assertEqual(payload['account_last4'], '9012')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('91234567', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '13.00')
        self.assertTrue(payment.e2e_id.startswith('FPS20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('FPS'))
        self.assertNotIn('91234567', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '13.00', 'fps to Ada Mobile'))

    def test_same_day_send_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len(self.debits), 1)

    def test_chats_after_cutoff_queues_ecg_always_queues(self):
        self.now[0] = ts(2024, 6, 14, 18, 30)
        bene = self._add(nickname='HSBC FPS', proxy_type='', proxy='', bank_code='004', account_number='9988776655')
        chats, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', rail='chats', trace_id='late-chats',
        )
        self.assertEqual(chats.status, 'queued')
        self.assertEqual(chats.value_date, '20240617')
        self.assertEqual(self.debits, [])
        ecg, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='12.00', rail='ecg', trace_id='ecg-1',
        )
        self.assertEqual(ecg.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-chats'], 'sent')
        self.assertEqual(statuses['ecg-1'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('6.50', usd_amounts)
        self.assertIn('1.56', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='004', account_number='9988776655',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', rail='fps', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(FpsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '10.40')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(FpsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '1300.00')

    def test_cop_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else')
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='cop-no',
            )
        self.assertEqual(ctx.exception.code, 'cop_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='20.00', trace_id='cop-yes',
            cop_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.cop_outcome, 'match')

    def test_credit_account_pause_fps_cap_and_cny_night(self):
        bene = self._add()
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='1000000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        self.now[0] = ts(2024, 6, 14, 19, 0)
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='200000.01',
                currency='CNY', trace_id='cny-night',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                rail='chats', currency='CNY',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')

    def test_nsf_does_not_assign_e2e(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(FpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='30.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].e2e_id, '')

    def test_customer_cannot_cancel_completed_staff_can_recall(self):
        bene = self._add()
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'completed')
        with self.assertRaises(FpsError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '2.86')

    def test_sqlite_roundtrip_masks_hkid(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteFpsStore(path)
            service = FpsService(
                FpsPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=HkClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='HKID Ada', legal_name='Ada Lovelace', default_account='1001',
                proxy_type='hkid', proxy='A1234563',
            )
            reloaded = SqliteFpsStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.proxy, 'A1234563')
            self.assertNotIn('proxy', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['proxy_mask'], 'A****563')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
