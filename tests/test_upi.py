import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.upi import (
    MemoryUpiStore,
    NpciClock,
    SqliteUpiStore,
    UpiError,
    UpiPolicy,
    UpiService,
    compose_imps1,
    compose_pan,
    compose_rrn,
    compose_upi1,
    easter_gregorian,
    mask_ifsc,
    mask_pan,
    mask_rrn,
    mask_vpa,
    normalize_ifsc,
    normalize_pan,
    normalize_phone,
    normalize_vpa,
    pan_check_digit_ok,
    parse_upi1,
    rbi_holidays,
    split_upi1_file,
)

IST = timezone(timedelta(hours=5, minutes=30))
IFSC = 'HDFC0000001'
IFSC_SBI = 'SBIN0005943'
VPA = 'ada@oksbi'
PHONE = '+919876543210'
MMID = '9042901'
PAN = compose_pan('AAAPL1234')
ACCOUNT = '12345678901'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST).timestamp()


class FoundationTests(unittest.TestCase):
    def test_ifsc_format_and_mask(self):
        self.assertEqual(normalize_ifsc('hdfc-0000001'), IFSC)
        self.assertEqual(mask_ifsc(IFSC), 'HDFC0******')
        with self.assertRaises(UpiError) as ctx:
            normalize_ifsc('HDFC1000001')
        self.assertEqual(ctx.exception.code, 'invalid_ifsc')
        with self.assertRaises(UpiError):
            normalize_ifsc('00000000000')

    def test_vpa_psp_directory_and_mask(self):
        self.assertEqual(normalize_vpa('Ada.Lovelace@oksbi'), 'ada.lovelace@oksbi')
        self.assertEqual(mask_vpa(VPA)[0], 'a')
        self.assertIn('@oksbi', mask_vpa(VPA))
        self.assertNotEqual(mask_vpa(VPA), VPA)
        with self.assertRaises(UpiError) as ctx:
            normalize_vpa('ada@not-a-psp')
        self.assertEqual(ctx.exception.code, 'invalid_vpa')

    def test_pan_check_digit_and_mask(self):
        self.assertTrue(pan_check_digit_ok(PAN))
        self.assertEqual(normalize_pan('AAAPL-1234-' + PAN[-1]), PAN)
        self.assertEqual(compose_pan('AAAPL1234'), PAN)
        self.assertEqual(mask_pan(PAN)[:3], 'AAA')
        self.assertIn('*', mask_pan(PAN))
        with self.assertRaises(UpiError) as ctx:
            normalize_pan('AAAPL1234A')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        with self.assertRaises(UpiError):
            normalize_pan('AAADL1234A')

    def test_indian_phone_and_rrn(self):
        self.assertEqual(normalize_phone('98765 43210'), PHONE)
        self.assertEqual(normalize_phone('91 98765 43210'), PHONE)
        rrn = compose_rrn('20240614', 1, rail='upi')
        self.assertEqual(rrn, '240614000001')
        self.assertEqual(len(rrn), 12)
        utr = compose_rrn('20240614', 1, rail='neft')
        self.assertEqual(utr, 'KNH2024061400001')
        self.assertEqual(mask_rrn(rrn)[-4:], '0001')
        with self.assertRaises(UpiError) as ctx:
            compose_rrn('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_upi1_compose_parse_and_reject_xml(self):
        rrn = compose_rrn('20240614', 7, rail='upi')
        raw = compose_upi1(
            rrn=rrn, vpa=VPA, ifsc=IFSC, account_number=ACCOUNT, mmid='',
            phone='', pan='', amount=Decimal('25.50'), purpose='family',
            status='completed', rail='upi', mask=False,
        )
        parsed = parse_upi1(raw)
        self.assertEqual(parsed['rrn'], rrn)
        self.assertEqual(parsed['amount'], '25.50')
        self.assertEqual(parsed['settlement_method'], 'upi')
        masked = compose_upi1(
            rrn=rrn, vpa=VPA, ifsc=IFSC, account_number=ACCOUNT, mmid='',
            phone='', pan='', amount=Decimal('1.00'), purpose='other',
            status='completed', rail='upi', mask=True,
        )
        with self.assertRaises(UpiError) as ctx:
            parse_upi1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(UpiError) as xml_ctx:
            split_upi1_file('<?xml version="1.0"?><UPI/>')
        self.assertEqual(xml_ctx.exception.code, 'invalid_file')
        imps = compose_imps1(mmid=MMID, phone=PHONE, amount=Decimal('80.00'), rrn=rrn, mask=True)
        self.assertTrue(imps.startswith('IMPS1|1|'))
        self.assertNotIn('9042901', imps)
        self.assertNotIn('9876543210', imps)

    def test_rbi_holidays_and_ist_session_clock(self):
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        holidays = rbi_holidays(2024)
        self.assertIn(datetime(2024, 1, 26).date(), holidays)
        self.assertIn(datetime(2024, 6, 8).date(), holidays)
        self.assertIn(datetime(2024, 6, 22).date(), holidays)
        self.assertNotIn(datetime(2024, 6, 15).date(), holidays)
        self.assertIn(datetime(2024, 8, 15).date(), holidays)
        clock = NpciClock(open_hour=9, cutoff_hour=18, tz_offset_minutes=330)
        inside = ts(2024, 6, 14, 11, 0)
        after = ts(2024, 6, 14, 18, 30)
        self.assertTrue(clock.is_session(inside))
        self.assertFalse(clock.is_session(after))
        snap = clock.snapshot(after)
        self.assertTrue(snap['after_cutoff'])
        self.assertEqual(snap['timezone'], 'IST')
        republic = ts(2024, 1, 26, 11, 0)
        self.assertTrue(clock.snapshot(republic)['holiday'])
        self.assertFalse(clock.is_session(republic))


class UpiServiceTests(unittest.TestCase):
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

        policy = UpiPolicy(
            outbound_fee=Decimal('0.00'),
            dual_control_threshold=Decimal('200000.00'),
            min_amount=Decimal('1.00'),
        )
        self.service = UpiService(
            policy,
            MemoryUpiStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=NpciClock(open_hour=9, cutoff_hour=18, tz_offset_minutes=330),
        )

    def _add(self, nickname='Ada UPI', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            rail=kwargs.pop('rail', 'upi'),
            vpa=kwargs.pop('vpa', VPA),
            ifsc=kwargs.pop('ifsc', ''),
            account_number=kwargs.pop('account_number', ''),
            mmid=kwargs.pop('mmid', ''),
            phone=kwargs.pop('phone', ''),
            pan=kwargs.pop('pan', ''),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_vpa_ifsc_or_rrn(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('vpa', payload)
        self.assertNotIn('ifsc', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['vpa_masked'][-6:], '@oksbi')
        snap = self.service.snapshot('alice')
        self.assertNotIn('vpa', snap['beneficiaries'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        dumped = payment.to_dict()
        self.assertNotIn('rrn', dumped)
        self.assertNotIn('vpa', dumped)
        self.assertTrue(dumped['rrn_masked'].endswith('0001'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(len(payment.rrn), 12)
        self.assertEqual(self.debits[0], ('1001', '100.00', 'upi to Ada UPI'))

    def test_off_session_queues_rtgs_and_holiday_neft_but_upi_posts(self):
        self.service.policy.dual_control_threshold = Decimal('10000000.00')
        self.service.policy.rtgs_min = Decimal('200000.00')
        rtgs = self._add(
            nickname='Payroll RTGS', rail='rtgs', vpa='', ifsc=IFSC,
            account_number=ACCOUNT, pan=PAN,
        )
        self.now[0] = ts(2024, 6, 14, 18, 30)
        queued, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=rtgs.beneficiary_id, amount='250000.00', trace_id='night',
        )
        self.assertTrue(created)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.debits, [])

        upi = self._add(nickname='Night UPI', vpa='night@ybl')
        online, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=upi.beneficiary_id, amount='80.00', trace_id='online',
        )
        self.assertEqual(online.status, 'completed')
        self.assertEqual(self.debits[0][2], 'upi to Night UPI')

        neft = self._add(
            nickname='NEFT SBI', rail='neft', vpa='', ifsc=IFSC_SBI,
            account_number='9988776655', pan=PAN,
        )
        self.now[0] = ts(2024, 6, 8, 11, 0)
        holiday, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=neft.beneficiary_id, amount='5000.00', trace_id='holi',
        )
        self.assertEqual(holiday.status, 'queued')

        self.now[0] = ts(2024, 6, 10, 11, 0)
        released = self.service.run_due('alice')
        statuses = {row.trace_id: row.status for row in released}
        self.assertEqual(statuses['night'], 'completed')
        self.assertEqual(statuses['holi'], 'completed')
        self.assertTrue(released[0].rrn)

    def test_upi_cap_and_imps_mmid(self):
        bene = self._add()
        with self.assertRaises(UpiError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='100000.01', trace_id='cap',
            )
        self.assertEqual(ctx.exception.code, 'upi_amount_exceeded')

        imps = self._add(
            nickname='IMPS Mobile', rail='imps', vpa='', ifsc='',
            account_number='', mmid=MMID, phone='9876543210',
        )
        self.assertEqual(imps.to_dict()['mmid_masked'][-4:], '2901')
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=imps.beneficiary_id, amount='80.00', trace_id='imps',
        )
        self.assertEqual(sent.status, 'completed')
        self.assertEqual(sent.settlement_method, 'imps')
        with self.assertRaises(UpiError) as over:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=imps.beneficiary_id, amount='500000.01', trace_id='impscap',
            )
        self.assertEqual(over.exception.code, 'imps_amount_exceeded')

    def test_credit_account_forbidden_and_duplicate_fingerprint(self):
        with self.assertRaises(UpiError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        self._add()
        with self.assertRaises(UpiError) as dup:
            self._add(nickname='Other', vpa=VPA)
        self.assertEqual(dup.exception.code, 'beneficiary_duplicate')

    def test_ofac_hold_then_override_and_dual_control(self):
        bene = self._add(name='Blocked Person LLC')
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
        self.assertEqual(self.debits[0][2], 'upi to Ada UPI')

        big = self._add(
            nickname='Payroll', rail='neft', vpa='', ifsc=IFSC_SBI,
            account_number='5566778899', pan=PAN,
        )
        pending, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=big.beneficiary_id, amount='200000.00', trace_id='dual',
        )
        self.assertEqual(pending.status, 'pending_release')
        with self.assertRaises(UpiError) as same:
            self.service.release_payment(
                payment_id=pending.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(same.exception.code, 'same_approver')
        done = self.service.release_payment(
            payment_id=pending.payment_id, actor='approver', actor_type='tier2',
        )
        self.assertEqual(done.status, 'completed')

    def test_upi_complaint_window_and_neft_next_business_day(self):
        upi = self._add(nickname='UPI Cel', vpa='cel@ybl')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=upi.beneficiary_id, amount='40.00', trace_id='med',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][2].split()[0], 'upi')

        later, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=upi.beneficiary_id, amount='15.00', trace_id='late',
        )
        self.now[0] = later.completed_at + 25 * 3600
        with self.assertRaises(UpiError) as closed:
            self.service.recall_payment(
                payment_id=later.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.recall_payment(
            payment_id=later.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'recalled')

        neft = self._add(
            nickname='NEFT', rail='neft', vpa='', ifsc=IFSC,
            account_number=ACCOUNT, pan=PAN,
        )
        posted, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=neft.beneficiary_id, amount='12.00', trace_id='exp',
        )
        self.now[0] = posted.completed_at + 91 * 86400
        with self.assertRaises(UpiError) as med:
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
            store = SqliteUpiStore(path)
            service = UpiService(
                UpiPolicy(min_amount=Decimal('1.00')),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=NpciClock(tz_offset_minutes=330),
            )
            saved = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='HDFC', legal_name='Ada Lovelace', rail='upi',
                vpa=VPA, default_account='1001',
            )
            reloaded = SqliteUpiStore(path).get_beneficiary(saved.beneficiary_id)
            self.assertEqual(reloaded.nickname, 'HDFC')
            self.assertEqual(reloaded.vpa, VPA)
            dumped = reloaded.to_dict()
            self.assertTrue(dumped['vpa_masked'].endswith('@oksbi'))
            self.assertNotIn('vpa', dumped)
            self.assertNotIn('fingerprint', dumped)
        finally:
            os.unlink(path)

    def test_export_is_masked_upi1(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='8.00', trace_id='exp1',
        )
        exported = self.service.export_upi1(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('UPI1|1|', exported['upi1'])
        self.assertNotIn(VPA, exported['upi1'])
        self.assertNotIn(payment.rrn, exported['upi1'])
        with self.assertRaises(UpiError) as ctx:
            self.service.export_upi1(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'upi_forbidden')

    def test_paused_beneficiary_cannot_send(self):
        bene = self._add()
        self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(UpiError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='5.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')


if __name__ == '__main__':
    unittest.main()
