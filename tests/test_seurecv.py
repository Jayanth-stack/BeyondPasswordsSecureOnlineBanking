import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.seurecv import (
    EuClock,
    SeurecvError,
    SeurecvKey,
    SeurecvPolicy,
    SeurecvService,
    MemorySeurecvStore,
    SqliteSeurecvStore,
    compose_e2e,
    compose_se1,
    target2_holidays,
    sepa_name_outcome,
    mask_bank,
    mask_e2e,
    mask_proxy,
    normalize_bank_code,
    normalize_e2e,
    normalize_sepa_name,
    normalize_proxy,
    parse_fx_rate,
    parse_se1,
    proxy_fingerprint,
    split_se1_file,
)

CET = timezone(timedelta(hours=1))
RECEIVER = 'COBADEFFXXX'
SENDER = 'BNPAFRPPXXX'
PHONE = '+4915112345678'
IBAN = 'DE89370400440532013000'
NID = '12345678903'
EMAIL = 'jose@example.de'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CET).timestamp()


def e2e(seq=1, day='20240614', rail='inst'):
    return compose_e2e(rail, day, seq)


class FoundationTests(unittest.TestCase):
    def test_bank_proxy_phone_email_sepa_and_e2e(self):
        self.assertEqual(normalize_bank_code('COBADEFFXXX'), 'COBADEFFXXX')
        self.assertEqual(mask_bank('COBADEFFXXX'), 'COBA***')
        with self.assertRaises(SeurecvError) as ctx:
            normalize_bank_code('XXXXXX')
        self.assertEqual(ctx.exception.code, 'invalid_bank')
        with self.assertRaises(SeurecvError):
            normalize_bank_code('COBA12FFXXX')
        self.assertEqual(normalize_sepa_name('José García'), 'José García')
        self.assertEqual(sepa_name_outcome('José García', 'José García'), 'match')
        self.assertEqual(sepa_name_outcome('José García', 'José'), 'close_match')
        self.assertEqual(sepa_name_outcome('José García', 'Marie Dupont'), 'no_match')
        kind, value = normalize_proxy('mobile', '15112345678')
        self.assertEqual((kind, value), ('phone', PHONE))
        self.assertEqual(mask_proxy('phone', PHONE), '+49****5678')
        self.assertEqual(mask_proxy('iban', IBAN), 'DE89****3000')
        self.assertEqual(mask_proxy('email', EMAIL), 'j***@example.de')
        self.assertEqual(mask_proxy('nid', NID)[-4:], '8903')
        self.assertEqual(len(proxy_fingerprint('phone', PHONE)), 64)
        composed = e2e(1)
        self.assertEqual(composed, 'SCTI20240614000001')
        self.assertEqual(mask_e2e(composed)[-4:], composed[-4:])
        self.assertTrue(mask_e2e(composed).startswith('SCTI'))
        with self.assertRaises(SeurecvError) as imad:
            normalize_e2e('20240614KONOHA01000001')
        self.assertEqual(imad.exception.code, 'invalid_reference')
        self.assertEqual(parse_fx_rate('1.085000'), Decimal('1.085000'))

    def test_eu_clock_weekend_and_new_year(self):
        clock = EuClock()
        friday = ts(2024, 6, 14, 11, 0)
        friday_night = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_session(friday, rail='sct'))
        self.assertFalse(clock.is_session(friday_night, rail='sct'))
        self.assertTrue(clock.is_session(friday_night, rail='inst'))
        self.assertFalse(clock.is_session(friday_night, rail='t2'))
        self.assertEqual(clock.cycle_date(friday), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        self.assertIn(date(2022, 1, 1), target2_holidays(2022))
        self.assertTrue(clock.is_holiday(date(2022, 1, 3)))
        national = ts(2022, 1, 1, 11, 0)
        self.assertEqual(clock.value_date(national, rail='sct').isoformat(), '2022-01-04')
        self.assertEqual(clock.value_date(friday_night, rail='sct').isoformat(), '2024-06-17')
        self.assertEqual(clock.value_date(friday_night, rail='t2').isoformat(), '2024-06-17')
        self.assertTrue(clock.snapshot(friday, rail='inst')['always_on'])
        after_cips = ts(2024, 6, 14, 17, 0)
        self.assertTrue(clock.is_after_cutoff(after_cips, rail='t2'))

    def test_se1_roundtrip_rejects_xml(self):
        record = compose_se1(
            e2e=e2e(1),
            rail='inst',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='José García',
            amount=Decimal('10000.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        parsed = parse_se1(record)
        self.assertEqual(parsed['e2e'], e2e(1))
        self.assertEqual(parsed['proxy'], PHONE)
        self.assertEqual(parsed['amount'], Decimal('10000.00'))
        self.assertEqual(parsed['currency'], 'EUR')
        self.assertEqual(parsed['sepa_name'], 'José García')
        masked = compose_se1(
            e2e=e2e(1),
            rail='inst',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='José García',
            amount=Decimal('10000.00'),
            purpose='goods',
            status='posted',
            mask=True,
        )
        self.assertIn('+49****5678', masked)
        self.assertIn('J***a', masked)
        self.assertNotIn('15112345678', masked)
        self.assertNotIn(e2e(1), masked)
        with self.assertRaises(SeurecvError) as ctx:
            parse_se1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(SeurecvError) as xml:
            split_se1_file('<?xml version="1.0"?><!DOCTYPE sct><SE1></SE1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(SeurecvError) as clearing:
            parse_se1(record.replace('SCTI', 'FEDWIRE'))
        self.assertEqual(clearing.exception.code, 'invalid_file')
        with self.assertRaises(SeurecvError) as inst:
            parse_se1(record.replace('SCTI', 'INST'))
        self.assertEqual(inst.exception.code, 'invalid_file')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class SeurecvServiceTests(unittest.TestCase):
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

        self.service = SeurecvService(
            SeurecvPolicy(
                dual_control_threshold=Decimal('1000000.00'),
                min_amount=Decimal('0.01'),
                receiver_bank=RECEIVER,
                eurusd=Decimal('1.085000'),
            ),
            MemorySeurecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=EuClock(),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Jose mobile'),
            proxy_type=kwargs.pop('proxy_type', 'phone'),
            proxy=kwargs.pop('proxy', PHONE),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', e2e(values.pop('seq', 1), rail=values.get('rail', 'inst'))),
            'amount': values.pop('amount', '10000.00'),
            'name': values.pop('name', 'José García'),
            'send_bank': values.pop('send_bank', SENDER),
            'recv_bank': values.pop('recv_bank', RECEIVER),
            'proxy_type': values.pop('proxy_type', 'phone'),
            'proxy': values.pop('proxy', PHONE),
            'rail': values.pop('rail', 'inst'),
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
        self.assertEqual(payload['proxy_masked'], '+49****5678')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('proxy', dumped)
        self.assertNotIn('e2e', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertNotIn('kana', dumped)
        self.assertNotIn('sepa_name', dumped)
        self.assertEqual(dumped['proxy_masked'], '+49****5678')
        self.assertEqual(dumped['e2e_masked'][-4:], inbound.e2e[-4:])
        snap = self.service.snapshot('alice')
        self.assertNotIn('proxy', snap['inbounds'][0])
        self.assertNotIn('e2e', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '10850.00', 'inst from José García'))
        self.assertEqual(snap['fx_eurusd'], '1.085000')

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
            destination_account='1001', nickname='Jose email',
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
        self.service.store.put_key(SeurecvKey(
            key_id='cardkey', userid='alice', nickname='Card', proxy_type='phone',
            proxy=PHONE, fingerprint=proxy_fingerprint('phone', PHONE),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(SeurecvError) as ctx:
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
        with self.assertRaises(SeurecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '10850.00')

    def test_sepa_mismatch_holds_until_staff_override(self):
        self._key()
        inbound, _ = self._ingest(seq=40, sepa_name='Marie Dupont')
        self.assertEqual(inbound.status, 'held')
        self.assertEqual(inbound.reason, 'sepa')
        self.assertEqual(inbound.sepa_outcome, 'no_match')
        self.assertEqual(self.credits, [])
        dumped = inbound.to_dict()
        self.assertNotIn('Marie Dupont', str(dumped))
        posted = self.service.override_sepa(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='ops',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('5000.00')
        self._key()
        inbound, _ = self._ingest(amount='10000.00', seq=5)
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(SeurecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_inst_night_still_posts_sct_queues_until_next_session(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 18, 0)
        ibps_in, _ = self._ingest(seq=6, rail='inst')
        self.assertEqual(ibps_in.status, 'posted')
        self.assertTrue(self.service.calendar.is_after_cutoff(self.now[0], rail='sct'))
        beps_in, _ = self._ingest(seq=7, rail='sct', proxy=IBAN, proxy_type='iban')
        self.assertEqual(beps_in.status, 'unmatched')
        self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Jose IBAN', proxy_type='iban', proxy=IBAN, destination_account='1001',
        )
        queued = self.service.store.get_inbound(beps_in.inbound_id)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(queued.reason, 'sct_session')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.credits), 2)

    def test_return_window_and_staff_return(self):
        self._key()
        inbound, _ = self._ingest(seq=8)
        self.assertEqual(inbound.status, 'posted')
        self.now[0] = inbound.posted_at + (25 * 60 * 60)
        with self.assertRaises(SeurecvError) as late:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(late.exception.code, 'return_window_closed')
        returned = self.service.return_inbound(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '10850.00')
        self.assertTrue(self.debits[0][2].startswith('inst return '))
        with self.assertRaises(SeurecvError) as again:
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

        with self.assertRaises(SeurecvError) as ctx:
            self._ingest(recv_bank='BNPAFRPPXXX', seq=10)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(SeurecvError) as huge:
            self._ingest(amount='10000000.01', rail='inst', seq=11)
        self.assertEqual(huge.exception.code, 'inst_amount_exceeded')

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

        expired, _ = self._ingest(proxy='+4915999999999', proxy_type='phone', seq=13)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSeurecvStore(path)
            service = SeurecvService(
                SeurecvPolicy(receiver_bank=RECEIVER),
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
            loaded = SqliteSeurecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('proxy', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['proxy_masked'], '+49****5678')
            posted, _ = service.ingest(
                actor='teller', actor_type='tier1',
                values={
                    'end_to_end_id': e2e(90),
                    'amount': '10000.00',
                    'name': 'José García',
                    'send_bank': SENDER,
                    'recv_bank': RECEIVER,
                    'proxy_type': 'phone',
                    'proxy': PHONE,
                    'sepa_name': 'José García',
                },
            )
            loaded_in = SqliteSeurecvStore(path).get_inbound(posted.inbound_id)
            self.assertEqual(loaded_in.sepa_name, 'José García')
            self.assertNotIn('sepa_name', loaded_in.to_dict())
            self.assertEqual(loaded_in.to_dict()['sepa_masked'], 'J***a')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=14)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('+49****5678', exported['file'])
        self.assertNotIn('15112345678', exported['file'])
        self.assertNotIn(inbound.e2e, exported['file'])
        with self.assertRaises(SeurecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'end_to_end_id': e2e(15), 'amount': '10000.00', 'name': 'Ada',
                    'send_bank': SENDER, 'recv_bank': RECEIVER,
                    'proxy_type': 'phone', 'proxy': PHONE,
                },
            )
        self.assertEqual(ctx.exception.code, 'seurecv_forbidden')

    def test_claim_wrong_proxy_and_customer_return_within_window(self):
        inbound, _ = self._ingest(seq=16)
        with self.assertRaises(SeurecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                proxy_type='email', proxy=EMAIL, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            proxy_type='phone', proxy=PHONE, destination_account='1001', nickname='Jose mobile',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)


if __name__ == '__main__':
    unittest.main()
