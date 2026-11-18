"""Fedwire status-machine, policy gates, and remaining holiday contracts.

Open coverage PRs already lock IDOR, HMAC receipts, GET staff mutations,
nickname OFAC, fee exceptions, and Juneteenth/Thanksgiving/NY-boundary
calendars. These cases are still untested on master: terminal-state
rejects, customer_manage / allow_credit / max_wires, beneficiary aliases,
and Sunday-observed Christmas / Labor / Columbus / Presidents Day.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    normalize_account,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class NormalizeAndHolidayTests(unittest.TestCase):
    def test_account_json_float_is_normalized(self):
        self.assertEqual(normalize_account(1001.0), '1001')
        self.assertEqual(normalize_account('1001.0'), '1001')

    def test_labor_columbus_presidents_day_shift_value_date(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        labor = ts(2024, 9, 2, 11, 0)
        self.assertIn(date(2024, 9, 2), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(labor).isoformat(), '2024-09-03')
        columbus = ts(2024, 10, 14, 11, 0)
        self.assertIn(date(2024, 10, 14), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(columbus).isoformat(), '2024-10-15')
        presidents = ts(2024, 2, 19, 11, 0)
        self.assertIn(date(2024, 2, 19), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(presidents).isoformat(), '2024-02-20')

    def test_sunday_christmas_and_july4_are_observed_monday(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.assertIn(date(2022, 12, 26), us_fed_holidays(2022))
        christmas = ts(2022, 12, 25, 11, 0)
        self.assertEqual(calendar.value_date(christmas).isoformat(), '2022-12-27')
        self.assertEqual(calendar.value_date(ts(2022, 12, 26, 11, 0)).isoformat(), '2022-12-27')
        self.assertIn(date(2021, 7, 5), us_fed_holidays(2021))
        july4 = ts(2021, 7, 4, 11, 0)
        self.assertEqual(calendar.value_date(july4).isoformat(), '2021-07-06')


class WireStatusPolicyTests(unittest.TestCase):
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

    def test_terminal_states_reject_staff_actions(self):
        bene = self._add()
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='term-1',
        )
        self.assertEqual(sent.status, 'sent')
        with self.assertRaises(WireError) as ctx:
            self.service.reject_wire(wire_id=sent.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'not_rejectable')
        with self.assertRaises(WireError) as ctx:
            self.service.release_wire(wire_id=sent.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        with self.assertRaises(WireError) as ctx:
            self.service.waive_fee(wire_id=sent.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'invalid_status')
        with self.assertRaises(WireError) as ctx:
            self.service.override_ofac(wire_id=sent.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'invalid_status')

        recalled = self.service.recall_wire(
            wire_id=sent.wire_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(wire_id=sent.wire_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_recalled')
        self.assertEqual(len(self.credits), 2)

    def test_queued_cannot_be_completed_or_recalled(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        queued, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='33.00', trace_id='q-term',
        )
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.debits, [])
        with self.assertRaises(WireError) as ctx:
            self.service.complete_wire(wire_id=queued.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'not_completable')
        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(wire_id=queued.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'not_recallable')
        stored = self.service.store.get_wire(queued.wire_id)
        self.assertEqual(stored.status, 'queued')
        self.assertEqual(self.debits, [])
        self.assertEqual(self.credits, [])

    def test_beneficiary_aliases_and_already_active(self):
        bene = self._add()
        held = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='hold',
        )
        self.assertEqual(held.status, 'paused')
        resumed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='unpause',
        )
        self.assertEqual(resumed.status, 'active')
        with self.assertRaises(WireError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='activate',
            )
        self.assertEqual(ctx.exception.code, 'already_active')
        closed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='close',
        )
        self.assertEqual(closed.status, 'archived')

    def test_customer_manage_false_blocks_add_staff_can_still_add(self):
        locked = WireService(
            WirePolicy(customer_manage=False),
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
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        staff = locked.add_beneficiary(
            owner_userid='alice', actor='teller', actor_type='tier1',
            nickname='Chase', legal_name='Ada Lovelace', aba='021000021',
            account_number='77881234', street='1 Federal St', city='New York',
            state='NY', postal='10004', default_account='1001',
        )
        self.assertEqual(staff.userid, 'alice')

    def test_allow_credit_lets_credit_account_originate(self):
        self.service.policy.allow_credit = True
        bene = self._add(default_account='1003')
        wire, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00',
            internal_account='1003', trace_id='credit-ok',
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(self.debits[0][0], '1003')
        self.assertEqual(self.debits[0][1], '40.00')

    def test_max_wires_blocks_new_origination(self):
        self.service.policy.max_wires = 1
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='limit-1',
        )
        self.assertTrue(created)
        self.assertEqual(first.status, 'sent')
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='15.00', trace_id='limit-2',
            )
        self.assertEqual(ctx.exception.code, 'wire_limit')
        self.assertEqual(len(self.service.store.list_wires('alice')), 1)


if __name__ == '__main__':
    unittest.main()
