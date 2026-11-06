import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.npp import (
    AusClock,
    MemoryNppStore,
    NppError,
    NppPolicy,
    NppService,
    SqliteNppStore,
    aus_holidays,
    compose_acn,
    compose_e2e,
    compose_npp1,
    easter_gregorian,
    mask_abn,
    mask_bsb,
    mask_e2e,
    mask_payid,
    normalize_abn,
    normalize_bsb,
    normalize_e2e,
    normalize_payid,
    parse_npp1,
    resolve_payid,
    split_npp1_file,
)

AEST = timezone(timedelta(hours=10))
BSB = '062000'
BSB_WBC = '032000'
PAYID_EMAIL = 'ada@example.com'
PAYID_PHONE = '+61412345678'
ABN = '51824753556'
ACN = compose_acn('00408561')
ACCOUNT = '123456789'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=AEST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_bsb_format_and_mask(self):
        self.assertEqual(normalize_bsb('062-000'), BSB)
        self.assertEqual(mask_bsb(BSB), '062-***')
        with self.assertRaises(NppError) as ctx:
            normalize_bsb('000000')
        self.assertEqual(ctx.exception.code, 'invalid_bsb')
        with self.assertRaises(NppError):
            normalize_bsb('06200')

    def test_payid_email_phone_abn_and_mask(self):
        value, kind = normalize_payid('Ada@Example.com')
        self.assertEqual((value, kind), (PAYID_EMAIL, 'email'))
        self.assertEqual(mask_payid(PAYID_EMAIL, 'email')[0], 'a')
        self.assertIn('@example.com', mask_payid(PAYID_EMAIL, 'email'))
        self.assertNotEqual(mask_payid(PAYID_EMAIL, 'email'), PAYID_EMAIL)
        phone, phone_kind = normalize_payid('0412 345 678', payid_type='phone')
        self.assertEqual((phone, phone_kind), (PAYID_PHONE, 'phone'))
        self.assertEqual(normalize_abn('51 824 753 556'), ABN)
        self.assertEqual(mask_abn(ABN)[:2], '51')
        self.assertIn('*', mask_abn(ABN))
        with self.assertRaises(NppError) as ctx:
            normalize_abn('51824753557')
        self.assertEqual(ctx.exception.code, 'invalid_abn')
        self.assertEqual(ACN, '004085616')

    def test_e2e_layout_rejects_other_rails(self):
        self.assertEqual(compose_e2e('20240614', 1), 'NPP20240614000001')
        self.assertEqual(len(compose_e2e('20240614', 1)), 17)
        self.assertEqual(mask_e2e('NPP20240614000001')[-4:], '0001')
        with self.assertRaises(NppError) as ctx:
            compose_e2e('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        with self.assertRaises(NppError):
            normalize_e2e('240614000001')
        with self.assertRaises(NppError):
            normalize_e2e('20240614KONOHA01000001')
        with self.assertRaises(NppError):
            normalize_e2e('UETR' + 'A' * 13)

    def test_npp1_compose_parse_and_reject_xml(self):
        e2e = compose_e2e('20240614', 7)
        raw = compose_npp1(
            e2e=e2e, rail='osko', payid_type='email', payid=PAYID_EMAIL,
            bsb=BSB, account_number=ACCOUNT, abn=ABN, amount=Decimal('25.50'),
            purpose='family', status='completed', mask=False,
        )
        parsed = parse_npp1(raw)
        self.assertEqual(parsed['e2e'], e2e)
        self.assertEqual(parsed['amount'], '25.50')
        self.assertEqual(parsed['clearing'], 'AUNPP')
        self.assertEqual(parsed['settlement_method'], 'osko')
        masked = compose_npp1(
            e2e=e2e, rail='osko', payid_type='email', payid=PAYID_EMAIL,
            bsb=BSB, account_number=ACCOUNT, abn=ABN, amount=Decimal('1.00'),
            purpose='other', status='completed', mask=True,
        )
        self.assertNotIn(PAYID_EMAIL, masked)
        self.assertNotIn(e2e, masked)
        with self.assertRaises(NppError) as ctx:
            parse_npp1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        with self.assertRaises(NppError) as xml_ctx:
            split_npp1_file('<?xml version="1.0"?><NPP/>')
        self.assertEqual(xml_ctx.exception.code, 'invalid_file')
        with self.assertRaises(NppError) as clearing:
            parse_npp1('NPP1|1|NPP20240614000007|osko|email|a@b.co|062000|1234||1.00|other|completed|AUD|FEDNOW')
        self.assertEqual(clearing.exception.code, 'invalid_file')

    def test_payid_confirmation_of_payee(self):
        matched = resolve_payid(PAYID_EMAIL, 'Ada Lovelace')
        self.assertEqual(matched.outcome, 'match')
        self.assertEqual(matched.registered_name, 'ADA LOVELACE')
        close = resolve_payid(PAYID_EMAIL, 'Lovelace')
        self.assertEqual(close.outcome, 'close_match')
        self.assertIn('registered_name', close.to_dict())
        mismatch = resolve_payid(PAYID_EMAIL, 'Eve Example')
        self.assertEqual(mismatch.outcome, 'no_match')
        self.assertNotIn('registered_name', mismatch.to_dict())
        missing = resolve_payid('nobody@example.com', 'Ada Lovelace')
        self.assertEqual(missing.outcome, 'unavailable')

    def test_aus_holidays_and_aest_session_clock(self):
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        holidays = aus_holidays(2024)
        self.assertIn(datetime(2024, 1, 26).date(), holidays)
        self.assertIn(datetime(2024, 3, 29).date(), holidays)
        self.assertIn(datetime(2024, 4, 25).date(), holidays)
        self.assertIn(datetime(2024, 6, 10).date(), holidays)
        clock = AusClock(open_hour=9, cutoff_hour=17, tz_offset_minutes=600)
        inside = ts(2024, 6, 14, 11, 0)
        after = ts(2024, 6, 14, 17, 30)
        self.assertTrue(clock.is_session(inside))
        self.assertFalse(clock.is_session(after))
        snap = clock.snapshot(after)
        self.assertTrue(snap['after_cutoff'])
        self.assertEqual(snap['timezone'], 'AEST')
        australia_day = ts(2024, 1, 26, 11, 0)
        self.assertTrue(clock.snapshot(australia_day)['holiday'])
        self.assertFalse(clock.is_session(australia_day))


class NppServiceTests(unittest.TestCase):
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

        policy = NppPolicy(
            outbound_fee=Decimal('0.00'),
            dual_control_threshold=Decimal('10000.00'),
            min_amount=Decimal('1.00'),
        )
        self.service = NppService(
            policy,
            MemoryNppStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=AusClock(open_hour=9, cutoff_hour=17, tz_offset_minutes=600),
        )

    def _add(self, nickname='Ada PayID', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            rail=kwargs.pop('rail', 'osko'),
            payid=kwargs.pop('payid', PAYID_EMAIL),
            payid_type=kwargs.pop('payid_type', 'email'),
            bsb=kwargs.pop('bsb', ''),
            account_number=kwargs.pop('account_number', ''),
            abn=kwargs.pop('abn', ''),
            acn=kwargs.pop('acn', ''),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_payid_bsb_or_e2e(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('payid', payload)
        self.assertNotIn('bsb', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('account_number', payload)
        self.assertTrue(payload['payid_masked'].endswith('@example.com'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('payid', snap['beneficiaries'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        dumped = payment.to_dict()
        self.assertNotIn('e2e', dumped)
        self.assertNotIn('payid', dumped)
        self.assertTrue(dumped['e2e_masked'].endswith('0001'))
        self.assertEqual(payment.status, 'completed')
        self.assertTrue(payment.e2e.startswith('NPP20240614'))
        self.assertEqual(self.debits[0], ('1001', '100.00', 'osko to Ada PayID'))

    def test_off_session_queues_de_but_osko_posts_weekend_and_holiday(self):
        self.service.policy.dual_control_threshold = Decimal('10000000.00')
        de = self._add(
            nickname='Payroll DE', rail='de', payid='', payid_type='',
            bsb=BSB, account_number=ACCOUNT,
        )
        self.now[0] = ts(2024, 6, 14, 17, 30)
        queued, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=de.beneficiary_id, amount='2500.00', trace_id='night',
        )
        self.assertTrue(created)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.debits, [])

        osko = self._add(nickname='Night Osko', payid='night@example.net')
        online, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=osko.beneficiary_id, amount='80.00', trace_id='online',
        )
        self.assertEqual(online.status, 'completed')
        self.assertEqual(self.debits[0][2], 'osko to Night Osko')

        holiday_de = self._add(
            nickname='DE WBC', rail='de', payid='', payid_type='',
            bsb=BSB_WBC, account_number='99887766',
        )
        self.now[0] = ts(2024, 1, 26, 11, 0)
        holiday, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=holiday_de.beneficiary_id, amount='5000.00', trace_id='holi',
        )
        self.assertEqual(holiday.status, 'queued')

        self.now[0] = ts(2024, 1, 29, 11, 0)
        released = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in released}
        self.assertEqual(statuses['night'], 'completed')
        self.assertEqual(statuses['holi'], 'completed')
        self.assertTrue(released[0].e2e)

    def test_osko_cap_and_bsb_account(self):
        bene = self._add()
        with self.assertRaises(NppError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='10000.01', trace_id='cap',
            )
        self.assertEqual(ctx.exception.code, 'osko_amount_exceeded')

        bsb = self._add(
            nickname='CBA Account', rail='npp', payid='', payid_type='',
            bsb=BSB, account_number='88776655',
        )
        self.assertEqual(bsb.to_dict()['bsb_masked'], '062-***')
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bsb.beneficiary_id, amount='80.00', trace_id='bsb',
        )
        self.assertEqual(sent.status, 'completed')
        self.assertEqual(sent.settlement_method, 'npp')

    def test_credit_account_forbidden_and_duplicate_fingerprint(self):
        with self.assertRaises(NppError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        self._add()
        with self.assertRaises(NppError) as dup:
            self._add(nickname='Other', payid=PAYID_EMAIL)
        self.assertEqual(dup.exception.code, 'beneficiary_duplicate')

    def test_ofac_hold_payid_mismatch_and_dual_control(self):
        bene = self._add(name='Blocked Person LLC', payid='blocked@example.net')
        held, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', trace_id='ofac',
        )
        self.assertTrue(created)
        self.assertEqual(held.status, 'held')
        self.assertEqual(self.debits, [])
        released = self.service.override_ofac(
            payment_id=held.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(released.status, 'completed')

        mismatch = self._add(nickname='Wrong Name', name='Eve Example', payid=PAYID_EMAIL)
        blocked, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=mismatch.beneficiary_id, amount='40.00', trace_id='cop',
        )
        self.assertEqual(blocked.status, 'held')
        self.assertTrue(blocked.payid_hit)
        with self.assertRaises(NppError) as ofac_first:
            self.service.release_payment(
                payment_id=blocked.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ofac_first.exception.code, 'payid_mismatch')
        cleared = self.service.override_payid(
            payment_id=blocked.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(cleared.status, 'completed')

        big = self._add(
            nickname='Payroll', rail='npp', payid='', payid_type='',
            bsb=BSB_WBC, account_number='55667788',
        )
        pending, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=big.beneficiary_id, amount='10000.00', trace_id='dual',
        )
        self.assertEqual(pending.status, 'pending_release')
        with self.assertRaises(NppError) as same:
            self.service.release_payment(
                payment_id=pending.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(same.exception.code, 'same_approver')
        done = self.service.release_payment(
            payment_id=pending.payment_id, actor='approver', actor_type='tier2',
        )
        self.assertEqual(done.status, 'completed')

    def test_osko_complaint_window_and_de_next_business_day(self):
        osko = self._add(nickname='Osko Cel', payid='cel@example.net')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=osko.beneficiary_id, amount='40.00', trace_id='med',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][2].split()[0], 'osko')

        later, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=osko.beneficiary_id, amount='15.00', trace_id='late',
        )
        self.now[0] = later.completed_at + 25 * 3600
        with self.assertRaises(NppError) as closed:
            self.service.recall_payment(
                payment_id=later.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.recall_payment(
            payment_id=later.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'recalled')

        self.now[0] = ts(2024, 6, 17, 11, 0)
        de = self._add(
            nickname='DE', rail='de', payid='', payid_type='',
            bsb=BSB, account_number=ACCOUNT,
        )
        posted, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=de.beneficiary_id, amount='12.00', trace_id='exp',
        )
        self.now[0] = posted.completed_at + 91 * 86400
        with self.assertRaises(NppError) as med:
            self.service.recall_payment(
                payment_id=posted.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(med.exception.code, 'complaint_window_closed')

    def test_idempotent_trace_and_sqlite_roundtrip(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='9.00', trace_id='same',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='9.00', trace_id='same',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len(self.debits), 1)

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteNppStore(path)
            service = NppService(
                NppPolicy(min_amount=Decimal('1.00')),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=AusClock(tz_offset_minutes=600),
            )
            saved = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='CBA', legal_name='Ada Lovelace', rail='osko',
                payid=PAYID_EMAIL, default_account='1001',
            )
            reloaded = SqliteNppStore(path).get_beneficiary(saved.beneficiary_id)
            self.assertEqual(reloaded.nickname, 'CBA')
            self.assertEqual(reloaded.payid, PAYID_EMAIL)
            dumped = reloaded.to_dict()
            self.assertTrue(dumped['payid_masked'].endswith('@example.com'))
            self.assertNotIn('payid', dumped)
            self.assertNotIn('fingerprint', dumped)
        finally:
            os.unlink(path)

    def test_export_is_masked_npp1(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='8.00', trace_id='exp1',
        )
        exported = self.service.export_npp1(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('NPP1|1|', exported['npp1'])
        self.assertNotIn(PAYID_EMAIL, exported['npp1'])
        self.assertNotIn(payment.e2e, exported['npp1'])
        with self.assertRaises(NppError) as ctx:
            self.service.export_npp1(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'npp_forbidden')

    def test_paused_beneficiary_cannot_send(self):
        bene = self._add()
        self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(NppError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='5.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')


if __name__ == '__main__':
    unittest.main()
