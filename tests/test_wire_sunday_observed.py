"""Sunday-observed New Year / Juneteenth and customer originate-for-another.

Unique vs still-open coverage PRs #207/#205/#203/#201/#199/#197/#195/#193/#191/#189:
those lock Saturday→Friday observation, weekday MLK/Memorial/New Year,
Sunday-observed Christmas/July4, Labor/Columbus/Presidents, and
Juneteenth/Thanksgiving/Veterans weekdays. They do not lock Sunday New Year
2023 or Sunday Juneteenth 2022 (observed Monday, value date skips to Tuesday),
or a customer originating against another customer's beneficiary.
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


class WireSundayObservedTests(unittest.TestCase):
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
            "bob": {
                "checkin": {"Account": 2001, "Balance": 800},
                "savings": {"Account": 2002, "Balance": 10},
                "credit": "None",
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

    def test_sunday_new_year_and_juneteenth_observed_monday(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2023 = us_fed_holidays(2023)
        holidays_2022 = us_fed_holidays(2022)
        self.assertIn(date(2023, 1, 2), holidays_2023)
        self.assertNotIn(date(2023, 1, 1), holidays_2023)
        self.assertIn(date(2022, 6, 20), holidays_2022)
        self.assertNotIn(date(2022, 6, 19), holidays_2022)
        self.assertEqual(calendar.value_date(ts(2023, 1, 1, 11, 0)).isoformat(), "2023-01-03")
        self.assertEqual(calendar.value_date(ts(2022, 6, 19, 11, 0)).isoformat(), "2022-06-21")

    def test_customer_cannot_originate_for_another_customer(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="bob",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="40.00",
                trace_id="cross-customer",
            )
        self.assertEqual(ctx.exception.code, "wire_forbidden")
        self.assertEqual(self.debits, [])
        stored = self.service.store.list_wires("alice")
        self.assertEqual(stored, [])


if __name__ == "__main__":
    unittest.main()
