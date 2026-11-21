"""Fedwire kill-switch, customer_send, beneficiary cap, remaining holidays.

Unique vs still-open coverage PRs #203/#201/#199/#197/#195/#193/#191/#189:
those lock customer_manage/allow_credit/max_wires, Labor/Columbus/Presidents,
Juneteenth/Thanksgiving/Veterans, and Sunday-observed Christmas/July4 — not
WIRE_ENABLED, customer_send, max_beneficiaries, extra_holidays, MLK, Memorial
Day, or weekday New Year.
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import tests  # noqa: F401

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireKillSwitchTests(unittest.TestCase):
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

        def accounts_fn(_userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        self.service = WireService(
            WirePolicy(
                outbound_fee=Decimal('25.00'),
                dual_control_threshold=Decimal('10000.00'),
                max_beneficiaries=1,
            ),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
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
        )

    def test_disabled_blocks_customer_and_staff(self):
        self.service.policy.enabled = False
        with self.assertRaises(WireError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'wire_disabled')
        with self.assertRaises(WireError) as ctx:
            self._add(actor='teller', actor_type='tier1', nickname='StaffAdded')
        self.assertEqual(ctx.exception.code, 'wire_disabled')

    def test_customer_send_false_blocks_customer_staff_can_still_originate(self):
        bene = self._add()
        self.service.policy.customer_send = False
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='cust-blocked',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        self.assertEqual(self.debits, [])
        wire, created = self.service.originate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='staff-ok',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '40.00')

    def test_beneficiary_limit_blocks_second_open_row(self):
        self._add()
        with self.assertRaises(WireError) as ctx:
            self._add(nickname='BoA', account_number='99887766', aba='011401533')
        self.assertEqual(ctx.exception.code, 'beneficiary_limit')

    def test_extra_holiday_shifts_value_date(self):
        calendar = WireCalendar(
            cutoff_hour=17, tz_offset_hours=-4, extra_holidays=('2024-06-14',),
        )
        friday = ts(2024, 6, 14, 11, 0)
        self.assertEqual(calendar.value_date(friday).isoformat(), '2024-06-17')

    def test_mlk_memorial_and_weekday_new_year_shift_value_date(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2024 = us_fed_holidays(2024)
        self.assertIn(date(2024, 1, 15), holidays_2024)
        self.assertIn(date(2024, 5, 27), holidays_2024)
        self.assertIn(date(2024, 1, 1), holidays_2024)
        self.assertEqual(calendar.value_date(ts(2024, 1, 15, 11, 0)).isoformat(), '2024-01-16')
        self.assertEqual(calendar.value_date(ts(2024, 5, 27, 11, 0)).isoformat(), '2024-05-28')
        self.assertEqual(calendar.value_date(ts(2024, 1, 1, 11, 0)).isoformat(), '2024-01-02')


if __name__ == '__main__':
    unittest.main()
