import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money
from utility.precv import (
    MemoryPrecvStore,
    PrecvError,
    PrecvKey,
    PrecvPolicy,
    PrecvService,
    SpiClock,
    SqlitePrecvStore,
    brazil_holidays,
    compose_end_to_end_id,
    compose_pix1,
    cnpj_check_digit_ok,
    cpf_check_digit_ok,
    easter_gregorian,
    key_fingerprint,
    mask_dict_key,
    mask_end_to_end_id,
    mask_ispb,
    normalize_dict_key,
    normalize_ispb,
    parse_pix1,
    split_pix1_file,
)

BRT = timezone(timedelta(hours=-3))
RECEIVER = '00360305'
SENDER = '60701190'
CPF = '52998224725'
EMAIL = 'ada@example.com'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BRT).timestamp()


def e2e(seq=1, day='20240614', ispb=SENDER):
    return compose_end_to_end_id(ispb, day, seq)


class FoundationTests(unittest.TestCase):
    def test_ispb_dict_key_and_end_to_end_id(self):
        self.assertEqual(normalize_ispb('360305'), '00360305')
        self.assertEqual(mask_ispb('00360305'), '00****05')
        with self.assertRaises(PrecvError) as ctx:
            normalize_ispb('12')
        self.assertEqual(ctx.exception.code, 'invalid_ispb')
        self.assertTrue(cpf_check_digit_ok(CPF))
        self.assertFalse(cpf_check_digit_ok('11111111111'))
        self.assertTrue(cnpj_check_digit_ok('11222333000181'))
        kind, value = normalize_dict_key('cpf', '529.982.247-25')
        self.assertEqual((kind, value), ('cpf', CPF))
        self.assertEqual(mask_dict_key('cpf', CPF), '***.***.***-25')
        self.assertEqual(mask_dict_key('email', EMAIL), 'a***@example.com')
        self.assertEqual(normalize_dict_key('phone', '11999887766')[1], '+5511999887766')
        evp = '550e8400-e29b-41d4-a716-446655440000'
        self.assertEqual(normalize_dict_key('random', evp)[0], 'evp')
        self.assertEqual(len(key_fingerprint('cpf', CPF)), 64)
        composed = e2e(1)
        self.assertEqual(len(composed), 32)
        self.assertTrue(composed.startswith('E6070119020240614'))
        self.assertEqual(mask_end_to_end_id(composed)[-4:], composed[-4:])
        self.assertIn('************', mask_end_to_end_id(composed))

    def test_spi_clock_night_weekend_and_tiradentes(self):
        clock = SpiClock(tz_offset_hours=-3, night_start_hour=20, night_end_hour=6)
        friday_day = ts(2024, 6, 14, 11, 0)
        friday_night = ts(2024, 6, 14, 21, 0)
        self.assertFalse(clock.is_night(friday_day))
        self.assertTrue(clock.is_night(friday_night))
        self.assertTrue(clock.snapshot(friday_night)['instant'])
        self.assertEqual(clock.cycle_date(friday_day), '20240614')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertTrue(clock.snapshot(saturday)['weekend'])
        tiradentes = date(2024, 4, 21)
        self.assertIn(tiradentes, brazil_holidays(2024))
        self.assertTrue(clock.is_holiday(tiradentes))
        carnival = easter_gregorian(2024) - timedelta(days=47)
        self.assertEqual(carnival, date(2024, 2, 13))

    def test_pix1_roundtrip_rejects_xml(self):
        record = compose_pix1(
            end_to_end_id=e2e(1),
            txid='INV1',
            send_ispb=SENDER,
            recv_ispb=RECEIVER,
            key_type='cpf',
            key_value=CPF,
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        parsed = parse_pix1(record)
        self.assertEqual(parsed['end_to_end_id'], e2e(1))
        self.assertEqual(parsed['key_value'], CPF)
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        masked = compose_pix1(
            end_to_end_id=e2e(1),
            txid='INV1',
            send_ispb=SENDER,
            recv_ispb=RECEIVER,
            key_type='cpf',
            key_value=CPF,
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            mask=True,
        )
        self.assertIn('***.***.***-25', masked)
        self.assertIn('E************', masked)
        self.assertNotIn(CPF, masked)
        with self.assertRaises(PrecvError) as ctx:
            parse_pix1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(PrecvError) as xml:
            split_pix1_file('<?xml version="1.0"?><!DOCTYPE pix><PIX1></PIX1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class PrecvServiceTests(unittest.TestCase):
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

        self.service = PrecvService(
            PrecvPolicy(
                dual_control_threshold=Decimal('10000.00'),
                min_amount=Decimal('0.01'),
                max_amount=Decimal('100000.00'),
                receiver_ispb=RECEIVER,
            ),
            MemoryPrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SpiClock(tz_offset_hours=-3),
        )

    def _key(self, **kwargs):
        return self.service.add_key(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ada CPF'),
            key_type=kwargs.pop('key_type', 'cpf'),
            key_value=kwargs.pop('key_value', CPF),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', e2e(values.pop('seq', 1))),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_ispb': values.pop('send_ispb', SENDER),
            'recv_ispb': values.pop('recv_ispb', RECEIVER),
            'key_type': values.pop('key_type', 'cpf'),
            'key': values.pop('key', CPF),
            'purpose': values.pop('purpose', 'goods'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_key_or_e2e(self):
        enrollment = self._key()
        payload = enrollment.to_dict()
        self.assertNotIn('key_value', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['key_masked'], '***.***.***-25')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('key_value', dumped)
        self.assertNotIn('end_to_end_id', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['key_masked'], '***.***.***-25')
        self.assertTrue(dumped['end_to_end_id_masked'].startswith('E************'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('key_value', snap['inbounds'][0])
        self.assertNotIn('end_to_end_id', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'pix from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_end_to_end_id(self):
        self._key()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_key_does_not_credit_until_dict_claim(self):
        inbound, _ = self._ingest(key='ada@example.com', key_type='email', seq=2)
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer',
            key_type='email', key_value='ada@example.com',
            destination_account='1001', nickname='Ada mail',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(claimed.to_dict()['key_masked'], 'a***@example.com')

    def test_enroll_rematches_unmatched_dict_key(self):
        inbound, _ = self._ingest(seq=3)
        self.assertEqual(inbound.status, 'unmatched')
        self._key()
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_key(PrecvKey(
            key_id='cardkey', userid='alice', nickname='Card', key_type='cpf',
            key_value=CPF, fingerprint=key_fingerprint('cpf', CPF),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(PrecvError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card key', key_type='email', key_value=EMAIL,
                destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._key()
        inbound, _ = self._ingest(name='Blocked Person', seq=4)
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(PrecvError) as ctx:
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
        with self.assertRaises(PrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_night_still_posts_instantly(self):
        self._key()
        self.now[0] = ts(2024, 6, 14, 21, 0)
        inbound, _ = self._ingest(seq=6)
        self.assertEqual(inbound.status, 'posted')
        self.assertTrue(self.service.calendar.is_night(self.now[0]))
        self.assertEqual(len(self.credits), 1)

    def test_med_customer_window_and_staff_window(self):
        self._key()
        inbound, _ = self._ingest(seq=7)
        self.assertEqual(inbound.status, 'posted')
        self.now[0] = inbound.posted_at + (81 * 60)
        with self.assertRaises(PrecvError) as late:
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
        self.assertTrue(self.debits[0][2].startswith('pix return '))
        with self.assertRaises(PrecvError) as again:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(again.exception.code, 'already_returned')

    def test_assign_pause_resume_wrong_receiver_and_sqlite(self):
        inbound, _ = self._ingest(key=EMAIL, key_type='email', seq=8)
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.to_dict()['key_masked'], 'a***@example.com')

        with self.assertRaises(PrecvError) as ctx:
            self._ingest(recv_ispb='60746948', seq=9)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(PrecvError) as huge:
            self._ingest(amount='100000.01', seq=10)
        self.assertEqual(huge.exception.code, 'pix_amount_exceeded')

        token = [row for row in self.service.store.list_keys('alice') if row.key_type == 'email'][0]
        paused = self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        waiting, _ = self._ingest(key=EMAIL, key_type='email', seq=11)
        self.assertEqual(waiting.status, 'unmatched')
        self.service.set_key_status(
            key_id=token.key_id, actor='alice', actor_type='customer', status='active',
        )
        rematched = self.service.store.get_inbound(waiting.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        expired, _ = self._ingest(key='other@example.com', key_type='email', seq=12)
        self.assertEqual(expired.status, 'unmatched')
        self.now[0] = expired.expiry_at + 1
        due = self.service.run_due()
        self.assertTrue(any(row.status == 'expired' for row in due))

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePrecvStore(path)
            service = PrecvService(
                PrecvPolicy(receiver_ispb=RECEIVER),
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
                nickname='Persist', key_type='cpf', key_value=CPF,
                destination_account='1001',
            )
            loaded = SqlitePrecvStore(path).get_key(row.key_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('key_value', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['key_masked'], '***.***.***-25')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._key()
        inbound, _ = self._ingest(seq=13)
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('***.***.***-25', exported['file'])
        self.assertNotIn(CPF, exported['file'])
        self.assertNotIn(inbound.end_to_end_id, exported['file'])
        with self.assertRaises(PrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={
                    'end_to_end_id': e2e(14), 'amount': '250.00', 'name': 'Ada',
                    'send_ispb': SENDER, 'recv_ispb': RECEIVER,
                    'key_type': 'cpf', 'key': CPF,
                },
            )
        self.assertEqual(ctx.exception.code, 'precv_forbidden')

    def test_claim_wrong_key_and_customer_med_within_window(self):
        inbound, _ = self._ingest(seq=15)
        with self.assertRaises(PrecvError) as wrong:
            self.service.claim(
                actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
                key_type='email', key_value=EMAIL, destination_account='1001',
            )
        self.assertEqual(wrong.exception.code, 'not_claimable')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', inbound_id=inbound.inbound_id,
            key_type='cpf', key_value=CPF, destination_account='1001', nickname='Ada CPF',
        )
        self.assertEqual(claimed.status, 'posted')
        returned = self.service.request_return(
            inbound_id=claimed.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)


if __name__ == '__main__':
    unittest.main()
