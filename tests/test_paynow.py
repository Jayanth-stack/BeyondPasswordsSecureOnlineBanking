import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.paynow import (
    AmountError,
    MemoryPayNowStore,
    PayNowError,
    PayNowPolicy,
    PayNowService,
    SgClock,
    SqlitePayNowStore,
    compose_e2e,
    compose_fast1,
    compose_nric,
    compose_uen,
    convert_sgd_to_usd,
    mask_e2e,
    mask_nric,
    mask_phone,
    mask_uen,
    money_str,
    nric_check_ok,
    normalize_bank_code,
    normalize_nric,
    normalize_phone,
    normalize_uen,
    parse_fast1,
    parse_money,
    resolve_paynow,
    sg_mas_holidays,
    uen_check_ok,
)

SGT = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=SGT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(convert_sgd_to_usd(Decimal('100.00'), Decimal('0.74')), Decimal('74.00'))

    def test_nric_checksum_and_mask(self):
        self.assertTrue(nric_check_ok('S0000001I'))
        self.assertTrue(nric_check_ok('S1234567D'))
        self.assertEqual(compose_nric('S', '0000001'), 'S0000001I')
        self.assertEqual(compose_nric('T', '0000001'), 'T0000001E')
        self.assertEqual(normalize_nric('s-0000001-i'), 'S0000001I')
        self.assertEqual(mask_nric('S0000001I'), 'S****001I')
        with self.assertRaises(PayNowError) as ctx:
            normalize_nric('S0000001A')
        self.assertEqual(ctx.exception.code, 'invalid_nric')

    def test_uen_checksum_and_mask(self):
        self.assertEqual(compose_uen('12345678'), '12345678E')
        self.assertTrue(uen_check_ok('12345678E'))
        self.assertEqual(normalize_uen('1234-5678-E'), '12345678E')
        self.assertEqual(mask_uen('12345678E'), '12****78E')
        with self.assertRaises(PayNowError) as ctx:
            normalize_uen('12345678A')
        self.assertEqual(ctx.exception.code, 'invalid_uen')

    def test_plus65_mobile_and_mas_bank(self):
        self.assertEqual(normalize_phone('91234567'), '+6591234567')
        self.assertEqual(normalize_phone('+65 9123 4567'), '+6591234567')
        self.assertEqual(mask_phone('+6591234567'), '+65****4567')
        with self.assertRaises(PayNowError):
            normalize_phone('61234567')
        self.assertEqual(normalize_bank_code('7171'), '7171')
        with self.assertRaises(PayNowError) as ctx:
            normalize_bank_code('0000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_e2e_and_fast1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('fast', '20240614', 1), 'FAST20240614000001')
        self.assertEqual(compose_e2e('paynow', '20240614', 2), 'PNW20240614000002')
        self.assertEqual(mask_e2e('FAST20240614000001'), 'FAST****0001')
        with self.assertRaises(PayNowError) as ctx:
            compose_e2e('fast', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_fast1(
            rail='fast', amount='10.00', bank_code='7171', account_last4='4567',
            e2e_id='FAST20240614000001', purpose='family', value_date='20240614',
            proxy_type='phone', proxy_mask='+65****4567', ofac_hit=False, sequence=1,
        )
        parsed = parse_fast1(line)
        self.assertEqual(parsed['clearing'], 'FAST')
        self.assertEqual(parsed['currency'], 'SGD')
        with self.assertRaises(PayNowError) as ctx:
            parse_fast1('<?xml version="1.0"?><FAST/>')
        self.assertEqual(ctx.exception.code, 'invalid_fast1')
        with self.assertRaises(PayNowError) as ctx:
            parse_fast1('1|AUNPP|SGD|10.00|7171|4567|FAST20240614000001|family|20240614|none|****|N|1|9092')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(PayNowError) as ctx:
            parse_fast1('1|FEDWIRE|SGD|10.00|7171|4567|FAST20240614000001|family|20240614|none|****|N|1|9092')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')

    def test_confirmation_of_payee_outcomes(self):
        hit = resolve_paynow('nric', 'S0000001I', 'Ada Lovelace')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_paynow('phone', '91234567', 'Lovelace')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_paynow('uen', '12345678E', 'Blocked Person')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_paynow('phone', '98887777', 'Ada')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertNotIn('S0000001I', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], 'S****001I')
        self.assertEqual(close.to_dict()['proxy_mask'], '+65****4567')

    def test_sg_clock_fast_247_meps_cutoff_giro_next_day(self):
        calendar = SgClock(meps_cutoff_hour=17, meps_cutoff_minute=30)
        friday = ts(2024, 6, 14, 16, 0)
        friday_late = ts(2024, 6, 14, 18, 0)
        self.assertEqual(calendar.value_date(friday, rail='fast').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='fast').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_late, rail='fast')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='meps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='meps').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_late, rail='meps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='giro').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='meps').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 8, 9).date(), sg_mas_holidays(2024))
        national = ts(2024, 8, 9, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='fast').isoformat(), '2024-08-09')
        self.assertEqual(calendar.value_date(national, rail='meps').isoformat(), '2024-08-12')


class PayNowServiceTests(unittest.TestCase):
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

        policy = PayNowPolicy(
            outbound_fee=Decimal('0.00'),
            meps_fee=Decimal('20.00'),
            dual_control_threshold=Decimal('10000.00'),
            sgdusd=Decimal('0.74'),
        )
        self.service = PayNowService(
            policy,
            MemoryPayNowStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SgClock(meps_cutoff_hour=17, meps_cutoff_minute=30),
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
            bank_code=kwargs.pop('bank_code', '7171'),
            account_number=kwargs.pop('account_number', '123456789012'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_proxy_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['proxy_mask'], '+65****4567')
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
        self.assertEqual(payment.usd_amount, '74.00')
        self.assertTrue(payment.e2e_id.startswith('PNW20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('PNW'))
        self.assertNotIn('91234567', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '74.00', 'paynow to Ada Mobile'))

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

    def test_meps_after_cutoff_queues_giro_always_queues(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add(nickname='DBS Fast', proxy_type='', proxy='', bank_code='7171', account_number='9988776655')
        meps, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', rail='meps', trace_id='late-meps',
        )
        self.assertEqual(meps.status, 'queued')
        self.assertEqual(meps.value_date, '20240617')
        self.assertEqual(self.debits, [])
        giro, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='12.00', rail='giro', trace_id='giro-1',
        )
        self.assertEqual(giro.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-meps'], 'sent')
        self.assertEqual(statuses['giro-1'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('37.00', usd_amounts)
        self.assertIn('8.88', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='7171', account_number='9988776655',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', rail='fast', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(PayNowError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '59.20')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(PayNowError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '7400.00')

    def test_cop_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else')
        with self.assertRaises(PayNowError) as ctx:
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

    def test_credit_account_and_pause_and_fast_cap(self):
        bene = self._add()
        with self.assertRaises(PayNowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(PayNowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='200000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(PayNowError) as ctx:
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
        with self.assertRaises(PayNowError) as ctx:
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
        with self.assertRaises(PayNowError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '16.28')

    def test_sqlite_roundtrip_masks_nric(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePayNowStore(path)
            service = PayNowService(
                PayNowPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=SgClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='NRIC Ada', legal_name='Ada Lovelace', default_account='1001',
                proxy_type='nric', proxy='S0000001I',
            )
            reloaded = SqlitePayNowStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.proxy, 'S0000001I')
            self.assertNotIn('proxy', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['proxy_mask'], 'S****001I')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
