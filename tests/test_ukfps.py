import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.ukfps import (
    AmountError,
    CopResolution,
    MemoryUkfpsStore,
    SqliteUkfpsStore,
    UkClock,
    UkfpsError,
    UkfpsPolicy,
    UkfpsService,
    compose_e2e,
    compose_fps18,
    compose_gb_iban,
    convert_instructed_to_usd,
    gb_iban_ok,
    mask_e2e,
    mask_email,
    mask_iban,
    mask_phone,
    money_str,
    normalize_email,
    normalize_iban,
    normalize_phone,
    normalize_sort_code,
    parse_fps18,
    parse_money,
    resolve_ukfps,
    uk_holidays,
    vocalink_ok,
)

GMT = timezone.utc


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=GMT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_fx(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('12.355')), '12.36')
        self.assertEqual(convert_instructed_to_usd(Decimal('100.00'), Decimal('1.27')), Decimal('127.00'))

    def test_vocalink_and_sort_code(self):
        self.assertTrue(vocalink_ok('089999', '66374958'))
        self.assertTrue(vocalink_ok('107999', '88837491'))
        self.assertTrue(vocalink_ok('871427', '46238510'))
        self.assertFalse(vocalink_ok('000000', '12345678'))
        self.assertEqual(normalize_sort_code('20-04-15'), '200415')
        with self.assertRaises(UkfpsError) as ctx:
            normalize_sort_code('000000')
        self.assertEqual(ctx.exception.code, 'invalid_sort')

    def test_gb_iban_mod97_and_mask(self):
        self.assertTrue(gb_iban_ok('GB82WEST12345698765432'))
        self.assertEqual(normalize_iban('gb82 west 123456 98765432'), 'GB82WEST12345698765432')
        self.assertEqual(mask_iban('GB82WEST12345698765432'), 'GB82****5432')
        composed = compose_gb_iban('WEST', '123456', '98765432')
        self.assertTrue(gb_iban_ok(composed))
        with self.assertRaises(UkfpsError) as ctx:
            normalize_iban('GB00WEST12345698765432')
        self.assertEqual(ctx.exception.code, 'invalid_iban')

    def test_paym_email_and_sort(self):
        self.assertEqual(normalize_email('Ada@Example.CO.UK'), 'ada@example.co.uk')
        self.assertEqual(mask_email('ada@example.co.uk'), 'a***@example.co.uk')
        with self.assertRaises(UkfpsError):
            normalize_email('not-an-email')
        self.assertEqual(normalize_phone('07911123456'), '+447911123456')
        self.assertEqual(normalize_phone('+44 7911 123456'), '+447911123456')
        self.assertEqual(mask_phone('+447911123456'), '+44****3456')
        with self.assertRaises(UkfpsError):
            normalize_phone('01234567890')

    def test_e2e_and_fps18_reject_xml_and_foreign_clearing(self):
        self.assertEqual(compose_e2e('fps', '20240614', 1), 'FPS20240614000001')
        self.assertEqual(compose_e2e('chaps', '20240614', 2), 'CHAPS20240614000002')
        self.assertEqual(compose_e2e('bacs', '20240614', 3), 'BACS20240614000003')
        self.assertEqual(mask_e2e('FPS20240614000001'), 'FPS****0001')
        with self.assertRaises(UkfpsError) as ctx:
            compose_e2e('fps', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        line = compose_fps18(
            rail='fps', amount='10.00', bank_code='089999', account_last4='4958',
            e2e_id='FPS20240614000001', purpose='family', value_date='20240614',
            proxy_type='phone', proxy_mask='+44****3456', ofac_hit=False, sequence=1,
        )
        parsed = parse_fps18(line)
        self.assertEqual(parsed['clearing'], 'FPS')
        self.assertEqual(parsed['currency'], 'GBP')
        with self.assertRaises(UkfpsError) as ctx:
            parse_fps18('<?xml version="1.0"?><FPS/>')
        self.assertEqual(ctx.exception.code, 'invalid_fps18')
        with self.assertRaises(UkfpsError) as ctx:
            parse_fps18('1|FAST|GBP|10.00|089999|4958|FPS20240614000001|family|20240614|none|****|N|1|040075')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(UkfpsError) as ctx:
            parse_fps18('1|FEDWIRE|GBP|10.00|089999|4958|FPS20240614000001|family|20240614|none|****|N|1|040075')
        self.assertEqual(ctx.exception.code, 'invalid_clearing')
        with self.assertRaises(UkfpsError) as ctx:
            parse_fps18('1|CHAPS|HKD|10.00|089999|4958|CHAPS20240614000001|family|20240614|none|****|N|1|040075')
        self.assertEqual(ctx.exception.code, 'invalid_currency')

    def test_confirmation_of_payee_outcomes(self):
        hit = resolve_ukfps('sort', '08999966374958', 'Ada Lovelace')
        self.assertEqual(hit.outcome, 'match')
        close = resolve_ukfps('phone', '07911123456', 'Lovelace')
        self.assertEqual(close.outcome, 'close_match')
        none = resolve_ukfps('email', 'ada@example.co.uk', 'Blocked Person')
        self.assertEqual(none.outcome, 'no_match')
        missing = resolve_ukfps('phone', '07999888777', 'Ada')
        self.assertEqual(missing.outcome, 'unavailable')
        self.assertIsInstance(hit, CopResolution)
        self.assertNotIn('66374958', str(hit.to_dict()))
        self.assertEqual(hit.to_dict()['proxy_mask'], '08-********4958')
        self.assertEqual(close.to_dict()['proxy_mask'], '+44****3456')

    def test_uk_clock_fps_247_chaps_cutoff_bacs_next_day(self):
        calendar = UkClock(chaps_cutoff_hour=18, chaps_cutoff_minute=0)
        friday = ts(2024, 6, 14, 16, 0)
        friday_late = ts(2024, 6, 14, 18, 30)
        self.assertEqual(calendar.value_date(friday, rail='fps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='fps').isoformat(), '2024-06-14')
        self.assertFalse(calendar.snapshot(friday_late, rail='fps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='chaps').isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late, rail='chaps').isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_late, rail='chaps')['after_cutoff'])
        self.assertEqual(calendar.value_date(friday, rail='bacs').isoformat(), '2024-06-17')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday, rail='chaps').isoformat(), '2024-06-17')
        self.assertIn(datetime(2024, 5, 6).date(), uk_holidays(2024))
        early_may = ts(2024, 5, 6, 11, 0)
        self.assertEqual(calendar.value_date(early_may, rail='fps').isoformat(), '2024-05-06')
        self.assertEqual(calendar.value_date(early_may, rail='chaps').isoformat(), '2024-05-07')


class UkfpsServiceTests(unittest.TestCase):
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

        policy = UkfpsPolicy(
            outbound_fee=Decimal('0.00'),
            chaps_fee=Decimal('25.00'),
            dual_control_threshold=Decimal('10000.00'),
            gbpusd=Decimal('1.27'),
        )
        self.service = UkfpsService(
            policy,
            MemoryUkfpsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=UkClock(chaps_cutoff_hour=18, chaps_cutoff_minute=0),
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
            proxy=kwargs.pop('proxy', '07911123456'),
            bank_code=kwargs.pop('bank_code', '089999'),
            account_number=kwargs.pop('account_number', '66374958'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_proxy_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['proxy_mask'], '+44****3456')
        self.assertEqual(payload['account_last4'], '4958')
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('fingerprint', snap['beneficiaries'][0])
        self.assertNotIn('7911123456', str(snap))
        self.assertNotIn('66374958', str(snap))
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.usd_amount, '127.00')
        self.assertTrue(payment.e2e_id.startswith('FPS20240614'))
        leaked = payment.to_dict()
        self.assertNotEqual(leaked['e2e_id'], payment.e2e_id)
        self.assertTrue(leaked['e2e_id'].startswith('FPS'))
        self.assertNotIn('7911123456', str(leaked))
        self.assertEqual(self.debits[0], ('1001', '127.00', 'ukfps to Ada Mobile'))

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

    def test_chaps_after_cutoff_queues_bacs_always_queues(self):
        self.now[0] = ts(2024, 6, 14, 18, 30)
        bene = self._add(
            nickname='Barclays CHAPS', proxy_type='', proxy='',
            bank_code='040004', account_number='12345678',
        )
        chaps, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', rail='chaps', trace_id='late-chaps',
        )
        self.assertEqual(chaps.status, 'queued')
        self.assertEqual(chaps.value_date, '20240617')
        self.assertEqual(self.debits, [])
        bacs, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='12.00', rail='bacs', trace_id='bacs-1',
        )
        self.assertEqual(bacs.status, 'queued')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in due}
        self.assertEqual(statuses['late-chaps'], 'sent')
        self.assertEqual(statuses['bacs-1'], 'sent')
        usd_amounts = [item[1] for item in self.debits]
        self.assertIn('63.50', usd_amounts)
        self.assertIn('15.24', usd_amounts)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(
            nickname='Blocked', name='Blocked Person',
            proxy_type='', proxy='', bank_code='040004', account_number='12345678',
        )
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', rail='fps', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(UkfpsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '101.60')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(UkfpsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '12700.00')

    def test_cop_mismatch_blocks_until_staff_override_on_originate(self):
        bene = self._add(nickname='Wrong Name', name='Someone Else')
        with self.assertRaises(UkfpsError) as ctx:
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

    def test_credit_account_pause_fps_cap_and_chaps_currency(self):
        bene = self._add()
        with self.assertRaises(UkfpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(UkfpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='1000000.01',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')
        with self.assertRaises(UkfpsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                rail='chaps', currency='USD',
            )
        self.assertEqual(ctx.exception.code, 'invalid_currency')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(UkfpsError) as ctx:
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
        with self.assertRaises(UkfpsError) as ctx:
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
        with self.assertRaises(UkfpsError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')
        recalled = self.service.recall_payment(
            payment_id=live.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '27.94')

    def test_sqlite_roundtrip_masks_iban(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteUkfpsStore(path)
            service = UkfpsService(
                UkfpsPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=UkClock(),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='IBAN Ada', legal_name='Ada Lovelace Ltd', default_account='1001',
                proxy_type='iban', proxy='GB82WEST12345698765432',
            )
            reloaded = SqliteUkfpsStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.proxy, 'GB82WEST12345698765432')
            self.assertNotIn('proxy', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['proxy_mask'], 'GB82****5432')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
