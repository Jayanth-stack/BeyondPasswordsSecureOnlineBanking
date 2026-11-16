"""Fedwire contracts still untested on master: policy gates, YTD math, OFAC aliases, fee exceptions.

Open coverage PRs already lock GET staff IDOR, HMAC debit/credit classification,
global trace replay, and Juneteenth/Thanksgiving calendars. These cases are the
remaining money-integrity holes: nickname-only OFAC, year-boundary NYD observation,
fee debit exceptions that still transmit principal, and role strings the rest of
the bank treats as staff (`hr`, `Admin`) but Fedwire does not.
"""
import os
import tempfile
import unittest

import tests  # noqa: F401
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from utility.crypto_receipt import generate_receipt
from utility.wire import (
    MemoryWireStore,
    SqliteWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    _classify_money_result,
    compose_omad,
    screen_name,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class ClassifyAndScreenTests(unittest.TestCase):
    def test_truthy_legacy_strings_are_ok_dicts_fail(self):
        self.assertEqual(_classify_money_result(True), 'ok')
        self.assertEqual(_classify_money_result(1), 'ok')
        self.assertEqual(_classify_money_result('done'), 'ok')
        self.assertEqual(_classify_money_result('Amount Debited'), 'ok')
        self.assertEqual(_classify_money_result('Amount Credited'), 'ok')
        self.assertEqual(_classify_money_result(generate_receipt({
            'from_account': 10, 'amount': '40.00', 'status': 'done',
        })), 'failed')
        self.assertEqual(_classify_money_result({'message': 'Amount Debited'}), 'failed')

    def test_ofac_hits_nickname_alias_when_legal_name_is_clean(self):
        hit = screen_name('Ada Lovelace', aliases=('Blocked Person LLC',))
        self.assertTrue(hit.hit)
        self.assertEqual(hit.matched, 'BLOCKED PERSON')
        clear = screen_name('Ada Lovelace', aliases=('Ally Checking',))
        self.assertFalse(clear.hit)

    def test_omad_layout_and_invalid_cycle(self):
        self.assertEqual(compose_omad('20240614', 7), '20240614FRBNY001000007')
        with self.assertRaises(WireError) as ctx:
            compose_omad('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_imad')


class HolidayBoundaryTests(unittest.TestCase):
    def test_christmas_and_mlk_and_memorial_day(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        christmas = ts(2024, 12, 25, 11, 0)
        self.assertIn(date(2024, 12, 25), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(christmas).isoformat(), '2024-12-26')
        mlk = ts(2024, 1, 15, 11, 0)
        self.assertIn(date(2024, 1, 15), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(mlk).isoformat(), '2024-01-16')
        memorial = ts(2024, 5, 27, 11, 0)
        self.assertIn(date(2024, 5, 27), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(memorial).isoformat(), '2024-05-28')

    def test_saturday_new_year_observed_friday_is_missing_from_prior_year_set(self):
        # 2022-01-01 was Saturday; _observed returns 2021-12-31, which lives in
        # the 2022 holiday set. Checking 2021-12-31 uses the 2021 set, so Fedwire
        # still treats that Friday as a business day.
        self.assertIn(date(2021, 12, 31), us_fed_holidays(2022))
        self.assertNotIn(date(2021, 12, 31), us_fed_holidays(2021))
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        friday = ts(2021, 12, 31, 11, 0)
        self.assertTrue(calendar.is_business_day(date(2021, 12, 31)))
        self.assertEqual(calendar.value_date(friday).isoformat(), '2021-12-31')

    def test_extra_holidays_shift_value_date(self):
        calendar = WireCalendar(
            cutoff_hour=17, tz_offset_hours=-4, extra_holidays=('2024-06-14',),
        )
        midday = ts(2024, 6, 14, 11, 0)
        self.assertEqual(calendar.value_date(midday).isoformat(), '2024-06-17')


class WireIntegrityServiceTests(unittest.TestCase):
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

        self.service = WireService(
            WirePolicy(outbound_fee=Decimal('25.00'), dual_control_threshold=Decimal('10000.00')),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            },
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, owner='alice', actor=None, actor_type='customer', **kwargs):
        return self.service.add_beneficiary(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop('nickname', 'Chase Checking'),
            legal_name=kwargs.pop('legal_name', 'Ada Lovelace'),
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_ofac_hold_from_nickname_alias_does_not_debit(self):
        bene = self._add(nickname='Blocked Person', legal_name='Ada Lovelace')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='alias-ofac',
        )
        self.assertEqual(wire.status, 'held')
        self.assertTrue(wire.ofac_hit)
        self.assertEqual(self.debits, [])

    def test_duplicate_nickname_and_fingerprint(self):
        self._add()
        with self.assertRaises(WireError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'beneficiary_duplicate')
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='Ally', account_number='77881234')
        self.assertEqual(ctx.exception.code, 'beneficiary_duplicate')

    def test_max_beneficiaries_and_disabled_policy(self):
        self.service.policy = WirePolicy(max_beneficiaries=1)
        self._add()
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='BoA', account_number='99887766')
        self.assertEqual(ctx.exception.code, 'beneficiary_limit')

        locked = WireService(
            WirePolicy(enabled=False),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
        )
        with self.assertRaises(WireError) as ctx:
            locked.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Chase', legal_name='Ada Lovelace', aba='021000021',
                account_number='77881234', street='1 Federal St', city='New York',
                state='NY', postal='10004', default_account='1001',
            )
        self.assertEqual(ctx.exception.code, 'wire_disabled')

        nosend = WireService(
            WirePolicy(customer_send=False),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
            debit_fn=lambda account, amount, remark: 'Amount Debited',
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )
        bene = nosend.add_beneficiary(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Chase', legal_name='Ada Lovelace', aba='021000021',
            account_number='77881234', street='1 Federal St', city='New York',
            state='NY', postal='10004', default_account='1001',
        )
        with self.assertRaises(WireError) as ctx:
            nosend.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')

    def test_invalid_state_and_zip(self):
        with self.assertRaises(WireError) as ctx:
            self._add(state='XX')
        self.assertEqual(ctx.exception.code, 'invalid_address')
        with self.assertRaises(WireError) as ctx:
            self._add(postal='12')
        self.assertEqual(ctx.exception.code, 'invalid_address')
        zip4 = self._add(nickname='Zip4', account_number='11223344', postal='100041234')
        self.assertEqual(zip4.postal, '100041234')

    def test_already_paused_is_conflict(self):
        bene = self._add()
        self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(WireError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
            )
        self.assertEqual(ctx.exception.code, 'already_paused')

    def test_hr_and_capital_admin_are_not_staff(self):
        bene = self._add(nickname='Blocked', legal_name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='role-1',
        )
        self.assertEqual(wire.status, 'held')
        for actor_type in ('hr', 'Admin', 'Employee', 'manager', 'supervisor'):
            with self.assertRaises(WireError) as ctx:
                self.service.override_ofac(
                    wire_id=wire.wire_id, actor='staff1', actor_type=actor_type,
                )
            self.assertEqual(ctx.exception.code, 'wire_forbidden')
        released = self.service.override_ofac(
            wire_id=wire.wire_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(released.status, 'sent')

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default_account='9999')
        self.assertEqual(bene.default_account, '9999')
        credit = self._add(
            nickname='Visa', account_number='55556666', default_account='1003',
        )
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=credit.beneficiary_id, amount='40.00',
            internal_account='1003', trace_id='empty-credit',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(self.debits[0][0], '1003')

    def test_debit_exception_fails_wire_fee_exception_still_sends(self):
        def boom(account, amount, remark):
            self.debits.append((account, amount, remark))
            raise RuntimeError('ledger down')

        self.service.debit_fn = boom
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='boom-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        self.assertEqual(ctx.exception.extra['wire'].status, 'failed')
        self.assertEqual(ctx.exception.extra['wire'].imad, '')

        calls = {'n': 0}

        def fee_boom(account, amount, remark):
            calls['n'] += 1
            self.debits.append((account, amount, remark))
            if calls['n'] == 1:
                return 'Amount Debited'
            raise RuntimeError('fee rail down')

        self.service.debit_fn = fee_boom
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='30.00', trace_id='fee-boom',
        )
        self.assertEqual(wire.status, 'sent')
        self.assertTrue(wire.imad)
        self.assertEqual(wire.fee_status, 'nsf')
        recalled = self.service.recall_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual([row[1] for row in self.credits], ['30.00'])

    def test_snapshot_ytd_excludes_recalled_and_only_counts_collected_fees(self):
        bene = self._add()
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='ytd-1',
        )
        self.assertEqual(sent.fee_status, 'collected')
        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='15.00', trace_id='ytd-2',
        )
        self.service.recall_wire(wire_id=other.wire_id, actor='teller', actor_type='tier2')
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_sent'], '40.00')
        self.assertEqual(snap['ytd_fees'], '25.00')
        self.assertEqual(snap['recalled_ytd'], '15.00')
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        self.assertNotIn('account_number', snap['wires'][0])

    def test_sqlite_sent_wire_roundtrip_keeps_imad_and_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            service = WireService(
                WirePolicy(outbound_fee=Decimal('25.00')),
                SqliteWireStore(path),
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 5000}},
                calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', aba='021000021',
                account_number='99887766', street='1 Federal St', city='New York',
                state='NY', postal='10004', default_account='1001',
            )
            wire, _ = service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='sqlite-1',
            )
            reloaded = SqliteWireStore(path)
            stored = reloaded.get_wire(wire.wire_id)
            self.assertEqual(stored.status, 'sent')
            self.assertEqual(stored.imad, wire.imad)
            self.assertTrue(stored.imad.startswith('20240614KONOHA01'))
            self.assertNotIn('account_number', stored.to_dict())
            self.assertEqual(reloaded.get_beneficiary(bene.beneficiary_id).account_number, '99887766')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
