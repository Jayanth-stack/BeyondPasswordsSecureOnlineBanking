import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import (
    AmountError,
    MemoryWireStore,
    SqliteWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    aba_check_digit_ok,
    compose_imad,
    compute_fee,
    money_str,
    normalize_aba,
    parse_money,
    screen_name,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_aba_checksum_accepts_known_rtns(self):
        self.assertTrue(aba_check_digit_ok('021000021'))
        self.assertEqual(normalize_aba('21000021'), '021000021')
        self.assertEqual(normalize_aba('021-000-021'), '021000021')
        with self.assertRaises(WireError) as ctx:
            normalize_aba('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_aba')
        with self.assertRaises(WireError):
            normalize_aba('000000000')

    def test_imad_layout(self):
        self.assertEqual(compose_imad('20240614', 'KONOHA01', 1), '20240614KONOHA01000001')
        with self.assertRaises(WireError) as ctx:
            compose_imad('2024-06-14', 'KONOHA01', 1)
        self.assertEqual(ctx.exception.code, 'invalid_imad')

    def test_fee_flat_or_waived(self):
        fee = Decimal('25.00')
        self.assertEqual(compute_fee(Decimal('100.00'), fee), Decimal('25.00'))
        self.assertEqual(compute_fee(Decimal('100.00'), fee, waived=True), Decimal('0.00'))

    def test_ofac_hits_phrase_and_tokens_not_single_word(self):
        hit = screen_name('Mr Blocked Person LLC')
        self.assertTrue(hit.hit)
        self.assertEqual(hit.matched, 'BLOCKED PERSON')
        clear = screen_name('Ada Lovelace')
        self.assertFalse(clear.hit)
        # Single-token watchlist entries must be an exact party match.
        self.assertFalse(screen_name('SMITH', watchlist=('SMITH JR',)).hit)
        exact = screen_name('SANCTIONED ENTITY')
        self.assertTrue(exact.hit)

    def test_cutoff_weekend_and_july4(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        friday_before = ts(2024, 6, 14, 16, 0)
        friday_after = ts(2024, 6, 14, 17, 30)
        self.assertEqual(calendar.value_date(friday_before).isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_after).isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(friday_after)['after_cutoff'])
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(july4).isoformat(), '2024-07-05')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday).isoformat(), '2024-06-17')


class WireServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
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

        policy = WirePolicy(outbound_fee=Decimal('25.00'), dual_control_threshold=Decimal('10000.00'))
        self.service = WireService(
            policy,
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, nickname='Chase Checking', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_number(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['account_last4'], '1234')
        self.assertEqual(payload['aba'], '021000021')
        snap = self.service.snapshot('alice')
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='100.00', trace_id='w1',
        )
        self.assertTrue(created)
        self.assertNotIn('account_number', wire.to_dict())
        self.assertEqual(wire.status, 'sent')
        self.assertTrue(wire.imad.startswith('20240614KONOHA01'))
        self.assertEqual(self.debits[0], ('1001', '100.00', 'wire to Chase Checking'))
        self.assertEqual(self.debits[1][0], '1001')
        self.assertEqual(self.debits[1][1], '25.00')

    def test_same_day_send_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.wire_id, again.wire_id)
        self.assertEqual(len([row for row in self.debits if row[1] == '40.00']), 1)

    def test_after_cutoff_queues_until_next_business_day(self):
        self.now[0] = ts(2024, 6, 14, 17, 30)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', trace_id='late',
        )
        self.assertEqual(wire.status, 'queued')
        self.assertEqual(wire.value_date, '20240617')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0], ('1001', '50.00', 'wire to Chase Checking'))

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(wire.status, 'held')
        self.assertTrue(wire.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(WireError) as ctx:
            self.service.release_wire(wire_id=wire.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            wire_id=wire.wire_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'sent')
        self.assertEqual(self.debits[0][1], '80.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(wire.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(WireError) as ctx:
            self.service.release_wire(wire_id=wire.wire_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_wire(wire_id=wire.wire_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_credit_account_and_pause_and_archive(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')
        closed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(WireError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_nsf_does_not_assign_imad(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='30.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['wire'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['wire'].imad, '')

    def test_complete_assigns_omad_recall_credits(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='60.00', trace_id='c1',
        )
        completed = self.service.complete_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(completed.status, 'completed')
        self.assertTrue(completed.omad.startswith('20240614FRBNY001'))
        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(wire_id=wire.wire_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='15.00', trace_id='c2',
        )
        recalled = self.service.recall_wire(
            wire_id=other.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '15.00')

    def test_customer_can_cancel_queued_not_sent(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', trace_id='q1',
        )
        cancelled = self.service.cancel_wire(
            wire_id=wire.wire_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = ts(2024, 6, 14, 11, 0)
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'sent')
        with self.assertRaises(WireError) as ctx:
            self.service.cancel_wire(wire_id=live.wire_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_sqlite_roundtrip_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteWireStore(path)
            service = WireService(
                WirePolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', aba='021000021',
                account_number='99887766', street='1 Federal St', city='New York',
                state='NY', postal='10004', default_account='1001',
            )
            reloaded = SqliteWireStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '99887766')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['account_last4'], '7766')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
