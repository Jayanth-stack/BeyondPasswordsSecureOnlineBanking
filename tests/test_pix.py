import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.pix import (
    MemoryPixStore,
    PixError,
    PixPolicy,
    PixService,
    SpiClock,
    SqlitePixStore,
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
    normalize_cpf,
    normalize_cnpj,
    normalize_dict_key,
    normalize_end_to_end_id,
    normalize_ispb,
    normalize_phone,
    parse_pix1,
    split_pix1_file,
)

BRT = timezone(timedelta(hours=-3))
CPF = '52998224725'
CNPJ = '11222333000181'
PHONE = '+5511987654321'
EMAIL = 'maria@example.com'
EVP = '550e8400-e29b-41d4-a716-446655440000'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BRT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_ispb_pads_and_masks(self):
        self.assertEqual(normalize_ispb('360305'), '00360305')
        self.assertEqual(mask_ispb('00360305'), '00****05')
        with self.assertRaises(PixError) as ctx:
            normalize_ispb('123')
        self.assertEqual(ctx.exception.code, 'invalid_ispb')

    def test_cpf_and_cnpj_checksums(self):
        self.assertTrue(cpf_check_digit_ok(CPF))
        self.assertEqual(normalize_cpf('529.982.247-25'), CPF)
        self.assertTrue(cnpj_check_digit_ok(CNPJ))
        self.assertEqual(normalize_cnpj('11.222.333/0001-81'), CNPJ)
        with self.assertRaises(PixError) as ctx:
            normalize_cpf('11111111111')
        self.assertEqual(ctx.exception.code, 'invalid_key')
        with self.assertRaises(PixError):
            normalize_cnpj('00000000000000')

    def test_dict_key_normalize_and_mask(self):
        self.assertEqual(normalize_dict_key('tax_id', '529.982.247-25'), ('cpf', CPF))
        self.assertEqual(normalize_dict_key('phone', '11 98765-4321'), ('phone', PHONE))
        self.assertEqual(normalize_phone('+55 (11) 98765-4321'), PHONE)
        self.assertEqual(normalize_dict_key('random', EVP), ('evp', EVP))
        self.assertEqual(mask_dict_key('cpf', CPF), '***.***.***-25')
        self.assertEqual(mask_dict_key('cnpj', CNPJ), '**.***.***/****-81')
        self.assertEqual(mask_dict_key('email', EMAIL), 'm***@example.com')
        self.assertTrue(mask_dict_key('phone', PHONE).endswith('4321'))
        self.assertTrue(mask_dict_key('evp', EVP).startswith('550e8400'))
        finger = key_fingerprint('cpf', '529.982.247-25')
        self.assertEqual(finger, key_fingerprint('CPF', CPF))
        self.assertEqual(len(finger), 64)

    def test_end_to_end_id_layout(self):
        e2e = compose_end_to_end_id('00360305', '20240614', 1)
        self.assertEqual(len(e2e), 32)
        self.assertTrue(e2e.startswith('E0036030520240614PIX'))
        self.assertEqual(normalize_end_to_end_id(e2e), e2e)
        self.assertEqual(mask_end_to_end_id(e2e), 'E************0001')
        with self.assertRaises(PixError) as ctx:
            compose_end_to_end_id('00360305', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_pix1_compose_parse_and_reject_xml(self):
        e2e = compose_end_to_end_id('00360305', '20240614', 7)
        raw = compose_pix1(
            end_to_end_id=e2e,
            txid='INV42',
            send_ispb='00360305',
            recv_ispb='60701190',
            key_type='cpf',
            key_value=CPF,
            amount=Decimal('25.50'),
            purpose='family',
            status='completed',
            mask=False,
        )
        parsed = parse_pix1(raw)
        self.assertEqual(parsed['end_to_end_id'], e2e)
        self.assertEqual(parsed['amount'], '25.50')
        self.assertEqual(parsed['settlement_method'], 'spi')
        masked = compose_pix1(
            end_to_end_id=e2e, txid='', send_ispb='00360305', recv_ispb='60701190',
            key_type='cpf', key_value=CPF, amount=Decimal('1.00'), purpose='other',
            status='completed', mask=True,
        )
        with self.assertRaises(PixError) as ctx:
            parse_pix1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(PixError) as xml_ctx:
            split_pix1_file('<?xml version="1.0"?><PIX/>')
        self.assertEqual(xml_ctx.exception.code, 'invalid_file')

    def test_brazil_holidays_and_night_window(self):
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        holidays = brazil_holidays(2024)
        self.assertIn(datetime(2024, 2, 12).date(), holidays)  # Carnival Monday
        self.assertIn(datetime(2024, 2, 13).date(), holidays)  # Carnival Tuesday
        self.assertIn(datetime(2024, 9, 7).date(), holidays)
        clock = SpiClock(tz_offset_hours=-3, night_start_hour=20, night_end_hour=6)
        day = ts(2024, 6, 14, 11, 0)
        night = ts(2024, 6, 14, 21, 0)
        self.assertFalse(clock.is_night(day))
        self.assertTrue(clock.is_night(night))
        snap = clock.snapshot(night)
        self.assertTrue(snap['night'])
        self.assertTrue(snap['instant'])
        self.assertEqual(snap['settlement_method'], 'spi')
        independence = ts(2024, 9, 7, 10, 0)
        self.assertTrue(clock.snapshot(independence)['holiday'])


class PixServiceTests(unittest.TestCase):
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

        policy = PixPolicy(
            outbound_fee=Decimal('0.00'),
            dual_control_threshold=Decimal('10000.00'),
            day_max=Decimal('20000.00'),
            night_max=Decimal('1000.00'),
        )
        self.service = PixService(
            policy,
            MemoryPixStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SpiClock(tz_offset_hours=-3),
        )

    def _add(self, nickname='Maria CPF', name='Maria Silva', **kwargs):
        return self.service.add_key(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            key_type=kwargs.pop('key_type', 'cpf'),
            key_value=kwargs.pop('key_value', CPF),
            ispb=kwargs.pop('ispb', '60701190'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_key_or_fingerprint(self):
        key = self._add()
        payload = key.to_dict()
        self.assertNotIn('key_value', payload)
        self.assertNotIn('key_fingerprint', payload)
        self.assertEqual(payload['key_masked'], '***.***.***-25')
        self.assertEqual(payload['ispb_masked'], '60****90')
        snap = self.service.snapshot('alice')
        self.assertNotIn('key_value', snap['keys'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        dumped = payment.to_dict()
        self.assertNotIn('key_value', dumped)
        self.assertNotIn('end_to_end_id', dumped)
        self.assertTrue(dumped['end_to_end_id_masked'].startswith('E************'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(len(payment.end_to_end_id), 32)
        self.assertEqual(self.debits[0], ('1001', '100.00', 'pix to Maria CPF'))

    def test_night_cap_blocks_and_day_cap_allows(self):
        key = self._add()
        self.now[0] = ts(2024, 6, 14, 21, 0)
        with self.assertRaises(PixError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, amount='1000.01', trace_id='night',
            )
        self.assertEqual(ctx.exception.code, 'night_amount_exceeded')
        self.now[0] = ts(2024, 6, 14, 11, 0)
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='1000.01', trace_id='day',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'completed')

    def test_credit_account_forbidden_and_duplicate_fingerprint(self):
        with self.assertRaises(PixError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        self._add()
        with self.assertRaises(PixError) as dup:
            self._add(nickname='Other', key_value='529.982.247-25')
        self.assertEqual(dup.exception.code, 'key_duplicate')

    def test_ofac_hold_then_override_and_dual_control(self):
        key = self._add(name='Blocked Person LLC')
        held, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='50.00', trace_id='ofac',
        )
        self.assertTrue(created)
        self.assertEqual(held.status, 'held')
        self.assertEqual(self.debits, [])
        released = self.service.override_ofac(
            payment_id=held.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][2], 'pix to Maria CPF')

        big = self._add(nickname='Payroll', key_type='cnpj', key_value=CNPJ)
        pending, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            key_id=big.key_id, amount='10000.00', trace_id='dual',
        )
        self.assertEqual(pending.status, 'pending_release')
        with self.assertRaises(PixError) as same:
            self.service.release_payment(
                payment_id=pending.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(same.exception.code, 'same_approver')
        done = self.service.release_payment(
            payment_id=pending.payment_id, actor='approver', actor_type='tier2',
        )
        self.assertEqual(done.status, 'completed')

    def test_customer_med_window_and_staff_med(self):
        key = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='40.00', trace_id='med',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][2].split()[0], 'pix')

        later, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='15.00', trace_id='late',
        )
        self.now[0] = later.completed_at + 81 * 60
        with self.assertRaises(PixError) as closed:
            self.service.recall_payment(
                payment_id=later.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.recall_payment(
            payment_id=later.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'recalled')

        expired, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='12.00', trace_id='exp',
        )
        self.now[0] = expired.completed_at + 91 * 86400
        with self.assertRaises(PixError) as med:
            self.service.recall_payment(
                payment_id=expired.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(med.exception.code, 'med_window_closed')

    def test_idempotent_trace_and_sqlite_roundtrip(self):
        key = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='9.00', trace_id='same',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='9.00', trace_id='same',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len(self.debits), 1)

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePixStore(path)
            service = PixService(
                PixPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=SpiClock(tz_offset_hours=-3),
            )
            saved = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Itau', legal_name='Ada Lovelace', key_type='email',
                key_value=EMAIL, ispb='60701190', default_account='1001',
            )
            reloaded = SqlitePixStore(path).get_key(saved.key_id)
            self.assertEqual(reloaded.nickname, 'Itau')
            self.assertEqual(reloaded.key_value, EMAIL)
            dumped = reloaded.to_dict()
            self.assertEqual(dumped['key_masked'], 'm***@example.com')
            self.assertNotIn('key_value', dumped)
        finally:
            os.unlink(path)

    def test_export_is_masked_pix1(self):
        key = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, amount='8.00', trace_id='exp1',
        )
        exported = self.service.export_pix1(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('PIX1|1|', exported['pix1'])
        self.assertNotIn(CPF, exported['pix1'])
        self.assertNotIn(payment.end_to_end_id, exported['pix1'])
        with self.assertRaises(PixError) as ctx:
            self.service.export_pix1(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'pix_forbidden')

    def test_paused_key_cannot_send(self):
        key = self._add()
        self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(PixError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, amount='5.00',
            )
        self.assertEqual(ctx.exception.code, 'key_paused')


if __name__ == '__main__':
    unittest.main()
