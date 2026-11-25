"""Customer cancel of dual-control, OFAC reject, exact cutoff, weekday Christmas.

Unique vs still-open coverage PRs #211/#209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189:
those lock cancel of queued / not_cancelable of sent, reject of sent, Sunday
Veterans / Saturday Juneteenth / Friday-after-Thanksgiving, and staff
originate against another customer's beneficiary. They do not lock customer
cancel of pending_release (dual-control still cancelable, no debit), staff
reject of an OFAC hold (no debit, override then invalid_status), cutoff at
exactly 17:00 ET queuing, or weekday Christmas 2024 shifting value date.
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


class WirePendingReleaseCancelTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []
        self.accounts = {
            "alice": {
                "checkin": {"Account": 1001, "Balance": 5000},
                "savings": {"Account": 1002, "Balance": 80},
                "credit": {"Account": 1003, "Balance": -20},
            },
        }

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return "Amount Debited"

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return "Success"

        self.service = WireService(
            WirePolicy(outbound_fee=Decimal("25.00"), dual_control_threshold=Decimal("10000.00")),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: self.accounts.get(userid, {}),
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, owner="alice", actor=None, actor_type="customer", **kwargs):
        return self.service.add_beneficiary(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop("nickname", "Chase Checking"),
            legal_name=kwargs.pop("legal_name", "Ada Lovelace"),
            aba=kwargs.pop("aba", "021000021"),
            account_number=kwargs.pop("account_number", "77881234"),
            street=kwargs.pop("street", "1 Federal St"),
            city=kwargs.pop("city", "New York"),
            state=kwargs.pop("state", "NY"),
            postal=kwargs.pop("postal", "10004"),
            default_account=kwargs.pop("default_account", "1001"),
        )

    def test_customer_can_cancel_pending_release_without_debit(self):
        bene = self._add()
        wire, created = self.service.originate(
            owner_userid="alice",
            actor="maker",
            actor_type="tier1",
            beneficiary_id=bene.beneficiary_id,
            amount="10000.00",
            trace_id="dc-cancel",
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, "pending_release")
        self.assertEqual(self.debits, [])
        cancelled = self.service.cancel_wire(
            wire_id=wire.wire_id, actor="alice", actor_type="customer",
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.debits, [])
        with self.assertRaises(WireError) as ctx:
            self.service.release_wire(
                wire_id=wire.wire_id, actor="checker", actor_type="tier2",
            )
        self.assertEqual(ctx.exception.code, "not_releasable")
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, "cancelled")
        self.assertEqual(stored.imad, "")

    def test_staff_reject_of_ofac_hold_does_not_debit(self):
        bene = self._add(nickname="Blocked", legal_name="Blocked Person")
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="80.00",
            trace_id="ofac-reject",
        )
        self.assertEqual(wire.status, "held")
        self.assertTrue(wire.ofac_hit)
        self.assertEqual(self.debits, [])
        rejected = self.service.reject_wire(
            wire_id=wire.wire_id, actor="teller", actor_type="tier1", reason="ofac",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.debits, [])
        with self.assertRaises(WireError) as ctx:
            self.service.override_ofac(
                wire_id=wire.wire_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "invalid_status")
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, "rejected")
        self.assertEqual(stored.imad, "")

    def test_exact_cutoff_queues_and_does_not_debit(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.assertTrue(calendar.is_after_cutoff(self.now[0]))
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="exact-cutoff",
        )
        self.assertEqual(wire.status, "queued")
        self.assertEqual(wire.value_date, "20240617")
        self.assertEqual(self.debits, [])
        self.assertEqual(wire.imad, "")

    def test_weekday_christmas_2024_shifts_value_date(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2024 = us_fed_holidays(2024)
        self.assertIn(date(2024, 12, 25), holidays_2024)
        self.assertEqual(calendar.value_date(ts(2024, 12, 25, 11, 0)).isoformat(), "2024-12-26")
        self.assertTrue(calendar.is_business_day(date(2024, 12, 26)))


if __name__ == "__main__":
    unittest.main()
