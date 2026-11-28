"""Complete/recall of an OFAC hold fail, and Good Friday is a business day.

Unique vs still-open PRs #216/#214/#213/#211: those locked staff/customer
cancel of an OFAC hold, staff reject of OFAC, customer cancel of
pending_release, Saturday queue-until-Monday, and Friday-after-Thanksgiving
as a business day. They never locked that complete/recall of a still-held
wire are not_completable / not_recallable (no debit), or that Good Friday
2024-03-29 10:00 ET is a Fedwire business day so originate sends same-day.
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


class WireOfacCompleteGoodFridayTests(unittest.TestCase):
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

    def test_complete_and_recall_of_ofac_hold_do_not_debit(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', trace_id='ofac-complete',
        )
        self.assertEqual(wire.status, 'held')
        self.assertEqual(self.debits, [])

        with self.assertRaises(WireError) as ctx:
            self.service.complete_wire(
                wire_id=wire.wire_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'not_completable')

        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(
                wire_id=wire.wire_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'not_recallable')
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, 'held')
        self.assertEqual(self.debits, [])
        self.assertEqual(self.credits, [])

    def test_good_friday_morning_sends_same_day(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        good_friday = ts(2024, 3, 29, 10, 0)
        self.assertEqual(calendar.value_date(good_friday).isoformat(), '2024-03-29')
        self.assertFalse(calendar.snapshot(good_friday)['after_cutoff'])
        self.assertTrue(calendar.is_business_day(datetime(2024, 3, 29).date()))

        self.now[0] = good_friday
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', trace_id='gf-send',
        )
        self.assertEqual(wire.status, 'sent')
        self.assertEqual(wire.value_date, '20240329')
        self.assertEqual(self.debits[0], ('1001', '40.00', 'wire to Chase Checking'))


if __name__ == '__main__':
    unittest.main()
