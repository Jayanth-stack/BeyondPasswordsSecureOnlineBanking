import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.spei import (
    MemorySpeiStore,
    SpeiClock,
    SpeiError,
    SpeiPolicy,
    SpeiService,
    SqliteSpeiStore,
    clabe_check_digit_ok,
    compose_cep1,
    compose_clabe,
    compose_codi1,
    compose_rfc,
    compose_tracking_key,
    easter_gregorian,
    mask_clabe,
    mask_rfc,
    mask_tracking_key,
    mexico_holidays,
    normalize_clabe,
    normalize_phone,
    normalize_rfc,
    parse_cep1,
    rfc_check_digit_ok,
    split_cep1_file,
)

CT = timezone(timedelta(hours=-6))
CLABE = '012180000000000015'
CLABE_BANAMEX = '002180123456789013'
RFC = 'GODE561231GR8'
PHONE = '+525512345678'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CT).timestamp()


class FoundationTests(unittest.TestCase):
    def test_clabe_checksum_compose_and_mask(self):
        self.assertTrue(clabe_check_digit_ok(CLABE))
        self.assertEqual(compose_clabe('12', '180', '1'), CLABE)
        self.assertEqual(normalize_clabe('012-180-00000000001-5'), CLABE)
        self.assertEqual(mask_clabe(CLABE), '**************0015')
        with self.assertRaises(SpeiError) as ctx:
            normalize_clabe('012180000000000016')
        self.assertEqual(ctx.exception.code, 'invalid_clabe')
        with self.assertRaises(SpeiError):
            normalize_clabe('000000000000000000')

    def test_rfc_sat_check_digit_and_mask(self):
        self.assertTrue(rfc_check_digit_ok(RFC))
        self.assertEqual(compose_rfc('GODE561231GR'), RFC)
        self.assertEqual(normalize_rfc('GODE-561231-GR8'), RFC)
        self.assertEqual(mask_rfc(RFC)[0], 'G')
        self.assertTrue('*' in mask_rfc(RFC))
        with self.assertRaises(SpeiError) as ctx:
            normalize_rfc('GODE561231GR0')
        self.assertEqual(ctx.exception.code, 'invalid_rfc')

    def test_mexican_phone_and_tracking_key(self):
        self.assertEqual(normalize_phone('55 1234 5678'), PHONE)
        self.assertEqual(normalize_phone('+52 55 1234 5678'), PHONE)
        tracking = compose_tracking_key('646', '20240614', 1)
        self.assertEqual(tracking, 'KNH646202406140000001')
        self.assertEqual(mask_tracking_key(tracking)[-4:], '0001')
        with self.assertRaises(SpeiError) as ctx:
            compose_tracking_key('646', '2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_cep1_compose_parse_and_reject_xml(self):
        tracking = compose_tracking_key('646', '20240614', 7)
        raw = compose_cep1(
            tracking_key=tracking,
            send_bank='646',
            recv_bank='012',
            clabe=CLABE,
            rfc=RFC,
            phone=PHONE,
            amount=Decimal('25.50'),
            purpose='family',
            status='completed',
            rail='spei',
            mask=False,
        )
        parsed = parse_cep1(raw)
        self.assertEqual(parsed['tracking_key'], tracking)
        self.assertEqual(parsed['amount'], '25.50')
        self.assertEqual(parsed['settlement_method'], 'spei')
        masked = compose_cep1(
            tracking_key=tracking, send_bank='646', recv_bank='012', clabe=CLABE,
            rfc=RFC, phone='', amount=Decimal('1.00'), purpose='other',
            status='completed', rail='spei', mask=True,
        )
        with self.assertRaises(SpeiError) as ctx:
            parse_cep1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(SpeiError) as xml_ctx:
            split_cep1_file('<?xml version="1.0"?><CEP/>')
        self.assertEqual(xml_ctx.exception.code, 'invalid_file')
        codi = compose_codi1(phone=PHONE, amount=Decimal('80.00'), tracking_key=tracking, mask=True)
        self.assertTrue(codi.startswith('CODI1|1|'))
        self.assertNotIn('5512345678', codi)

    def test_banxico_holidays_and_session_clock(self):
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        holidays = mexico_holidays(2024)
        self.assertIn(datetime(2024, 2, 5).date(), holidays)
        self.assertIn(datetime(2024, 3, 18).date(), holidays)
        self.assertIn(datetime(2024, 3, 28).date(), holidays)
        self.assertIn(datetime(2024, 3, 29).date(), holidays)
        self.assertIn(datetime(2024, 9, 16).date(), holidays)
        clock = SpeiClock(open_hour=6, cutoff_hour=18, tz_offset_hours=-6)
        inside = ts(2024, 6, 14, 11, 0)
        after = ts(2024, 6, 14, 18, 30)
        self.assertTrue(clock.is_session(inside))
        self.assertFalse(clock.is_session(after))
        snap = clock.snapshot(after)
        self.assertTrue(snap['after_cutoff'])
        independence = ts(2024, 9, 16, 11, 0)
        self.assertTrue(clock.snapshot(independence)['holiday'])
        self.assertFalse(clock.is_session(independence))


class SpeiServiceTests(unittest.TestCase):
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

        policy = SpeiPolicy(
            outbound_fee=Decimal('0.00'),
            dual_control_threshold=Decimal('200000.00'),
            session_max=Decimal('1000000.00'),
            online_max=Decimal('8000.00'),
            codi_max=Decimal('8000.00'),
        )
        self.service = SpeiService(
            policy,
            MemorySpeiStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SpeiClock(open_hour=6, cutoff_hour=18, tz_offset_hours=-6),
        )

    def _add(self, nickname='BBVA Nomina', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            rail=kwargs.pop('rail', 'spei'),
            clabe=kwargs.pop('clabe', CLABE),
            rfc=kwargs.pop('rfc', RFC),
            phone=kwargs.pop('phone', ''),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_clabe_rfc_or_tracking(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('clabe', payload)
        self.assertNotIn('rfc', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['clabe_masked'], '**************0015')
        self.assertEqual(payload['bank_code'], '012')
        snap = self.service.snapshot('alice')
        self.assertNotIn('clabe', snap['beneficiaries'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        dumped = payment.to_dict()
        self.assertNotIn('tracking_key', dumped)
        self.assertNotIn('clabe', dumped)
        self.assertTrue(dumped['tracking_key_masked'].startswith('KNH************'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(len(payment.tracking_key), 21)
        self.assertEqual(self.debits[0], ('1001', '100.00', 'spei to BBVA Nomina'))

    def test_off_session_queues_large_spei_but_codi_and_online_post(self):
        bene = self._add()
        self.now[0] = ts(2024, 6, 14, 18, 30)
        queued, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='8000.01', trace_id='night',
        )
        self.assertTrue(created)
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.debits, [])
        online, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='8000.00', trace_id='online',
        )
        self.assertEqual(online.status, 'completed')
        self.assertEqual(self.debits[0][2], 'spei to BBVA Nomina')

        codi = self._add(
            nickname='CoDi Cel', rail='codi', clabe='', rfc='', phone='5512345678',
        )
        self.assertEqual(codi.to_dict()['phone_masked'][-4:], '5678')
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=codi.beneficiary_id, amount='80.00', trace_id='codi',
        )
        self.assertEqual(sent.status, 'completed')
        self.assertEqual(sent.settlement_method, 'codi')
        with self.assertRaises(SpeiError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=codi.beneficiary_id, amount='8000.01', trace_id='cap',
            )
        self.assertEqual(ctx.exception.code, 'codi_amount_exceeded')

        self.now[0] = ts(2024, 6, 17, 11, 0)
        released = self.service.run_due('alice')
        self.assertEqual(released[0].status, 'completed')
        self.assertTrue(released[0].tracking_key)

    def test_credit_account_forbidden_and_duplicate_fingerprint(self):
        with self.assertRaises(SpeiError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        self._add()
        with self.assertRaises(SpeiError) as dup:
            self._add(nickname='Other', clabe=CLABE)
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
        self.assertEqual(self.debits[0][2], 'spei to BBVA Nomina')

        big = self._add(nickname='Payroll', clabe=CLABE_BANAMEX)
        pending, _ = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=big.beneficiary_id, amount='200000.00', trace_id='dual',
        )
        self.assertEqual(pending.status, 'pending_release')
        with self.assertRaises(SpeiError) as same:
            self.service.release_payment(
                payment_id=pending.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(same.exception.code, 'same_approver')
        done = self.service.release_payment(
            payment_id=pending.payment_id, actor='approver', actor_type='tier2',
        )
        self.assertEqual(done.status, 'completed')

    def test_codi_return_window_and_spei_devolucion(self):
        codi = self._add(nickname='CoDi', rail='codi', clabe='', rfc='', phone='5512345678')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=codi.beneficiary_id, amount='40.00', trace_id='med',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][2].split()[0], 'codi')

        later, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=codi.beneficiary_id, amount='15.00', trace_id='late',
        )
        self.now[0] = later.completed_at + 25 * 3600
        with self.assertRaises(SpeiError) as closed:
            self.service.recall_payment(
                payment_id=later.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(closed.exception.code, 'return_window_closed')
        staff = self.service.recall_payment(
            payment_id=later.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(staff.status, 'recalled')

        spei = self._add(nickname='WireLike')
        posted, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=spei.beneficiary_id, amount='12.00', trace_id='exp',
        )
        self.now[0] = posted.completed_at + 91 * 86400
        with self.assertRaises(SpeiError) as med:
            self.service.recall_payment(
                payment_id=posted.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(med.exception.code, 'devolucion_window_closed')

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
            store = SqliteSpeiStore(path)
            service = SpeiService(
                SpeiPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=SpeiClock(tz_offset_hours=-6),
            )
            saved = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Banorte', legal_name='Ada Lovelace', rail='spei',
                clabe=CLABE, rfc=RFC, default_account='1001',
            )
            reloaded = SqliteSpeiStore(path).get_beneficiary(saved.beneficiary_id)
            self.assertEqual(reloaded.nickname, 'Banorte')
            self.assertEqual(reloaded.clabe, CLABE)
            dumped = reloaded.to_dict()
            self.assertEqual(dumped['clabe_masked'], '**************0015')
            self.assertNotIn('clabe', dumped)
            self.assertNotIn('fingerprint', dumped)
        finally:
            os.unlink(path)

    def test_export_is_masked_cep1(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='8.00', trace_id='exp1',
        )
        exported = self.service.export_cep1(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('CEP1|1|', exported['cep1'])
        self.assertNotIn(CLABE, exported['cep1'])
        self.assertNotIn(RFC, exported['cep1'])
        self.assertNotIn(payment.tracking_key, exported['cep1'])
        with self.assertRaises(SpeiError) as ctx:
            self.service.export_cep1(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'spei_forbidden')

    def test_paused_beneficiary_cannot_send(self):
        bene = self._add()
        self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(SpeiError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='5.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')


if __name__ == '__main__':
    unittest.main()
