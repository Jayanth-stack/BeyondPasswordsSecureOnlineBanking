import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.hkrecv import (
    HkClock,
    HkrecvError,
    HkrecvKey,
    HkrecvPolicy,
    HkrecvService,
    MemoryHkrecvStore,
    SqliteHkrecvStore,
    compose_e2e,
    compose_fps1,
    compose_hkid,
    compose_fpsid,
    hk_holidays,
    hkid_check_ok,
    mask_bank,
    mask_e2e,
    mask_proxy,
    normalize_bank_code,
    normalize_e2e,
    normalize_proxy,
    parse_fps1,
    proxy_fingerprint,
    split_fps1_file,
)

HKT = timezone(timedelta(hours=8))
RECEIVER = '099'
SENDER = '004'
PHONE = '+85291234567'
HKID = 'A1234563'
FPSID = '1234567'
EMAIL = 'ada@example.hk'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=HKT).timestamp()


def e2e(seq=1, day='20240614', rail='fps'):
    return compose_e2e(rail, day, seq)


class FoundationTests(unittest.TestCase):
    def test_hkid_fpsid_phone_email_bank_and_e2e(self):
        self.assertEqual(normalize_bank_code('99'), '099')
        self.assertEqual(mask_bank('099'), '0**')
        with self.assertRaises(HkrecvError) as ctx:
            normalize_bank_code('000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(HkrecvError):
            normalize_bank_code('1234')
        self.assertTrue(hkid_check_ok(HKID))
        self.assertFalse(hkid_check_ok('A1234560'))
        self.assertEqual(compose_hkid('A', '123456'), HKID)
        self.assertEqual(compose_fpsid('1234567'), FPSID)
        kind, value = normalize_proxy('mobile', '91234567')
        self.assertEqual((kind, value), ('phone', PHONE))
        self.assertEqual(mask_proxy('phone', PHONE), '+852****4567')
        self.assertEqual(mask_proxy('hkid', HKID), 'A****563')
        self.assertEqual(mask_proxy('email', EMAIL), 'a***@example.hk')
        self.assertEqual(len(proxy_fingerprint('phone', PHONE)), 64)
        composed = e2e(1)
        self.assertEqual(composed, 'FPS20240614000001')
        self.assertEqual(mask_e2e(composed)[-4:], composed[-4:])
        self.assertTrue(mask_e2e(composed).startswith('FPS'))
        with self.assertRaises(HkrecvError) as imad:
            normalize_e2e('20240614KONOHA01000001')
        self.assertEqual(imad.exception.code, 'invalid_reference')

    def test_hk_clock_weekend_and_july1(self):
        clock = HkClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_night = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_session(friday))
        self.assertFalse(clock.is_session(friday_night))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        july1 = date(2024, 7, 1)
        self.assertIn(july1, hk_holidays(2024))
        self.assertTrue(clock.is_holiday(july1))
        self.assertEqual(clock.value_date(friday_night, rail='chats').isoformat(), '2024-06-17')
        self.assertEqual(clock.value_date(friday, rail='ecg').isoformat(), '2024-06-17')
        self.assertTrue(clock.snapshot(friday, rail='fps')['always_on'])
        self.assertTrue(clock.is_cny_night(friday_night))

    def test_fps1_roundtrip_rejects_xml(self):
        record = compose_fps1(
            e2e=e2e(1),
            rail='fps',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        parsed = parse_fps1(record)
        self.assertEqual(parsed['e2e'], e2e(1))
        self.assertEqual(parsed['proxy'], PHONE)
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        self.assertEqual(parsed['currency'], 'HKD')
        masked = compose_fps1(
            e2e=e2e(1),
            rail='fps',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=True,
        )
        self.assertIn('+852****4567', masked)
        self.assertNotIn('91234567', masked)
        self.assertNotIn(e2e(1), masked)
        with self.assertRaises(HkrecvError) as ctx:
            parse_fps1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(HkrecvError) as xml:
            split_fps1_file('<?xml version="1.0"?><!DOCTYPE fps><FPS1></FPS1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(HkrecvError) as clearing:
            parse_fps1(record.replace('FPS', 'FEDWIRE'))
        self.assertEqual(clearing.exception.code, 'invalid_file')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class HkrecvServiceTests(unittest.TestCase):
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

        self.service = HkrecvService(
            HkrecvPolicy(
                dual_control_threshold=Decimal('100000.00'),
                min_amount=Decimal('0.01'),
                receiver_bank=RECEIVER,
                hkdusd=Decimal('0.13'),
            ),
            MemoryHkrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=HkClock(),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ada mobile'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', PHONE),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', e2e(values.pop('seq', 1))),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_bank': values.pop('send_bank', SENDER),
            'recv_bank': values.pop('recv_bank', RECEIVER),
            'proxy_type': values.pop('proxy_type', 'phone'),
            'proxy': values.pop('proxy', PHONE),
            'rail': values.pop('rail', 'fps'),
            'purpose': values.pop('purpose', 'goods'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_proxy_or_e2e(self):
        enrollment = self._key()
        payload = enrollment.to_dict()
        self.assertNotIn('proxy', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['proxy_masked'], '+852****4567')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('proxy', dumped)
        self.assertNotIn('e2e', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['proxy_masked'], '+852****4567')
        self.assertEqual(dumped['e2e_masked'][-4:], inbound.e2e[-4:])
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['inbounds'][0])
        self.assertNotIn('e2e', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '32.50', 'fps from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_e2e(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_proxy_does_not_credit_until_claim(self):
        inbound, _ = self._ingest(proxy=HKID, proxy_type='hkid', seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            proxy_type='hkid', proxy=HKID,
            destination_account='1001', nickname='Ada hkid',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('****', claimed.to_dict()['proxy_masked'])

    def test_enroll_rematches_unmatched_proxy(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(HkrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', proxy_type='phone',
            proxy=PHONE, fingerprint=proxy_fingerprint('phone', PHONE),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(HkrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', proxy_type='hkid', proxy=HKID,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(HkrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '32.50')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('200.00')
        self._key()
        inbound, _ = self._ingest(amount='250.00', seq=5)
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(HkrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_fps_night_still_posts_ecg_queues_until_next_session(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 18, 0)
        fps_in, _ = self._ingest(seq=6, rail='fps')
        self.assertEqual(fps_in.status, 'posted')
        self.assertFalse(self.service.calendar.is_session(self.now[0]))
        ecg_in, _ = self._ingest(seq=7, rail='ecg', proxy=FPSID, proxy_type='fpsid')
        self.assertEqual(ecg_in.status, 'unmatched')
        self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Ada FPSID', proxy_type='fpsid', proxy=FPSID, destination_account='1001',
        )
        queued = self.service.store.get_inbound(ecg_in.inbound_id)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(queued.reason, 'ecg_session')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_cny_night_cap(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 19, 0)
        with self.assertRaises(HkrecvError) as ctx:
            self._ingest(seq=20, amount='200000.01', currency='CNY')
        self.assertEqual(ctx.exception.code, 'cny_night_cap')

    def test_return_window_and_staff_return(self):
        self._key()
        inbound, _ = self._ingest(seq=8)
        self.assertEqual(inbound.status, 'posted')
        self.now[0] = inbound.posted_at + (25 * 60 * 60)
        with self.assertRaises(HkrecvError) as late:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '32.50')
        self.assertTrue(self.debits[0][2].startswith('fps return '))
        with self.assertRaises(HkrecvError) as again:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(proxy=HKID, proxy_type='hkid', seq=9)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertIn('****', assigned.to_dict()['proxy_masked'])

        with self.assertRaises(HkrecvError) as ctx:
            self._ingest(recv_bank='004', seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(HkrecvError) as huge:
            self._ingest(amount='1000000.01', rail='fps', seq=11)
        self.assertEqual(huge.exception.code, 'fps_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.proxy_type == 'hkid'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(proxy=HKID, proxy_type='hkid', seq=12)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(proxy='+85298765432', proxy_type='phone', seq=13)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteHkrecvStore(path)
            service = HkrecvService(
                HkrecvPolicy(receiver_bank=RECEIVER),
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
                nickname='Persist', proxy_type='phone', proxy=PHONE,
                destination_account='1001',
            )
            loaded = SqliteHkrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('proxy', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['proxy_masked'], '+852****4567')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=14)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('+852****4567', exported['file'])
        self.assertNotIn('91234567', exported['file'])
        self.assertNotIn(inbound.e2e, exported['file'])
        with self.assertRaises(HkrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'end_to_end_id': e2e(15), 'amount': '250.00', 'name': 'Ada',
                    'send_bank': SENDER, 'recv_bank': RECEIVER,
                    'proxy_type': 'phone', 'proxy': PHONE,
                },
            )
        self.assertEqual(ctx.exception.code, 'hkrecv_forbidden')

    def test_claim_wrong_proxy_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=16)
        with self.assertRaises(HkrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                proxy_type='hkid', proxy=HKID, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            proxy_type='phone', proxy=PHONE, destination_account='1001', nickname='Ada mobile',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)


if __name__ == '__main__':
    unittest.main()
