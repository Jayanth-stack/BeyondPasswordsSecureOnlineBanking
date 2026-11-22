"""Saturday-observed Fed holidays and beneficiary fingerprint duplicates.

Unique vs still-open coverage PRs #205/#203/#201/#199/#197/#195/#193/#191/#189:
those lock weekday MLK/Memorial/New Year, Sunday-observed Christmas/July4
(Monday), Labor/Columbus/Presidents, Juneteenth/Thanksgiving/Veterans, and
max_beneficiaries. They do not lock Saturday → Friday observation or a
second nickname that reuses the same ABA + account.
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


class WireSaturdayObservedTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return "Amount Debited"

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return "Success"

        def accounts_fn(_userid):
            return {
                "checkin": {"Account": 1001, "Balance": 5000},
                "savings": {"Account": 1002, "Balance": 80},
                "credit": {"Account": 1003, "Balance": -20},
            }

        self.service = WireService(
            WirePolicy(outbound_fee=Decimal("25.00"), dual_control_threshold=Decimal("10000.00")),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
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

    def test_saturday_independence_christmas_veterans_observed_friday(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        holidays_2020 = us_fed_holidays(2020)
        holidays_2021 = us_fed_holidays(2021)
        holidays_2023 = us_fed_holidays(2023)
        self.assertIn(date(2020, 7, 3), holidays_2020)
        self.assertNotIn(date(2020, 7, 4), holidays_2020)
        self.assertIn(date(2021, 12, 24), holidays_2021)
        self.assertNotIn(date(2021, 12, 25), holidays_2021)
        self.assertIn(date(2023, 11, 10), holidays_2023)
        self.assertNotIn(date(2023, 11, 11), holidays_2023)
        self.assertEqual(calendar.value_date(ts(2020, 7, 3, 11, 0)).isoformat(), "2020-07-06")
        self.assertEqual(calendar.value_date(ts(2021, 12, 24, 11, 0)).isoformat(), "2021-12-27")
        self.assertEqual(calendar.value_date(ts(2023, 11, 10, 11, 0)).isoformat(), "2023-11-13")

    def test_duplicate_fingerprint_is_rejected_even_with_new_nickname(self):
        self._add()
        with self.assertRaises(WireError) as ctx:
            self._add(nickname="Chase Payroll")
        self.assertEqual(ctx.exception.code, "beneficiary_duplicate")
        other = self._add(
            nickname="BoA Checking",
            aba="011401533",
            account_number="99887766",
        )
        self.assertEqual(other.nickname, "BoA Checking")
        self.assertEqual(self.debits, [])


if __name__ == "__main__":
    unittest.main()
