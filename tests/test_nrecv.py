import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.nrecv import (
    AusClock,
    MemoryNrecvStore,
    NrecvError,
    NrecvKey,
    NrecvPolicy,
    NrecvService,
    SqliteNrecvStore,
    abn_check_ok,
    acn_check_ok,
    aus_holidays,
    compose_acn,
    compose_e2e,
    compose_npp1,
    easter_gregorian,
    mask_bsb,
    mask_e2e,
    mask_payid,
    normalize_bsb,
    normalize_payid,
    parse_npp1,
    payid_fingerprint,
    split_npp1_file,
)

AEST = timezone(timedelta(hours=10))
RECEIVER = '062000'
SENDER = '013000'
EMAIL = 'ada@example.com'
PHONE = '+61412345678'
ABN = '51824753556'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=AEST).timestamp()


def e2e(seq=1, day='20240614'):
    return compose_e2e(day, seq)


class FoundationTests(unittest.TestCase):
    def test_bsb_payid_and_e2e(self):
        self.assertEqual(normalize_bsb('062-000'), '062000')
        self.assertEqual(mask_bsb('062000'), '062-***')
        with self.assertRaises(NrecvError) as ctx:
            normalize_bsb('12')
        self.assertEqual(ctx.exception.code, 'invalid_bsb')
        self.assertTrue(abn_check_ok(ABN))
        self.assertFalse(abn_check_ok('11111111111'))
        self.assertTrue(acn_check_ok(compose_acn('00000001')))
        value, kind = normalize_payid('ada@example.com', payid_type='email')
        self.assertEqual((kind, value), ('email', EMAIL))
        self.assertEqual(mask_payid(EMAIL, 'email'), 'a*a@example.com')
        self.assertEqual(normalize_payid('0412345678', payid_type='mobile')[0], PHONE)
        self.assertEqual(normalize_payid(ABN, payid_type='abn')[0], ABN)
        self.assertEqual(len(payid_fingerprint('email', EMAIL)), 64)
        composed = e2e(1)
        self.assertEqual(composed, 'NPP20240614000001')
        self.assertEqual(mask_e2e(composed)[-4:], composed[-4:])
        self.assertTrue(mask_e2e(composed).startswith('*'))
        with self.assertRaises(NrecvError) as imad:
            from utility.nrecv import normalize_e2e
            normalize_e2e('20240614KONOHA01000001')
        self.assertEqual(imad.exception.code, 'invalid_reference')

    def test_aus_clock_weekend_and_anzac(self):
        clock = AusClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_night = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_session(friday))
        self.assertFalse(clock.is_session(friday_night))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        anzac = date(2024, 4, 25)
        self.assertIn(anzac, aus_holidays(2024))
        self.assertTrue(clock.is_holiday(anzac))
        easter = easter_gregorian(2024)
        self.assertEqual(easter, date(2024, 3, 31))
        self.assertEqual(clock.value_date(friday_night).isoformat(), '2024-06-17')

    def test_npp1_roundtrip_rejects_xml(self):
        record = compose_npp1(
            e2e=e2e(1),
            rail='osko',
            payid_type='email',
            payid=EMAIL,
            bsb=RECEIVER,
            account_number='1001',
            abn='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        parsed = parse_npp1(record)
        self.assertEqual(parsed['e2e'], e2e(1))
        self.assertEqual(parsed['payid'], EMAIL)
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        masked = compose_npp1(
            e2e=e2e(1),
            rail='osko',
            payid_type='email',
            payid=EMAIL,
            bsb=RECEIVER,
            account_number='1001',
            abn='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=True,
        )
        self.assertIn('a*a@example.com', masked)
        self.assertNotIn(EMAIL, masked)
        self.assertNotIn(e2e(1), masked)
        with self.assertRaises(NrecvError) as ctx:
            parse_npp1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(NrecvError) as xml:
            split_npp1_file('<?xml version="1.0"?><!DOCTYPE npp><NPP1></NPP1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(NrecvError) as clearing:
            parse_npp1(record.replace('AUNPP', 'FEDWIRE'))
        self.assertEqual(clearing.exception.code, 'invalid_file')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class NrecvServiceTests(unittest.TestCase):
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

        self.service = NrecvService(
            NrecvPolicy(
                dual_control_threshold=Decimal('10000.00'),
                min_amount=Decimal('0.01'),
                receiver_bsb=RECEIVER,
            ),
            MemoryNrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=AusClock(),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ada mail'),
            payid_type=kwargs.pop('payid_type', 'email'),
            payid=kwargs.pop('payid', EMAIL),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', e2e(values.pop('seq', 1))),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_bsb': values.pop('send_bsb', SENDER),
            'recv_bsb': values.pop('recv_bsb', RECEIVER),
            'payid_type': values.pop('payid_type', 'email'),
            'payid': values.pop('payid', EMAIL),
            'rail': values.pop('rail', 'osko'),
            'purpose': values.pop('purpose', 'goods'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_payid_or_e2e(self):
        enrollment = self._key()
        payload = enrollment.to_dict()
        self.assertNotIn('payid', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['payid_masked'], 'a*a@example.com')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('payid', dumped)
        self.assertNotIn('e2e', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['payid_masked'], 'a*a@example.com')
        self.assertEqual(dumped['e2e_masked'][-4:], inbound.e2e[-4:])
        snap = self.service.snapshot('alice')
        self.assertNotIn('payid', snap['inbounds'][0])
        self.assertNotIn('e2e', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'npp from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_e2e(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_payid_does_not_credit_until_claim(self):
        inbound, _ = self._ingest(payid=PHONE, payid_type='phone', seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            payid_type='phone', payid=PHONE,
            destination_account='1001', nickname='Ada mobile',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('***', claimed.to_dict()['payid_masked'])

    def test_enroll_rematches_unmatched_payid(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(NrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', payid_type='email',
            payid=EMAIL, fingerprint=payid_fingerprint('email', EMAIL),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(NrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', payid_type='phone', payid=PHONE,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(NrecvError) as ctx:
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
        with self.assertRaises(NrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_osko_night_still_posts_de_queues_until_session(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 18, 0)
        osko, _ = self._ingest(seq=6, rail='osko')
        self.assertEqual(osko.status, 'posted')
        self.assertFalse(self.service.calendar.is_session(self.now[0]))
        de_in, _ = self._ingest(seq=7, rail='de', payid=ABN, payid_type='abn')
        self.assertEqual(de_in.status, 'unmatched')
        self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Ada ABN', payid_type='abn', payid=ABN, destination_account='1001',
        )
        queued = self.service.store.get_inbound(de_in.inbound_id)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(queued.reason, 'de_session')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_return_window_and_staff_return(self):
        self._key()
        inbound, _ = self._ingest(seq=8)
        self.assertEqual(inbound.status, 'posted')
        self.now[0] = inbound.posted_at + (25 * 60 * 60)
        with self.assertRaises(NrecvError) as late:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '250.00')
        self.assertTrue(self.debits[0][2].startswith('npp return '))
        with self.assertRaises(NrecvError) as again:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(payid=PHONE, payid_type='phone', seq=9)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertIn('***', assigned.to_dict()['payid_masked'])

        with self.assertRaises(NrecvError) as ctx:
            self._ingest(recv_bsb='082000', seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(NrecvError) as huge:
            self._ingest(amount='10000.01', rail='osko', seq=11)
        self.assertEqual(huge.exception.code, 'npp_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.payid_type == 'phone'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(payid=PHONE, payid_type='phone', seq=12)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(payid='other@example.com', payid_type='email', seq=13)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteNrecvStore(path)
            service = NrecvService(
                NrecvPolicy(receiver_bsb=RECEIVER),
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
                nickname='Persist', payid_type='email', payid=EMAIL,
                destination_account='1001',
            )
            loaded = SqliteNrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('payid', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['payid_masked'], 'a*a@example.com')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=14)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('a*a@example.com', exported['file'])
        self.assertNotIn(EMAIL, exported['file'])
        self.assertNotIn(inbound.e2e, exported['file'])
        with self.assertRaises(NrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'end_to_end_id': e2e(15), 'amount': '250.00', 'name': 'Ada',
                    'send_bsb': SENDER, 'recv_bsb': RECEIVER,
                    'payid_type': 'email', 'payid': EMAIL,
                },
            )
        self.assertEqual(ctx.exception.code, 'nrecv_forbidden')

    def test_claim_wrong_payid_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=16)
        with self.assertRaises(NrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                payid_type='phone', payid=PHONE, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            payid_type='email', payid=EMAIL, destination_account='1001', nickname='Ada mail',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)


if __name__ == '__main__':
    unittest.main()
