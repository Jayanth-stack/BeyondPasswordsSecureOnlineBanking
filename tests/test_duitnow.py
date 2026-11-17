import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.duitnow import (
    AmountError,
    MemoryDuitnowStore,
    DuitnowError,
    DuitnowPolicy,
    DuitnowService,
    SqliteDuitnowStore,
    MalayResolution,
    MyClock,
    nric_ok,
    compose_e2e,
    compose_dn1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_malay,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_malay,
    parse_fx_rate,
    parse_money,
    parse_dn1,
    duitnow_ok,
    rate_str,
    resolve_duitnow,
    my_holidays,
)

MYT = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MYT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.214500'), Decimal('0.214500'))
        self.assertEqual(rate_str(Decimal('0.214500')), '0.2145')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.214500')),
            Decimal('2145.00'),
        )
        previous = os.environ.get('DUITNOW_FX_MYRUSD')
        os.environ['DUITNOW_FX_MYRUSD'] = '0.214500'
        try:
            self.assertEqual(DuitnowPolicy.from_env().myrusd, Decimal('0.214500'))
        finally:
            if previous is None:
                os.environ.pop('DUITNOW_FX_MYRUSD', None)
            else:
                os.environ['DUITNOW_FX_MYRUSD'] = previous

    def test_paynet_bank_and_account(self):
        self.assertTrue(duitnow_ok('012', '1234567890'))
        self.assertTrue(duitnow_ok('014', '9876543210'))
        self.assertTrue(duitnow_ok('029', '1111222233'))
        self.assertFalse(duitnow_ok('000', '1234567890'))
        self.assertFalse(duitnow_ok('012', '1234'))
        self.assertEqual(normalize_bank_code('12'), '012')
        with self.assertRaises(DuitnowError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_nric_date_and_state(self):
        self.assertTrue(nric_ok('900101145679'))
        self.assertFalse(nric_ok('900231145679'))
        self.assertFalse(nric_ok('900101001234'))
        self.assertFalse(nric_ok('123'))

    def test_malay_nfc_and_mask(self):
        self.assertEqual(normalize_malay('Ahmad  bin  Abdullah'), 'Ahmad bin Abdullah')
        self.assertEqual(normalize_malay('Ahmad bin Abdullah'), 'Ahmad bin Abdullah')
        self.assertEqual(mask_malay('Ahmad bin Abdullah'), 'A***h')
        with self.assertRaises(DuitnowError) as ctx:
            normalize_malay('')
        self.assertEqual(ctx.exception.code, 'invalid_malay')
        with self.assertRaises(DuitnowError):
            normalize_malay('A')

    def test_phone_email_and_duit_proxy(self):
        self.assertEqual(normalize_email('Ahmad@Example.my'), 'ahmad@example.my')
        self.assertEqual(mask_email('ahmad@example.my'), 'a***@example.my')
        with self.assertRaises(DuitnowError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('0123456789'), '+60123456789')
        self.assertEqual(normalize_phone('+60 12 345 6789'), '+60123456789')
        self.assertEqual(mask_phone('+60123456789'), '+60****6789')
        with self.assertRaises(DuitnowError):
            normalize_phone('0321234567')

    def test_e2e_and_dn1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('duitnow', '20240614', 1), 'DUIT20240614000001')
        self.assertEqual(compose_e2e('ibg', '20240614', 2), 'IBGX20240614000002')
        self.assertEqual(compose_e2e('rentas', '20240614', 3), 'RNTS20240614000003')
        self.assertEqual(mask_e2e('DUIT20240614000001'), 'DUIT****0001')
        with self.assertRaises(DuitnowError) as ctx:
            compose_e2e('duitnow', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_dn1(
            rail='duitnow', amount='10000.00', bank_code='012', branch_code='',
            account_last4='7890', e2e_id='DUIT20240614000001', purpose='family',
            value_date='20240614', malay_mask='A***h', ofac_hit=False, sequence=1,
        )
        parsed = parse_dn1(line)
        self.assertEqual(parsed['clearing'], 'DUIT')
        self.assertEqual(parsed['currency'], 'MYR')
        with self.assertRaises(DuitnowError) as ctx:
            parse_dn1('<?xml version="1.0"?><DUIT/>')
        self.assertEqual(ctx.exception.code, 'invalid_dn1')
        with self.assertRaises(DuitnowError) as ctx:
            parse_dn1('1|PPAY|MYR|10.00|012||7890|DUIT20240614000001|family|20240614|****|N|1|012')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(DuitnowError) as ctx:
            parse_dn1('1|ZGN|MYR|10.00|012||7890|DUIT20240614000001|family|20240614|****|N|1|012')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(DuitnowError) as ctx:
            parse_dn1('1|DUIT|USD|10.00|012||7890|DUIT20240614000001|family|20240614|****|N|1|012')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_malay_inquiry_outcomes(self):
        hit = resolve_duitnow('', '', 'Ahmad bin Abdullah', bank_code='012', account_number='1234567890')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_duitnow('phone', '0123456789', 'Ahmad')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_duitnow('email', 'ahmad@example.my', 'Siti binti Hassan')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_duitnow('phone', '0199998888', 'Ahmad')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, MalayResolution)
        self.assertNotIn('1234567890', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '012-****7890')
        self.assertEqual(close.to_dict()['proxy_mask'], '+60****6789')
        self.assertEqual(hit.to_dict()['registered_mask'], 'A***h')

    def test_my_clock_duitnow_247_ibg_cutoff_rentas(self):
        calendar = MyClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_ibg_late = ts(2024, 6, 14, 16, 30)
        friday_rentas_late = ts(2024, 6, 14, 16, 30)
        self.assertEqual(calendar.value_date(friday, rail='duitnow').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_ibg_late, rail='duitnow').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_ibg_late, rail='duitnow')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='ibg').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_ibg_late, rail='ibg').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_ibg_late, rail='ibg')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='rentas').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_rentas_late, rail='rentas').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='ibg').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 8, 31).date(), my_holidays(2024))
        self.assertIn(datetime(2024, 9, 2).date(), my_holidays(2024))
        national = ts(2024, 8, 31, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='duitnow').isoformat(), '2024-08-31')
        self.assertEqual(calendar.value_date(national, rail='ibg').isoformat(), '2024-09-03')
        self.assertIn(datetime(2022, 5, 2).date(), my_holidays(2022))


class DuitnowServiceTests(unittest.TestCase):
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

        policy = DuitnowPolicy(
            outbound_fee=Decimal('0.00'),
            rentas_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('50000.00'),
            myrusd=Decimal('0.214500'),
        )
        self.service = DuitnowService(
            policy,
            MemoryDuitnowStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=MyClock(),
        )

    def _add(self, nickname='Ahmad Mobile', name='Ahmad Abdullah', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            malay_name=kwargs.pop('malay_name', 'Ahmad bin Abdullah'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '0123456789'),
            bank_code=kwargs.pop('bank_code', '012'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '1234567890'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_malay_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('malay_name', payload)
        self.assertEqual(payload['proxy_mask'], '+60****6789')
        self.assertEqual(payload['account_last4'], '7890')
        self.assertEqual(payload['malay_mask'], 'A***h')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('0123456789', str(snap))
        self.assertNotIn('1234567890', str(snap))
        self.assertNotIn('Ahmad bin Abdullah', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '2145.00')
        self.assertTrue(payment.e2e_id.startswith('DUIT20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('DUIT'))
        self.assertNotIn('0123456789', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '2145.00', 'duitnow to Ahmad Mobile'))

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

    def test_ibg_after_cutoff_queues_rentas_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 30)
        bene = self._add(
            nickname='CIMB RTGS', proxy_type='', proxy='',
            bank_code='014', account_number='9876543210',
            malay_name='Siti binti Hassan', legal_name='Siti Hassan',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='ibg', trace_id='late-ibg',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 16, 30)
        rentas, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='rentas', trace_id='late-rentas',
        )
        self.assertEqual(rentas.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-ibg'], 'sent')
        self.assertEqual(statuses['late-rentas'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('7078.50', usd_amounts)
        self.assertIn('8580.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='014',
            account_number='9876543210', malay_name='Siti binti Hassan',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='duitnow', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(DuitnowError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '2145.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(DuitnowError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10725.00')

    def test_malay_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', malay_name='Wong Wei Ming')
        with self.assertRaises(DuitnowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='malay-no',
            )
        self.assertEqual(ctx.exception.code, 'malay_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='malay-yes',
            malay_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.malay_outcome, 'match')

    def test_credit_account_pause_duitnow_cap_and_rentas_currency(self):
        bene = self._add()
        with self.assertRaises(DuitnowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(DuitnowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(DuitnowError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='rentas', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(DuitnowError) as ctx:
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
        with self.assertRaises(DuitnowError) as ctx:
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
        with self.assertRaises(DuitnowError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '2145.00')

    def test_sqlite_roundtrip_masks_malay_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteDuitnowStore(path)
            service = DuitnowService(
                DuitnowPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=MyClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Maybank Ahmad', legal_name='Ahmad Abdullah', malay_name='Ahmad bin Abdullah',
                default_account='1001', proxy_type='', proxy='',
                bank_code='012', account_number='1234567890',
            )
            reloaded = SqliteDuitnowStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '1234567890')
            self.assertEqual(reloaded.malay_name, 'Ahmad bin Abdullah')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('malay_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['malay_mask'], 'A***h')
            self.assertEqual(reloaded.to_dict()['account_last4'], '7890')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
