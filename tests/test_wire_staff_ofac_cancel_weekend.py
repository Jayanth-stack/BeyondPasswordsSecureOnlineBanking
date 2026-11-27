"""Staff cancel of an OFAC hold, and Saturday queue-until-Monday.

Unique vs still-open PRs #214/#213/#211: those locked customer cancel of an
OFAC hold, customer cancel of pending_release, staff reject of OFAC (no
debit), and Friday-after-Thanksgiving as a business day. They never locked
staff unwind of a held wire (cancel vs reject), that a stranger cannot
cancel another customer's hold, or that Saturday morning queues (snapshot
after_cutoff includes weekends) until Monday run_due.
"""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireStaffOfacCancelWeekendTests(unittest.TestCase):
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

    def test_staff_cancel_of_ofac_hold_blocks_later_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', trace_id='ofac-staff-cancel',
        )
        self.assertEqual(wire.status, 'held')
        self.assertEqual(self.debits, [])

        with self.assertRaises(WireError) as ctx:
            self.service.cancel_wire(
                wire_id=wire.wire_id, actor='bob', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'wire_forbidden')
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, 'held')

        cancelled = self.service.cancel_wire(
            wire_id=wire.wire_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.assertEqual(self.debits, [])

        with self.assertRaises(WireError) as ctx:
            self.service.override_ofac(
                wire_id=wire.wire_id, actor='teller', actor_type='tier1', note='cleared',
            )
        self.assertEqual(ctx.exception.code, 'invalid_status')
        self.assertEqual(self.debits, [])

        with self.assertRaises(WireError) as ctx:
            self.service.reject_wire(
                wire_id=wire.wire_id, actor='teller', actor_type='tier1', reason='ofac',
            )
        self.assertEqual(ctx.exception.code, 'not_rejectable')

    def test_saturday_morning_queues_until_monday_run_due(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        saturday = ts(2024, 6, 15, 10, 0)
        self.assertEqual(calendar.value_date(saturday).isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(saturday)['after_cutoff'])
        monday = ts(2024, 6, 17, 10, 0)
        self.assertEqual(calendar.value_date(monday).isoformat(), '2024-06-17')
        self.assertFalse(calendar.snapshot(monday)['after_cutoff'])

        self.now[0] = saturday
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='sat-queue',
        )
        self.assertEqual(wire.status, 'queued')
        self.assertEqual(wire.value_date, '20240617')
        self.assertEqual(self.debits, [])

        self.now[0] = monday
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0], ('1001', '40.00', 'wire to Chase Checking'))


if __name__ == '__main__':
    unittest.main()
