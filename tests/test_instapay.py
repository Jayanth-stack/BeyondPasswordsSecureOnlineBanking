import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.instapay import (
    AmountError,
    MemoryInstapayStore,
    InstapayError,
    InstapayPolicy,
    InstapayService,
    SqliteInstapayStore,
    FilipinoResolution,
    PhClock,
    tin_ok,
    compose_e2e,
    compose_ip1,
    convert_instructed_to_usd,
    mask_e2e,
    mask_email,
    mask_phone,
    mask_filipino,
    money_str,
    normalize_bank_code,
    normalize_email,
    normalize_phone,
    normalize_filipino,
    parse_fx_rate,
    parse_money,
    parse_ip1,
    instapay_ok,
    rate_str,
    resolve_instapay,
    ph_holidays,
)

PHT = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PHT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(parse_fx_rate('0.017500'), Decimal('0.017500'))
        self.assertEqual(rate_str(Decimal('0.017500')), '0.0175')
        self.assertEqual(
            convert_instructed_to_usd(Decimal('10000.00'), Decimal('0.017500')),
            Decimal('175.00'),
        )
        previous = os.environ.get('INSTAPAY_FX_PHPUSD')
        os.environ['INSTAPAY_FX_PHPUSD'] = '0.017500'
        try:
            self.assertEqual(InstapayPolicy.from_env().phpusd, Decimal('0.017500'))
        finally:
            if previous is None:
                os.environ.pop('INSTAPAY_FX_PHPUSD', None)
            else:
                os.environ['INSTAPAY_FX_PHPUSD'] = previous

    def test_pchc_bank_and_account(self):
        self.assertTrue(instapay_ok('011', '1234567890'))
        self.assertTrue(instapay_ok('010', '9876543210'))
        self.assertTrue(instapay_ok('032', '1111222233'))
        self.assertFalse(instapay_ok('000', '1234567890'))
        self.assertFalse(instapay_ok('011', '1234'))
        self.assertEqual(normalize_bank_code('11'), '011')
        with self.assertRaises(InstapayError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')

    def test_tin_bir_checksum(self):
        self.assertTrue(tin_ok('123456782'))
        self.assertTrue(tin_ok('123456782000'))
        self.assertFalse(tin_ok('123456781'))
        self.assertFalse(tin_ok('123'))

    def test_filipino_nfc_and_mask(self):
        self.assertEqual(normalize_filipino('Juan  dela  Cruz'), 'Juan dela Cruz')
        self.assertEqual(normalize_filipino('Juan dela Cruz'), 'Juan dela Cruz')
        self.assertEqual(mask_filipino('Juan dela Cruz'), 'J***z')
        with self.assertRaises(InstapayError) as ctx:
            normalize_filipino('')
        self.assertEqual(ctx.exception.code, 'invalid_filipino')
        with self.assertRaises(InstapayError):
            normalize_filipino('J')

    def test_phone_email_and_insta_proxy(self):
        self.assertEqual(normalize_email('Juan@Example.ph'), 'juan@example.ph')
        self.assertEqual(mask_email('juan@example.ph'), 'j***@example.ph')
        with self.assertRaises(InstapayError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('09171234567'), '+639171234567')
        self.assertEqual(normalize_phone('+63 917 123 4567'), '+639171234567')
        self.assertEqual(mask_phone('+639171234567'), '+63****4567')
        with self.assertRaises(InstapayError):
            normalize_phone('0321234567')

    def test_e2e_and_ip1_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('instapay', '20240614', 1), 'INST20240614000001')
        self.assertEqual(compose_e2e('pesonet', '20240614', 2), 'PESO20240614000002')
        self.assertEqual(compose_e2e('philpass', '20240614', 3), 'PASS20240614000003')
        self.assertEqual(mask_e2e('INST20240614000001'), 'INST****0001')
        with self.assertRaises(InstapayError) as ctx:
            compose_e2e('instapay', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_ip1(
            rail='instapay', amount='10000.00', bank_code='011', branch_code='',
            account_last4='7890', e2e_id='INST20240614000001', purpose='family',
            value_date='20240614', filipino_mask='J***z', ofac_hit=False, sequence=1,
        )
        parsed = parse_ip1(line)
        self.assertEqual(parsed['clearing'], 'INST')
        self.assertEqual(parsed['currency'], 'PHP')
        with self.assertRaises(InstapayError) as ctx:
            parse_ip1('<?xml version="1.0"?><INST/>')
        self.assertEqual(ctx.exception.code, 'invalid_ip1')
        with self.assertRaises(InstapayError) as ctx:
            parse_ip1('1|PPAY|PHP|10.00|011||7890|INST20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(InstapayError) as ctx:
            parse_ip1('1|ZGN|PHP|10.00|011||7890|INST20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(InstapayError) as ctx:
            parse_ip1('1|INST|USD|10.00|011||7890|INST20240614000001|family|20240614|****|N|1|011')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_filipino_inquiry_outcomes(self):
        hit = resolve_instapay('', '', 'Juan dela Cruz', bank_code='011', account_number='1234567890')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_instapay('phone', '09171234567', 'Juan')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_instapay('email', 'juan@example.ph', 'Maria Santos')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_instapay('phone', '09179998888', 'Juan')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, FilipinoResolution)
        self.assertNotIn('1234567890', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '011-****7890')
        self.assertEqual(close.to_dict()['proxy_mask'], '+63****4567')
        self.assertEqual(hit.to_dict()['registered_mask'], 'J***z')

    def test_ph_clock_instapay_247_pesonet_cutoff_philpass(self):
        calendar = PhClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_peso_late = ts(2024, 6, 14, 16, 0)
        friday_pass_late = ts(2024, 6, 14, 17, 30)
        self.assertEqual(calendar.value_date(friday, rail='instapay').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_peso_late, rail='instapay').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_peso_late, rail='instapay')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='pesonet').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_peso_late, rail='pesonet').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_peso_late, rail='pesonet')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='philpass').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_pass_late, rail='philpass').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='pesonet').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 8, 26).date(), ph_holidays(2024))
        self.assertIn(datetime(2024, 11, 30).date(), ph_holidays(2024))
        self.assertIn(datetime(2024, 12, 2).date(), ph_holidays(2024))
        national = ts(2024, 11, 30, 11, 0)
        self.assertEqual(calendar.value_date(national, rail='instapay').isoformat(), '2024-11-30')
        self.assertEqual(calendar.value_date(national, rail='pesonet').isoformat(), '2024-12-03')


class InstapayServiceTests(unittest.TestCase):
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

        policy = InstapayPolicy(
            outbound_fee=Decimal('0.00'),
            philpass_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('50000.00'),
            phpusd=Decimal('0.017500'),
        )
        self.service = InstapayService(
            policy,
            MemoryInstapayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=PhClock(),
        )

    def _add(self, nickname='Juan Mobile', name='Juan Cruz', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=kwargs.pop('legal_name', name),
            filipino_name=kwargs.pop('filipino_name', 'Juan dela Cruz'),
            default_account=kwargs.pop('default_account', '1001'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', '09171234567'),
            bank_code=kwargs.pop('bank_code', '011'),
            branch_code=kwargs.pop('branch_code', ''),
            account_number=kwargs.pop('account_number', '1234567890'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_filipino_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('filipino_name', payload)
        self.assertEqual(payload['proxy_mask'], '+63****4567')
        self.assertEqual(payload['account_last4'], '7890')
        self.assertEqual(payload['filipino_mask'], 'J***z')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('09171234567', str(snap))
        self.assertNotIn('1234567890', str(snap))
        self.assertNotIn('Juan dela Cruz', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '175.00')
        self.assertTrue(payment.e2e_id.startswith('INST20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('INST'))
        self.assertNotIn('09171234567', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '175.00', 'instapay to Juan Mobile'))

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

    def test_pesonet_after_cutoff_queues_philpass_night_queues(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        bene = self._add(
            nickname='BDO RTGS', proxy_type='', proxy='',
            bank_code='010', account_number='9876543210',
            filipino_name='Maria Santos', legal_name='Maria Santos',
        )
        core, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33000.00', rail='pesonet', trace_id='late-peso',
        )
        self.assertEqual(core.status, 'queued')
        self.assertEqual(core.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 17, 30)
        philpass, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40000.00', rail='philpass', trace_id='late-pass',
        )
        self.assertEqual(philpass.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-peso'], 'sent')
        self.assertEqual(statuses['late-pass'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('577.50', usd_amounts)
        self.assertIn('700.00', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='010',
            account_number='9876543210', filipino_name='Maria Santos',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', rail='instapay', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(InstapayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '175.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='50000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(InstapayError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '875.00')

    def test_filipino_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else', filipino_name='Wong Wei Ming')
        with self.assertRaises(InstapayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='filipino-no',
            )
        self.assertEqual(ctx.exception.code, 'filipino_mismatch')
        payment, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='filipino-yes',
            filipino_override=True,
        )
        self.assertTrue(created)
        self.assertEqual(payment.filipino_outcome, 'match')

    def test_credit_account_pause_instapay_cap_and_philpass_currency(self):
        bene = self._add()
        with self.assertRaises(InstapayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(InstapayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='50000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(InstapayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.00',
                rail='philpass', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(InstapayError) as ctx:
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
        with self.assertRaises(InstapayError) as ctx:
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
        with self.assertRaises(InstapayError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '175.00')

    def test_sqlite_roundtrip_masks_filipino_and_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInstapayStore(path)
            service = InstapayService(
                InstapayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=PhClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BPI Juan', legal_name='Juan Cruz', filipino_name='Juan dela Cruz',
                default_account='1001', proxy_type='', proxy='',
                bank_code='011', account_number='1234567890',
            )
            reloaded = SqliteInstapayStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '1234567890')
            self.assertEqual(reloaded.filipino_name, 'Juan dela Cruz')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertNotIn('filipino_name', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['filipino_mask'], 'J***z')
            self.assertEqual(reloaded.to_dict()['account_last4'], '7890')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
