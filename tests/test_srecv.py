import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.srecv import (
    MemorySrecvStore,
    SrecvError,
    SrecvKey,
    SrecvPolicy,
    SrecvService,
    SpeiClock,
    SqliteSrecvStore,
    clabe_check_digit_ok,
    compose_cep1,
    compose_clabe,
    compose_codi1,
    compose_rfc,
    compose_tracking_key,
    destination_fingerprint,
    easter_gregorian,
    mask_clabe,
    mask_phone,
    mask_rfc,
    mask_tracking_key,
    mexico_holidays,
    normalize_bank,
    normalize_clabe,
    normalize_phone,
    normalize_rfc,
    parse_cep1,
    rfc_check_digit_ok,
    split_cep1_file,
)

CT = timezone(timedelta(hours=-6))
RECEIVER = '646'
SENDER = '012'
CLABE = compose_clabe('646', '180', '1')
RFC = compose_rfc('GODE561231GR')
PHONE = '+525512345678'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CT).timestamp()


def tracking(seq=1, day='20240614', bank=SENDER):
    return compose_tracking_key(bank, day, seq)


class FoundationTests(unittest.TestCase):
    def test_clabe_rfc_phone_and_tracking_key(self):
        self.assertEqual(normalize_bank('12'), '012')
        self.assertEqual(CLABE, '646180000000000012')
        self.assertTrue(clabe_check_digit_ok(CLABE))
        self.assertEqual(normalize_clabe('646180000000000012'), CLABE)
        with self.assertRaises(SrecvError) as ctx:
            normalize_clabe('646180000000000011')
        self.assertEqual(ctx.exception.code, 'invalid_clabe')
        self.assertTrue(rfc_check_digit_ok(RFC))
        self.assertEqual(normalize_rfc('GODE-561231-GR8'), RFC)
        self.assertEqual(mask_rfc(RFC), 'G**********R8')
        self.assertEqual(normalize_phone('5512345678'), PHONE)
        self.assertEqual(normalize_phone('525512345678'), PHONE)
        self.assertEqual(mask_phone(PHONE), '***-***-5678')
        self.assertEqual(mask_clabe(CLABE), '**************0012')
        composed = tracking(1)
        self.assertEqual(composed, 'KNH012202406140000001')
        self.assertTrue(mask_tracking_key(composed).startswith('KNH************'))
        self.assertEqual(len(destination_fingerprint('spei', CLABE, '')), 64)
        self.assertNotEqual(
            destination_fingerprint('spei', CLABE, ''),
            destination_fingerprint('codi', '', PHONE),
        )

    def test_banxico_clock_session_weekend_and_independence_day(self):
        clock = SpeiClock(open_hour=6, cutoff_hour=18, tz_offset_hours=-6)
        friday = ts(2024, 6, 14, 11, 0)
        after = ts(2024, 6, 14, 18, 0)
        night = ts(2024, 6, 14, 21, 0)
        self.assertTrue(clock.is_session(friday))
        self.assertFalse(clock.is_session(after))
        self.assertFalse(clock.is_session(night))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        self.assertIn(date(2024, 9, 16), mexico_holidays(2024))
        self.assertTrue(clock.is_holiday(date(2024, 9, 16)))
        easter = easter_gregorian(2024)
        self.assertEqual(easter, date(2024, 3, 31))

    def test_cep1_codi1_roundtrip_rejects_xml(self):
        record = compose_cep1(
            tracking_key=tracking(1),
            send_bank=SENDER,
            recv_bank=RECEIVER,
            clabe=CLABE,
            rfc=RFC,
            phone='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            rail='spei',
            mask=False,
        )
        parsed = parse_cep1(record)
        self.assertEqual(parsed['tracking_key'], tracking(1))
        self.assertEqual(parsed['clabe'], CLABE)
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        masked = compose_cep1(
            tracking_key=tracking(1),
            send_bank=SENDER,
            recv_bank=RECEIVER,
            clabe=CLABE,
            rfc=RFC,
            phone='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            rail='spei',
            mask=True,
        )
        self.assertIn('**************0012', masked)
        self.assertIn('KNH************', masked)
        self.assertNotIn(CLABE, masked)
        self.assertNotIn(RFC, masked)
        with self.assertRaises(SrecvError) as ctx:
            parse_cep1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(SrecvError) as xml:
            split_cep1_file('<?xml version="1.0"?><!DOCTYPE spei><CEP1></CEP1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        codi = compose_codi1(phone=PHONE, amount=Decimal('80.00'), tracking_key=tracking(2), mask=False)
        self.assertIn(PHONE, codi)
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class SrecvServiceTests(unittest.TestCase):
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

        self.service = SrecvService(
            SrecvPolicy(
                dual_control_threshold=Decimal('200000.00'),
                min_amount=Decimal('0.01'),
                session_max=Decimal('1000000.00'),
                online_max=Decimal('8000.00'),
                codi_max=Decimal('8000.00'),
                receiver_bank=RECEIVER,
            ),
            MemorySrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SpeiClock(tz_offset_hours=-6),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ada CLABE'),
            rail=kwargs.pop('rail', 'spei'),
            clabe=kwargs.pop('clabe', CLABE),
            rfc=kwargs.pop('rfc', RFC),
            phone=kwargs.pop('phone', ''),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'tracking_key': values.pop('tracking_key', tracking(values.pop('seq', 1))),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_bank': values.pop('send_bank', SENDER),
            'recv_bank': values.pop('recv_bank', RECEIVER),
            'rail': values.pop('rail', 'spei'),
            'clabe': values.pop('clabe', CLABE),
            'rfc': values.pop('rfc', RFC),
            'purpose': values.pop('purpose', 'goods'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_clabe_rfc_or_tracking(self):
        enrollment = self._key()
        payload = enrollment.to_dict()
        self.assertNotIn('clabe', payload)
        self.assertNotIn('rfc', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['clabe_masked'], '**************0012')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('clabe', dumped)
        self.assertNotIn('tracking_key', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['clabe_masked'], '**************0012')
        self.assertTrue(dumped['tracking_key_masked'].startswith('KNH************'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('clabe', snap['inbounds'][0])
        self.assertNotIn('tracking_key', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'spei from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_tracking_key(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_key_does_not_credit_until_claim(self):
        inbound, _ = self._ingest(phone=PHONE, rail='codi', clabe='', rfc='', seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            rail='codi', phone=PHONE,
            destination_account='1001', nickname='Ada CoDi',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(claimed.to_dict()['phone_masked'], '***-***-5678')

    def test_enroll_rematches_unmatched_clabe(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(SrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', rail='spei',
            clabe=CLABE, rfc=RFC, phone='', fingerprint=destination_fingerprint('spei', CLABE, ''),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(SrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', rail='codi', phone=PHONE,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(SrecvError) as ctx:
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
        with self.assertRaises(SrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_spei_en_linea_posts_after_cutoff_large_spei_queues(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 21, 0)
        inbound, _ = self._ingest(seq=6)
        self.assertEqual(inbound.status, 'posted')
        self.assertFalse(self.service.calendar.is_session(self.now[0]))
        large, _ = self._ingest(amount='8000.01', seq=61)
        self.assertEqual(large.status, 'queued')
        self.assertEqual(len(self.credits), 1)
        self.now[0] = ts(2024, 6, 17, 11, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_codi_posts_24_7_and_customer_devolucion_windows(self):
        self._key(nickname='Ada CoDi', rail='codi', clabe='', rfc='', phone=PHONE)
        self.now[0] = ts(2024, 6, 15, 21, 0)
        inbound, _ = self._ingest(rail='codi', clabe='', rfc='', phone=PHONE, seq=7)
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0][2], 'codi from Ada Lovelace')
        self.now[0] = inbound.posted_at + (25 * 60 * 60)
        with self.assertRaises(SrecvError) as late:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertTrue(self.debits[0][2].startswith('codi return '))

    def test_spei_customer_devolucion_closes_after_next_session(self):
        self._key()
        inbound, _ = self._ingest(seq=8)
        self.assertEqual(inbound.status, 'posted')
        within = self.service.request_return(
            inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(within.status, 'returned')
        inbound2, _ = self._ingest(seq=81)
        self.now[0] = ts(2024, 6, 17, 18, 1)
        with self.assertRaises(SrecvError) as closed:
            self.service.request_return(
                inbound_id=inbound2.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=inbound2.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(phone=PHONE, rail='codi', clabe='', rfc='', seq=9)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.to_dict()['phone_masked'], '***-***-5678')

        with self.assertRaises(SrecvError) as ctx:
            self._ingest(recv_bank='012', seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(SrecvError) as huge:
            self._ingest(amount='1000000.01', seq=11)
        self.assertEqual(huge.exception.code, 'spei_amount_exceeded')
        with self.assertRaises(SrecvError) as codi_cap:
            self._ingest(amount='8000.01', rail='codi', clabe='', rfc='', phone=PHONE, seq=12)
        self.assertEqual(codi_cap.exception.code, 'codi_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.rail == 'codi'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(phone=PHONE, rail='codi', clabe='', rfc='', seq=13)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(phone='+525598765432', rail='codi', clabe='', rfc='', seq=14)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSrecvStore(path)
            service = SrecvService(
                SrecvPolicy(receiver_bank=RECEIVER),
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
                nickname='Persist', rail='spei', clabe=CLABE, rfc=RFC,
                destination_account='1001',
            )
            loaded = SqliteSrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('clabe', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['clabe_masked'], '**************0012')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=15)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('**************0012', exported['file'])
        self.assertNotIn(CLABE, exported['file'])
        self.assertNotIn(inbound.tracking_key, exported['file'])
        self.assertNotIn(RFC, exported['file'])
        with self.assertRaises(SrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'tracking_key': tracking(16), 'amount': '250.00', 'name': 'Ada',
                    'send_bank': SENDER, 'recv_bank': RECEIVER,
                    'rail': 'spei', 'clabe': CLABE, 'rfc': RFC,
                },
            )
        self.assertEqual(ctx.exception.code, 'srecv_forbidden')

    def test_claim_wrong_key_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=17)
        with self.assertRaises(SrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                rail='codi', phone=PHONE, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            rail='spei', clabe=CLABE, rfc=RFC, destination_account='1001', nickname='Ada CLABE',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        with self.assertRaises(SrecvError) as again:
            self.service.request_return(
                inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')


if __name__ == '__main__':
    unittest.main()
