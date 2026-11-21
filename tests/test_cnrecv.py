import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.cnrecv import (
    CnClock,
    CnrecvError,
    CnrecvKey,
    CnrecvPolicy,
    CnrecvService,
    MemoryCnrecvStore,
    SqliteCnrecvStore,
    compose_e2e,
    compose_cn1,
    cn_holidays,
    chinese_outcome,
    mask_bank,
    mask_e2e,
    mask_proxy,
    normalize_bank_code,
    normalize_e2e,
    normalize_chinese,
    normalize_proxy,
    parse_fx_rate,
    parse_cn1,
    proxy_fingerprint,
    split_cn1_file,
)

CST = timezone(timedelta(hours=8))
RECEIVER = '104100000004'
SENDER = '102100099996'
PHONE = '+8613812345678'
CNAPS = '1041000000046222021234567890'
NID = '11010119900307002X'
EMAIL = 'zhang@example.cn'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CST).timestamp()


def e2e(seq=1, day='20240614', rail='ibps'):
    return compose_e2e(rail, day, seq)


class FoundationTests(unittest.TestCase):
    def test_bank_proxy_phone_email_chinese_and_e2e(self):
        self.assertEqual(normalize_bank_code('104100000004'), '104100000004')
        self.assertEqual(mask_bank('104100000004'), '104***')
        with self.assertRaises(CnrecvError) as ctx:
            normalize_bank_code('000000000000')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(CnrecvError):
            normalize_bank_code('104100000005')
        self.assertEqual(normalize_chinese('张伟'), '张伟')
        self.assertEqual(chinese_outcome('张伟', '张伟'), 'match')
        self.assertEqual(chinese_outcome('张伟', '张'), 'close_match')
        self.assertEqual(chinese_outcome('张伟', '李娜'), 'no_match')
        kind, value = normalize_proxy('mobile', '13812345678')
        self.assertEqual((kind, value), ('phone', PHONE))
        self.assertEqual(mask_proxy('phone', PHONE), '+86****5678')
        self.assertEqual(mask_proxy('cnaps', CNAPS), '104-****7890')
        self.assertEqual(mask_proxy('email', EMAIL), 'z***@example.cn')
        self.assertEqual(mask_proxy('nid', NID)[-4:], '7002')
        self.assertEqual(len(proxy_fingerprint('phone', PHONE)), 64)
        composed = e2e(1)
        self.assertEqual(composed, 'IBPS20240614000001')
        self.assertEqual(mask_e2e(composed)[-4:], composed[-4:])
        self.assertTrue(mask_e2e(composed).startswith('IBPS'))
        with self.assertRaises(CnrecvError) as imad:
            normalize_e2e('20240614KONOHA01000001')
        self.assertEqual(imad.exception.code, 'invalid_reference')
        self.assertEqual(parse_fx_rate('0.137000'), Decimal('0.137000'))

    def test_cn_clock_weekend_and_spring_festival(self):
        clock = CnClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_night = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_session(friday, rail='beps'))
        self.assertFalse(clock.is_session(friday_night, rail='beps'))
        self.assertTrue(clock.is_session(friday_night, rail='ibps'))
        self.assertFalse(clock.is_session(friday_night, rail='cips'))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        self.assertIn(date(2024, 2, 10), cn_holidays(2024))
        self.assertTrue(clock.is_holiday(date(2024, 2, 19)))
        festival = ts(2024, 2, 10, 11, 0)
        self.assertEqual(clock.value_date(festival, rail='beps').isoformat(), '2024-02-20')
        self.assertEqual(clock.value_date(friday_night, rail='beps').isoformat(), '2024-06-17')
        self.assertEqual(clock.value_date(friday_night, rail='cips').isoformat(), '2024-06-17')
        self.assertTrue(clock.snapshot(friday, rail='ibps')['always_on'])
        after_cips = ts(2024, 6, 14, 17, 0)
        self.assertTrue(clock.is_after_cutoff(after_cips, rail='cips'))

    def test_cn1_roundtrip_rejects_xml(self):
        record = compose_cn1(
            e2e=e2e(1),
            rail='ibps',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='张伟',
            amount=Decimal('10000.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        parsed = parse_cn1(record)
        self.assertEqual(parsed['e2e'], e2e(1))
        self.assertEqual(parsed['proxy'], PHONE)
        self.assertEqual(parsed['amount'], Decimal('10000.00'))
        self.assertEqual(parsed['currency'], 'CNY')
        self.assertEqual(parsed['chinese'], '张伟')
        masked = compose_cn1(
            e2e=e2e(1),
            rail='ibps',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='张伟',
            amount=Decimal('10000.00'),
            purpose='goods',
            status='posted',
            mask=True,
        )
        self.assertIn('+86****5678', masked)
        self.assertIn('张***伟', masked)
        self.assertNotIn('13812345678', masked)
        self.assertNotIn(e2e(1), masked)
        with self.assertRaises(CnrecvError) as ctx:
            parse_cn1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(CnrecvError) as xml:
            split_cn1_file('<?xml version="1.0"?><!DOCTYPE zgn><CN1></CN1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(CnrecvError) as clearing:
            parse_cn1(record.replace('IBPS', 'FEDWIRE'))
        self.assertEqual(clearing.exception.code, 'invalid_file')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class CnrecvServiceTests(unittest.TestCase):
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

        self.service = CnrecvService(
            CnrecvPolicy(
                dual_control_threshold=Decimal('1000000.00'),
                min_amount=Decimal('0.01'),
                receiver_bank=RECEIVER,
                cnyusd=Decimal('0.137000'),
            ),
            MemoryCnrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=CnClock(),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Zhang mobile'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', PHONE),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', e2e(values.pop('seq', 1), rail=values.get('rail', 'ibps'))),
            'amount': values.pop('amount', '10000.00'),
            'name': values.pop('name', '张伟'),
            'send_bank': values.pop('send_bank', SENDER),
            'recv_bank': values.pop('recv_bank', RECEIVER),
            'proxy_type': values.pop('proxy_type', 'phone'),
            'proxy': values.pop('proxy', PHONE),
            'rail': values.pop('rail', 'ibps'),
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
        self.assertEqual(payload['proxy_masked'], '+86****5678')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('proxy', dumped)
        self.assertNotIn('e2e', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertNotIn('kana', dumped)
        self.assertNotIn('chinese', dumped)
        self.assertEqual(dumped['proxy_masked'], '+86****5678')
        self.assertEqual(dumped['e2e_masked'][-4:], inbound.e2e[-4:])
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['inbounds'][0])
        self.assertNotIn('e2e', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '1370.00', 'ibps from 张伟'))
        self.assertEqual(snap['fx_cnyusd'], '0.137000')

    def test_enrolled_is_idempotent_by_e2e(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_proxy_does_not_credit_until_claim(self):
        inbound, _ = self._ingest(proxy=EMAIL, proxy_type='email', seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            proxy_type='email', proxy=EMAIL,
            destination_account='1001', nickname='Zhang email',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('***', claimed.to_dict()['proxy_masked'])

    def test_enroll_rematches_unmatched_proxy(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(CnrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', proxy_type='phone',
            proxy=PHONE, fingerprint=proxy_fingerprint('phone', PHONE),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(CnrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', proxy_type='email', proxy=EMAIL,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(CnrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '1370.00')

    def test_chinese_mismatch_holds_until_staff_override(self):
        self._key()
        inbound, _ = self._ingest(seq=40, chinese='李娜')
        self.assertEqual(inbound.status, 'held')
        self.assertEqual(inbound.reason, 'chinese')
        self.assertEqual(inbound.chinese_outcome, 'no_match')
        self.assertEqual(self.credits, [])
        dumped = inbound.to_dict()
        self.assertNotIn('李娜', str(dumped))
        posted = self.service.override_chinese(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='ops',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('5000.00')
        self._key()
        inbound, _ = self._ingest(amount='10000.00', seq=5)
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(CnrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_ibps_night_still_posts_beps_queues_until_next_session(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 18, 0)
        ibps_in, _ = self._ingest(seq=6, rail='ibps')
        self.assertEqual(ibps_in.status, 'posted')
        self.assertTrue(self.service.calendar.is_after_cutoff(self.now[0], rail='beps'))
        beps_in, _ = self._ingest(seq=7, rail='beps', proxy=CNAPS, proxy_type='cnaps')
        self.assertEqual(beps_in.status, 'unmatched')
        self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Zhang CNAPS', proxy_type='cnaps', proxy=CNAPS, destination_account='1001',
        )
        queued = self.service.store.get_inbound(beps_in.inbound_id)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(queued.reason, 'beps_session')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_return_window_and_staff_return(self):
        self._key()
        inbound, _ = self._ingest(seq=8)
        self.assertEqual(inbound.status, 'posted')
        self.now[0] = inbound.posted_at + (25 * 60 * 60)
        with self.assertRaises(CnrecvError) as late:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '1370.00')
        self.assertTrue(self.debits[0][2].startswith('ibps return '))
        with self.assertRaises(CnrecvError) as again:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(proxy=EMAIL, proxy_type='email', seq=9)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertIn('***', assigned.to_dict()['proxy_masked'])

        with self.assertRaises(CnrecvError) as ctx:
            self._ingest(recv_bank='102100099996', seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(CnrecvError) as huge:
            self._ingest(amount='10000000.01', rail='ibps', seq=11)
        self.assertEqual(huge.exception.code, 'ibps_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.proxy_type == 'email'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(proxy=EMAIL, proxy_type='email', seq=12)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(proxy='+8613999999999', proxy_type='phone', seq=13)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCnrecvStore(path)
            service = CnrecvService(
                CnrecvPolicy(receiver_bank=RECEIVER),
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
            loaded = SqliteCnrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('proxy', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['proxy_masked'], '+86****5678')
            posted, _ = service.ingest(
                actor='teller', actor_type='tier1',
                values={
                    'end_to_end_id': e2e(90),
                    'amount': '10000.00',
                    'name': '张伟',
                    'send_bank': SENDER,
                    'recv_bank': RECEIVER,
                    'proxy_type': 'phone',
                    'proxy': PHONE,
                    'chinese': '张伟',
                },
            )
            loaded_in = SqliteCnrecvStore(path).get_inbound(posted.inbound_id)
            self.assertEqual(loaded_in.chinese, '张伟')
            self.assertNotIn('chinese', loaded_in.to_dict())
            self.assertEqual(loaded_in.to_dict()['chinese_masked'], '张***伟')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=14)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('+86****5678', exported['file'])
        self.assertNotIn('13812345678', exported['file'])
        self.assertNotIn(inbound.e2e, exported['file'])
        with self.assertRaises(CnrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'end_to_end_id': e2e(15), 'amount': '10000.00', 'name': 'Ada',
                    'send_bank': SENDER, 'recv_bank': RECEIVER,
                    'proxy_type': 'phone', 'proxy': PHONE,
                },
            )
        self.assertEqual(ctx.exception.code, 'cnrecv_forbidden')

    def test_claim_wrong_proxy_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=16)
        with self.assertRaises(CnrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                proxy_type='email', proxy=EMAIL, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            proxy_type='phone', proxy=PHONE, destination_account='1001', nickname='Zhang mobile',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)


if __name__ == '__main__':
    unittest.main()
