import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.urecv import (
    MemoryUrecvStore,
    NpciClock,
    SqliteUrecvStore,
    UrecvError,
    UrecvKey,
    UrecvPolicy,
    UrecvService,
    compose_imps1,
    compose_pan,
    compose_rrn,
    compose_upi1,
    destination_fingerprint,
    easter_gregorian,
    mask_ifsc,
    mask_pan,
    mask_phone,
    mask_rrn,
    mask_vpa,
    normalize_ifsc,
    normalize_mmid,
    normalize_phone,
    normalize_rrn,
    normalize_vpa,
    pan_check_digit_ok,
    parse_upi1,
    rbi_holidays,
    split_upi1_file,
)

IST = timezone(timedelta(hours=5, minutes=30))
RECEIVER = 'KNHB0000001'
SENDER = 'HDFC0000001'
VPA = 'ada@oksbi'
PHONE = '+919876543210'
MMID = '1234567'
EXT_ACCOUNT = '123456789012'
PAN = compose_pan('AAAPA9999')


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST).timestamp()


def rrn(seq=1, day='20240614', rail='upi'):
    return compose_rrn(day, seq, rail=rail)


class FoundationTests(unittest.TestCase):
    def test_ifsc_vpa_mmid_pan_phone_and_rrn(self):
        self.assertEqual(normalize_ifsc('knhb0000001'), RECEIVER)
        self.assertEqual(mask_ifsc(RECEIVER), 'KNHB0******')
        self.assertEqual(normalize_vpa('Ada@oksbi'), VPA)
        self.assertEqual(mask_vpa(VPA), 'a*a@oksbi')
        with self.assertRaises(UrecvError) as ctx:
            normalize_vpa('ada@notapsp')
        self.assertEqual(ctx.exception.code, 'invalid_vpa')
        self.assertEqual(normalize_mmid('1234567'), MMID)
        self.assertTrue(pan_check_digit_ok(PAN))
        self.assertEqual(normalize_phone('9876543210'), PHONE)
        self.assertEqual(normalize_phone('919876543210'), PHONE)
        self.assertEqual(mask_phone(PHONE), '+91-****-3210')
        self.assertEqual(mask_pan(PAN), 'AAA******' + PAN[-1])
        composed = rrn(1)
        self.assertEqual(composed, '240614000001')
        self.assertEqual(normalize_rrn(composed), composed)
        self.assertTrue(mask_rrn(composed).endswith('0001'))
        self.assertEqual(len(destination_fingerprint('upi', VPA, '', '', '', '')), 64)
        self.assertNotEqual(
            destination_fingerprint('upi', VPA, '', '', '', ''),
            destination_fingerprint('imps', '', '', '', MMID, PHONE),
        )
        neft = rrn(1, rail='neft')
        self.assertEqual(neft, 'KNH2024061400001')
        self.assertEqual(len(neft), 16)

    def test_rbi_clock_session_sunday_and_independence_day(self):
        clock = NpciClock(open_hour=9, cutoff_hour=18, tz_offset_minutes=330)
        friday = ts(2024, 6, 14, 11, 0)
        after = ts(2024, 6, 14, 18, 0)
        night = ts(2024, 6, 14, 21, 0)
        self.assertTrue(clock.is_session(friday))
        self.assertFalse(clock.is_session(after))
        self.assertFalse(clock.is_session(night))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        sunday = ts(2024, 6, 16, 11, 0)
        self.assertTrue(clock.snapshot(sunday)['weekend'])
        self.assertIn(date(2024, 8, 15), rbi_holidays(2024))
        self.assertTrue(clock.is_holiday(date(2024, 8, 15)))
        easter = easter_gregorian(2024)
        self.assertEqual(easter, date(2024, 3, 31))
        self.assertTrue(clock.is_holiday(date(2024, 6, 8)))

    def test_upi1_imps1_roundtrip_rejects_xml(self):
        record = compose_upi1(
            rrn=rrn(1),
            vpa=VPA,
            ifsc='',
            account_number='',
            mmid='',
            phone='',
            pan='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            rail='upi',
            mask=False,
        )
        parsed = parse_upi1(record)
        self.assertEqual(parsed['rrn'], rrn(1))
        self.assertEqual(parsed['vpa'], VPA)
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        masked = compose_upi1(
            rrn=rrn(1),
            vpa=VPA,
            ifsc='',
            account_number='',
            mmid='',
            phone='',
            pan='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            rail='upi',
            mask=True,
        )
        self.assertIn('a*a@oksbi', masked)
        self.assertNotIn(VPA, masked)
        self.assertNotIn(rrn(1), masked)
        with self.assertRaises(UrecvError) as ctx:
            parse_upi1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(UrecvError) as xml:
            split_upi1_file('<?xml version="1.0"?><!DOCTYPE upi><UPI1></UPI1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        imps = compose_imps1(mmid=MMID, phone=PHONE, amount=Decimal('80.00'), rrn=rrn(2), mask=False)
        self.assertIn(PHONE, imps)
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class UrecvServiceTests(unittest.TestCase):
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
            if userid != 'alice':
                return {}
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        self.service = UrecvService(
            UrecvPolicy(
                dual_control_threshold=Decimal('200000.00'),
                min_amount=Decimal('0.01'),
                receiver_ifsc=RECEIVER,
            ),
            MemoryUrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=NpciClock(tz_offset_minutes=330),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ada VPA'),
            rail=kwargs.pop('rail', 'upi'),
            vpa=kwargs.pop('vpa', VPA),
            ifsc=kwargs.pop('ifsc', ''),
            account_number=kwargs.pop('account_number', ''),
            mmid=kwargs.pop('mmid', ''),
            phone=kwargs.pop('phone', ''),
            pan=kwargs.pop('pan', ''),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        rail = values.get('rail', 'upi')
        body = {
            'rrn': values.pop('rrn', rrn(values.pop('seq', 1), rail=rail)),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_ifsc': values.pop('send_ifsc', SENDER),
            'recv_ifsc': values.pop('recv_ifsc', RECEIVER),
            'rail': values.pop('rail', 'upi'),
            'vpa': values.pop('vpa', VPA),
            'purpose': values.pop('purpose', 'goods'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_vpa_ifsc_or_rrn(self):
        enrollment = self._key()
        payload = enrollment.to_dict()
        self.assertNotIn('vpa', payload)
        self.assertNotIn('ifsc', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['vpa_masked'], 'a*a@oksbi')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('vpa', dumped)
        self.assertNotIn('rrn', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['vpa_masked'], 'a*a@oksbi')
        self.assertTrue(dumped['rrn_masked'].endswith('0001'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('vpa', snap['inbounds'][0])
        self.assertNotIn('rrn', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'upi from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_rrn(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_key_does_not_credit_until_claim(self):
        inbound, _ = self._ingest(vpa='', rail='imps', mmid=MMID, phone=PHONE, seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            rail='imps', mmid=MMID, phone=PHONE,
            destination_account='1001', nickname='Ada IMPS',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(claimed.to_dict()['mmid_masked'], '***4567')

    def test_enroll_rematches_unmatched_vpa(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(UrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', rail='upi',
            vpa=VPA, ifsc='', account_number='', mmid='', phone='', pan='',
            fingerprint=destination_fingerprint('upi', VPA, '', '', '', ''),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(UrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', rail='imps', mmid=MMID, phone=PHONE,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(UrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '250.00')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('200.00')
        self._key()
        inbound, _ = self._ingest(amount='250.00', seq=5)
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(UrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_upi_posts_after_cutoff_rtgs_queues(self):
        self.service.policy.dual_control_threshold = Decimal('5000000.00')
        self._key()
        self.now[0] = ts(2024, 6, 14, 21, 0)
        inbound, _ = self._ingest(seq=6)
        self.assertEqual(inbound.status, 'posted')
        self.assertFalse(self.service.calendar.is_session(self.now[0]))
        self._key(
            nickname='Ada RTGS', rail='rtgs', vpa='', ifsc=RECEIVER,
            account_number=EXT_ACCOUNT, pan=PAN,
        )
        large, _ = self._ingest(
            amount='200000.00', seq=61, rail='rtgs', vpa='', ifsc=RECEIVER,
            account_number=EXT_ACCOUNT, pan=PAN,
        )
        self.assertEqual(large.status, 'queued')
        self.assertEqual(len(self.credits), 1)
        self.now[0] = ts(2024, 6, 17, 11, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_imps_posts_24_7_and_customer_upi_return_windows(self):
        self._key(nickname='Ada IMPS', rail='imps', vpa='', mmid=MMID, phone=PHONE)
        self.now[0] = ts(2024, 6, 16, 21, 0)
        inbound, _ = self._ingest(rail='imps', vpa='', mmid=MMID, phone=PHONE, seq=7)
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0][2], 'imps from Ada Lovelace')
        self._key()
        upi, _ = self._ingest(seq=71)
        self.now[0] = upi.posted_at + (25 * 60 * 60)
        with self.assertRaises(UrecvError) as late:
            self.service.request_return(
                inbound_id=upi.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=upi.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertTrue(self.debits[0][2].startswith('upi return '))

    def test_neft_customer_complaint_closes_after_next_session(self):
        self._key(
            nickname='Ada NEFT', rail='neft', vpa='', ifsc=RECEIVER,
            account_number=EXT_ACCOUNT, pan=PAN,
        )
        inbound, _ = self._ingest(
            seq=8, rail='neft', vpa='', ifsc=RECEIVER, account_number=EXT_ACCOUNT, pan=PAN,
        )
        self.assertEqual(inbound.status, 'posted')
        within = self.service.request_return(
            inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(within.status, 'returned')
        inbound2, _ = self._ingest(
            seq=81, rail='neft', vpa='', ifsc=RECEIVER, account_number=EXT_ACCOUNT, pan=PAN,
        )
        self.now[0] = ts(2024, 6, 17, 18, 1)
        with self.assertRaises(UrecvError) as closed:
            self.service.request_return(
                inbound_id=inbound2.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=inbound2.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(vpa='', rail='imps', mmid=MMID, phone=PHONE, seq=9)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.to_dict()['mmid_masked'], '***4567')

        with self.assertRaises(UrecvError) as ctx:
            self._ingest(recv_ifsc=SENDER, seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(UrecvError) as huge:
            self._ingest(amount='100000.01', seq=11)
        self.assertEqual(huge.exception.code, 'upi_amount_exceeded')
        with self.assertRaises(UrecvError) as imps_cap:
            self._ingest(amount='500000.01', rail='imps', vpa='', mmid=MMID, phone=PHONE, seq=12)
        self.assertEqual(imps_cap.exception.code, 'imps_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.rail == 'imps'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(vpa='', rail='imps', mmid=MMID, phone=PHONE, seq=13)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(vpa='bob@oksbi', seq=14)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteUrecvStore(path)
            service = UrecvService(
                UrecvPolicy(receiver_ifsc=RECEIVER),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': 'None',
                    'credit': 'None',
                },
            )
            row = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Persist', rail='upi', vpa=VPA,
                destination_account='1001',
            )
            loaded = SqliteUrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('vpa', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['vpa_masked'], 'a*a@oksbi')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=15)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('a*a@oksbi', exported['file'])
        self.assertNotIn(VPA, exported['file'])
        self.assertNotIn(inbound.rrn, exported['file'])
        with self.assertRaises(UrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'rrn': rrn(16), 'amount': '250.00', 'name': 'Ada',
                    'send_ifsc': SENDER, 'recv_ifsc': RECEIVER,
                    'rail': 'upi', 'vpa': VPA,
                },
            )
        self.assertEqual(ctx.exception.code, 'urecv_forbidden')

    def test_claim_wrong_key_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=17)
        with self.assertRaises(UrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                rail='imps', mmid=MMID, phone=PHONE, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            rail='upi', vpa=VPA, destination_account='1001', nickname='Ada VPA',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        with self.assertRaises(UrecvError) as again:
            self.service.request_return(
                inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')

    def test_neft_queues_on_sunday_and_rtgs_below_minimum(self):
        self._key(
            nickname='Ada NEFT', rail='neft', vpa='', ifsc=RECEIVER,
            account_number=EXT_ACCOUNT, pan=PAN,
        )
        self.now[0] = ts(2024, 6, 16, 11, 0)
        queued, _ = self._ingest(
            seq=18, rail='neft', vpa='', ifsc=RECEIVER, account_number=EXT_ACCOUNT, pan=PAN,
        )
        self.assertEqual(queued.status, 'queued')
        with self.assertRaises(UrecvError) as below:
            self._ingest(
                seq=19, rail='rtgs', vpa='', ifsc=RECEIVER, account_number=EXT_ACCOUNT,
                pan=PAN, amount='199999.99',
            )
        self.assertEqual(below.exception.code, 'rtgs_amount_below_minimum')


if __name__ == '__main__':
    unittest.main()
