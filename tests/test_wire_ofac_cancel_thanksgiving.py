"""Customer cancel of an OFAC hold, and Thanksgiving queue-until-Friday.

Unique vs still-open PRs #213/#211: those locked customer cancel of
pending_release, staff reject of OFAC (no debit), exact 17:00 cutoff, weekday
Christmas value date, and Friday-after-Thanksgiving 2024 as a business day.
They never locked customer unwind of a held wire, or that Thanksgiving morning
queues (snapshot after_cutoff includes non-business days) until Friday run_due.
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


class WireOfacCancelThanksgivingTests(unittest.TestCase):
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

    def test_customer_cancel_of_ofac_hold_blocks_later_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', trace_id='ofac-cancel',
        )
        self.assertEqual(wire.status, 'held')
        self.assertEqual(self.debits, [])

        cancelled = self.service.cancel_wire(
            wire_id=wire.wire_id, actor='alice', actor_type='customer',
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
            self.service.release_wire(wire_id=wire.wire_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'not_releasable')

    def test_thanksgiving_2024_queues_until_friday_run_due(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        thanksgiving = ts(2024, 11, 28, 10, 0)
        self.assertEqual(calendar.value_date(thanksgiving).isoformat(), '2024-11-29')
        self.assertTrue(calendar.snapshot(thanksgiving)['after_cutoff'])
        friday_after = ts(2024, 11, 29, 10, 0)
        self.assertEqual(calendar.value_date(friday_after).isoformat(), '2024-11-29')
        self.assertFalse(calendar.snapshot(friday_after)['after_cutoff'])

        self.now[0] = thanksgiving
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='thanks',
        )
        self.assertEqual(wire.status, 'queued')
        self.assertEqual(wire.value_date, '20241129')
        self.assertEqual(self.debits, [])

        self.now[0] = friday_after
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0], ('1001', '40.00', 'wire to Chase Checking'))


if __name__ == '__main__':
    unittest.main()
