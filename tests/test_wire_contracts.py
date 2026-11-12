"""Integrity contracts for domestic Fedwire that happy-path service tests miss."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    _classify_money_result,
    compose_imad,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class ClassifyMoneyResultTests(unittest.TestCase):
    def test_debit_strings_ok_and_receipt_dict_failed(self):
        self.assertEqual(_classify_money_result("Amount Debited"), "ok")
        self.assertEqual(_classify_money_result("done"), "ok")
        self.assertEqual(_classify_money_result("Insufficient Balance"), "nsf")
        self.assertEqual(
            _classify_money_result({"amount": "40.00", "signature": "deadbeef"}),
            "failed",
        )


class WireIntegrityTests(unittest.TestCase):
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

        self.service = WireService(
            WirePolicy(
                outbound_fee=Decimal("25.00"),
                dual_control_threshold=Decimal("10000.00"),
            ),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                "checkin": {"Account": 1001, "Balance": 5000},
                "savings": {"Account": 1002, "Balance": 80},
                "credit": {"Account": 1003, "Balance": -20},
            },
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
            **kwargs,
        )

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice = self._add()
        first, created = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=alice.beneficiary_id,
            amount="40.00",
            trace_id="shared",
        )
        self.assertTrue(created)
        bob = self._add(
            owner="bob",
            nickname="BoA",
            account_number="99887766",
        )
        before = list(self.debits)
        again, created_again = self.service.originate(
            owner_userid="bob",
            actor="bob",
            actor_type="customer",
            beneficiary_id=bob.beneficiary_id,
            amount="40.00",
            trace_id="shared",
        )
        self.assertFalse(created_again)
        self.assertEqual(again.wire_id, first.wire_id)
        self.assertEqual(again.userid, "alice")
        self.assertEqual(self.debits, before)

    def test_customer_cannot_originate_against_foreign_beneficiary(self):
        alice = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="bob",
                actor_type="customer",
                beneficiary_id=alice.beneficiary_id,
                amount="40.00",
            )
        self.assertEqual(ctx.exception.code, "wire_forbidden")
        self.assertEqual(self.debits, [])

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        def receipt_debit(account, amount, remark):
            self.debits.append((account, amount, remark))
            return {"amount": amount, "signature": "deadbeef"}

        self.service.debit_fn = receipt_debit
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="alice",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="40.00",
                trace_id="hmac-1",
            )
        self.assertEqual(ctx.exception.code, "failed")
        self.assertEqual(ctx.exception.extra["wire"].status, "failed")
        self.assertEqual(ctx.exception.extra["wire"].imad, "")

    def test_amount_bounds_and_fee_nsf_still_sends_principal(self):
        bene = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="alice",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="9.99",
            )
        self.assertEqual(ctx.exception.code, "amount_out_of_range")
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="alice",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="1000000.01",
            )
        self.assertEqual(ctx.exception.code, "amount_out_of_range")

        calls = []

        def mixed_debit(account, amount, remark):
            calls.append((account, amount, remark))
            if "fee" in remark:
                return "Insufficient Balance"
            return "Amount Debited"

        self.service.debit_fn = mixed_debit
        wire, created = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="fee-nsf",
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, "sent")
        self.assertEqual(wire.fee_status, "nsf")
        self.assertTrue(wire.imad)
        self.assertEqual(calls[0][1], "40.00")
        self.assertEqual(calls[1][1], "25.00")

    def test_customer_waive_fee_is_ignored_preview_does_not_debit(self):
        bene = self._add()
        preview = self.service.preview(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            waive_fee=True,
        )
        self.assertEqual(preview["fee"], "25.00")
        self.assertEqual(self.debits, [])
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            waive_fee=True,
            trace_id="no-waive",
        )
        self.assertEqual(wire.fee, "25.00")
        self.assertEqual(wire.fee_status, "collected")

    def test_same_day_imad_sequences_are_unique(self):
        bene = self._add()
        first, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="imad-1",
        )
        second, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="41.00",
            trace_id="imad-2",
        )
        self.assertEqual(first.imad, compose_imad("20240614", "KONOHA01", 1))
        self.assertEqual(second.imad, compose_imad("20240614", "KONOHA01", 2))
        self.assertNotEqual(first.imad, second.imad)

    def test_thanksgiving_and_saturday_july4_shift_value_date(self):
        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        thanksgiving = ts(2024, 11, 28, 11, 0)
        self.assertIn(datetime(2024, 11, 28).date(), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(thanksgiving).isoformat(), "2024-11-29")
        # 2026-07-04 is Saturday; observed Friday 2026-07-03, next business day Monday.
        saturday_july4 = ts(2026, 7, 4, 11, 0)
        self.assertEqual(calendar.value_date(saturday_july4).isoformat(), "2026-07-06")

    def test_run_due_holds_queued_wire_if_dual_control_threshold_drops(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="50.00",
            trace_id="queued-dc",
        )
        self.assertEqual(wire.status, "queued")
        self.assertEqual(self.debits, [])
        self.service.policy.dual_control_threshold = Decimal("10.00")
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due("alice")
        self.assertEqual(due[0].status, "pending_release")
        self.assertEqual(self.debits, [])
        self.assertEqual(due[0].imad, "")


if __name__ == "__main__":
    unittest.main()
