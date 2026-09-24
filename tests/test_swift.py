import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.swift import (
    AmountError,
    FxBook,
    MemorySwiftStore,
    SqliteSwiftStore,
    SwiftError,
    SwiftPolicy,
    SwiftService,
    Target2Calendar,
    compose_mt103,
    compose_mur,
    compose_uetr,
    compute_fee,
    easter_gregorian,
    iban_check_digit_ok,
    mask_iban,
    normalize_bic,
    normalize_iban,
    parse_money,
    target2_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_iban_mod97_accepts_known_samples(self):
        self.assertTrue(iban_check_digit_ok('GB82WEST12345698765432'))
        self.assertEqual(normalize_iban('gb82 west 12345698765432'), 'GB82WEST12345698765432')
        self.assertEqual(normalize_iban('DE89 3704 0044 0532 0130 00'), 'DE89370400440532013000')
        with self.assertRaises(SwiftError) as ctx:
            normalize_iban('DE89370400440532013001')
        self.assertEqual(ctx.exception.code, 'invalid_iban')
        with self.assertRaises(SwiftError):
            normalize_iban('DE89')

    def test_iban_length_must_match_country(self):
        with self.assertRaises(SwiftError) as ctx:
            normalize_iban('NL91ABNA04171643000')
        self.assertEqual(ctx.exception.code, 'invalid_iban')

    def test_mask_iban_never_shows_the_middle(self):
        self.assertEqual(mask_iban('DE89370400440532013000'), 'DE****3000')
        self.assertNotIn('37040044', mask_iban('DE89370400440532013000'))

    def test_bic_iso9362(self):
        self.assertEqual(normalize_bic('deutdeff'), 'DEUTDEFFXXX')
        self.assertEqual(normalize_bic('NWBKGB2L'), 'NWBKGB2LXXX')
        self.assertEqual(normalize_bic('DEUTDEFF500'), 'DEUTDEFF500')
        with self.assertRaises(SwiftError) as ctx:
            normalize_bic('DEUT0EFF')
        self.assertEqual(ctx.exception.code, 'invalid_bic')
        with self.assertRaises(SwiftError):
            normalize_bic('XXXXZZ12')

    def test_fx_book_converts_and_respects_jpy_minor_units(self):
        book = FxBook()
        eur = book.quote('100.00', 'EUR')
        self.assertEqual(eur.debit_usd, '108.00')
        jpy = book.quote('10000', 'JPY')
        self.assertEqual(jpy.amount, '10000')
        self.assertEqual(jpy.debit_usd, '67.00')
        with self.assertRaises(SwiftError):
            book.quote('10', 'ZZZ')
        with self.assertRaises(AmountError):
            parse_money('nope')

    def test_charge_bearer_fees(self):
        self.assertEqual(compute_fee('OUR'), Decimal('45.00'))
        self.assertEqual(compute_fee('SHA'), Decimal('35.00'))
        self.assertEqual(compute_fee('BEN'), Decimal('15.00'))
        self.assertEqual(compute_fee('OUR', waived=True), Decimal('0.00'))

    def test_uetr_and_mur_and_mt103(self):
        uetr = compose_uetr()
        self.assertRegex(uetr, r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
        self.assertEqual(compose_mur('20240617', 'KONOUS33XXX', 1), '20240617KONOUS33000001')
        with self.assertRaises(SwiftError) as ctx:
            compose_mur('2024-06-17', 'KONOUS33', 1)
        self.assertEqual(ctx.exception.code, 'invalid_mur')
        fields = compose_mt103(
            reference='REF1',
            value_date='20240617',
            currency='EUR',
            amount=Decimal('100.00'),
            ordering_name='Alice',
            beneficiary_name='Ada Lovelace',
            beneficiary_iban='DE****3000',
            beneficiary_account='',
            beneficiary_bic='DEUTDEFFXXX',
            charge='SHA',
            purpose='goods',
            uetr=uetr,
        )
        self.assertEqual(fields['23B'], 'CRED')
        self.assertEqual(fields['32A'], '20240617EUR100,00')
        self.assertEqual(fields['71A'], 'SHA')
        self.assertEqual(fields['57A'], 'DEUTDEFF')
        self.assertEqual(fields['121'], uetr)
        self.assertIn('DE****3000', fields['59'])

    def test_target2_skips_weekends_may1_and_easter(self):
        calendar = Target2Calendar(cutoff_hour=16, tz_offset_hours=-4)
        friday_before = ts(2024, 6, 14, 15, 0)
        friday_after = ts(2024, 6, 14, 16, 30)
        self.assertEqual(calendar.value_date(friday_before).isoformat(), '2024-06-17')
        self.assertEqual(calendar.value_date(friday_after).isoformat(), '2024-06-18')
        self.assertTrue(calendar.snapshot(friday_after)['after_cutoff'])
        may1 = ts(2024, 5, 1, 10, 0)
        self.assertIn(datetime(2024, 5, 1).date(), target2_holidays(2024))
        self.assertEqual(calendar.value_date(may1).isoformat(), '2024-05-03')
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        good_friday = ts(2024, 3, 29, 10, 0)
        self.assertEqual(calendar.value_date(good_friday).isoformat(), '2024-04-03')


class SwiftServiceTests(unittest.TestCase):
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

        policy = SwiftPolicy(dual_control_threshold=Decimal('10000.00'))
        self.service = SwiftService(
            policy,
            MemorySwiftStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=-4),
            fx=FxBook({'EUR': Decimal('1.08'), 'JPY': Decimal('0.0067'), 'USD': Decimal('1')}),
            uetr_fn=lambda: '11111111-2222-4333-8444-555555555555',
        )

    def _add(self, nickname='Berlin Checking', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            bic=kwargs.pop('bic', 'DEUTDEFF'),
            iban=kwargs.pop('iban', 'DE89370400440532013000'),
            street=kwargs.pop('street', 'Taunusanlage 12'),
            city=kwargs.pop('city', 'Frankfurt'),
            country=kwargs.pop('country', 'DE'),
            postal=kwargs.pop('postal', '60325'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_iban(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('iban', payload)
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['iban_masked'], 'DE****3000')
        self.assertEqual(payload['bic'], 'DEUTDEFFXXX')
        snap = self.service.snapshot('alice')
        self.assertNotIn('iban', snap['beneficiaries'][0])
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', currency='EUR', trace_id='s1',
        )
        self.assertTrue(created)
        self.assertNotIn('iban', wire.to_dict())
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(wire.debit_usd, '108.00')
        self.assertEqual(wire.uetr, '11111111-2222-4333-8444-555555555555')
        self.assertEqual(self.debits[0], ('1001', '108.00', 'swift to Berlin Checking'))
        self.assertEqual(self.debits[1][1], '35.00')

    def test_country_mismatch_and_iban_required_for_sepa(self):
        with self.assertRaises(SwiftError) as ctx:
            self._add(bic='NWBKGB2L')
        self.assertEqual(ctx.exception.code, 'invalid_country')
        with self.assertRaises(SwiftError) as ctx:
            self._add(iban='')
        self.assertEqual(ctx.exception.code, 'invalid_iban')

    def test_non_iban_country_uses_account_number(self):
        bene = self.service.add_beneficiary(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Tokyo', legal_name='Ada Lovelace', bic='MHCBJPJT',
            account_number='99887766', street='1 Marunouchi', city='Tokyo',
            country='JP', default_account='1001', default_currency='JPY',
        )
        self.assertEqual(bene.to_dict()['account_last4'], '7766')
        self.assertEqual(bene.to_dict()['iban_masked'], '')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='10000', currency='JPY', trace_id='jp1',
        )
        self.assertEqual(wire.debit_usd, '67.00')
        self.assertEqual(wire.status, 'sent')

    def test_same_day_send_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', currency='EUR', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', currency='EUR', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.wire_id, again.wire_id)
        self.assertEqual(len([row for row in self.debits if row[1] == '43.20']), 1)

    def test_after_cutoff_queues_until_value_date(self):
        self.now[0] = ts(2024, 6, 14, 16, 30)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', currency='EUR', trace_id='late',
        )
        self.assertEqual(wire.status, 'queued')
        self.assertEqual(wire.value_date, '20240618')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 18, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0], ('1001', '54.00', 'swift to Berlin Checking'))

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', currency='EUR', trace_id='ofac',
        )
        self.assertEqual(wire.status, 'held')
        self.assertTrue(wire.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(SwiftError) as ctx:
            self.service.release_wire(wire_id=wire.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            wire_id=wire.wire_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(self.debits[0][1], '86.40')

    def test_dual_control_uses_usd_equivalent(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', currency='EUR', trace_id='hv',
        )
        self.assertEqual(wire.status, 'pending_release')
        self.assertEqual(wire.debit_usd, '10800.00')
        self.assertEqual(self.debits, [])
        with self.assertRaises(SwiftError) as ctx:
            self.service.release_wire(wire_id=wire.wire_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_wire(wire_id=wire.wire_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10800.00')

    def test_credit_account_and_pause_and_archive(self):
        bene = self._add()
        with self.assertRaises(SwiftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00', currency='EUR',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(SwiftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00', currency='EUR',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')
        closed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(SwiftError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_nsf_does_not_assign_uetr(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(SwiftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='30.00', currency='EUR', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['wire'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['wire'].uetr, '')

    def test_complete_assigns_mur_recall_credits_usd(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='60.00', currency='EUR', trace_id='c1',
        )
        completed = self.service.complete_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(completed.status, 'completed')
        self.assertTrue(completed.mur.startswith('20240617KONOUS33'))
        with self.assertRaises(SwiftError) as ctx:
            self.service.recall_wire(wire_id=wire.wire_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='15.00', currency='EUR', trace_id='c2',
        )
        recalled = self.service.recall_wire(
            wire_id=other.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '16.20')

    def test_customer_can_cancel_queued_not_sent(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', currency='EUR', trace_id='q1',
        )
        cancelled = self.service.cancel_wire(
            wire_id=wire.wire_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = ts(2024, 6, 14, 11, 0)
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', currency='EUR', trace_id='live2',
        )
        self.assertEqual(live.status, 'sent')
        with self.assertRaises(SwiftError) as ctx:
            self.service.cancel_wire(wire_id=live.wire_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_sqlite_roundtrip_masks_iban(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteSwiftStore(path)
            service = SwiftService(
                SwiftPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=-4),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Commerzbank', legal_name='Ada Lovelace', bic='COBADEFF',
                iban='DE89370400440532013000', street='Kaiserplatz', city='Frankfurt',
                country='DE', postal='60311', default_account='1001',
            )
            reloaded = SqliteSwiftStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.iban, 'DE89370400440532013000')
            self.assertNotIn('iban', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['iban_masked'], 'DE****3000')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
