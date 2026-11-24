"""Sunday Veterans / Saturday Juneteenth observation and staff cross-customer originate.

Unique vs still-open coverage PRs #209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189:
those lock Sunday New Year/Juneteenth and Christmas/July4, Saturday
Independence/Christmas/Veterans, weekday MLK/Memorial/New Year, Labor/
Columbus/Presidents, and customer originate-for-another. They do not lock
Sunday Veterans Day 2018 or Saturday Juneteenth 2021, that the Friday after
Thanksgiving stays a business day, or staff originating against another
customer's beneficiary (the ownership check after the role gate).
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


class WireVeteransJuneteenthTests(unittest.TestCase):
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

    def test_sunday_veterans_and_saturday_juneteenth_observed(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2018 = us_fed_holidays(2018)
        holidays_2021 = us_fed_holidays(2021)
        self.assertIn(date(2018, 11, 12), holidays_2018)
        self.assertNotIn(date(2018, 11, 11), holidays_2018)
        self.assertIn(date(2021, 6, 18), holidays_2021)
        self.assertNotIn(date(2021, 6, 19), holidays_2021)
        self.assertEqual(calendar.value_date(ts(2018, 11, 11, 11, 0)).isoformat(), "2018-11-13")
        self.assertEqual(calendar.value_date(ts(2021, 6, 18, 11, 0)).isoformat(), "2021-06-21")

    def test_friday_after_thanksgiving_is_still_a_business_day(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2024 = us_fed_holidays(2024)
        self.assertIn(date(2024, 11, 28), holidays_2024)
        self.assertNotIn(date(2024, 11, 29), holidays_2024)
        self.assertTrue(calendar.is_business_day(date(2024, 11, 29)))
        self.assertEqual(calendar.value_date(ts(2024, 11, 29, 11, 0)).isoformat(), "2024-11-29")

    def test_staff_cannot_originate_against_another_customers_beneficiary(self):
        bob_bene = self._add(
            owner="bob",
            nickname="BoA Checking",
            aba="011401533",
            account_number="99887766",
            default_account="2001",
        )
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="teller",
                actor_type="tier1",
                beneficiary_id=bob_bene.beneficiary_id,
                amount="40.00",
                trace_id="staff-cross-customer",
            )
        self.assertEqual(ctx.exception.code, "wire_forbidden")
        self.assertEqual(self.debits, [])
        stored = self.service.store.list_wires("alice")
        self.assertEqual(stored, [])


if __name__ == "__main__":
    unittest.main()
