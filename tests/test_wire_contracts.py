"""High-risk Fedwire contracts missing from the original PR #73 tests.

Global trace replay, HMAC-receipt classification, foreign-beneficiary IDOR,
amount bounds, fee-NSF still sending principal, staff-only fee waiver,
preview without debit, and observed Fed holidays.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.crypto_receipt import generate_receipt
from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
    _classify_money_result,
    us_fed_holidays,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class WireHelperContractTests(unittest.TestCase):
    def test_classify_treats_debit_strings_ok_and_receipt_dict_as_failed(self):
        self.assertEqual(_classify_money_result("Amount Debited"), "ok")
        self.assertEqual(_classify_money_result("Success"), "ok")
        self.assertEqual(_classify_money_result("Insufficient Balance"), "nsf")
        receipt = generate_receipt(
            {"from_account": 10, "to_account": 20, "amount": 40.0, "status": "done"}
        )
        self.assertIsInstance(receipt, dict)
        self.assertIn("signature", receipt)
        self.assertEqual(_classify_money_result(receipt), "failed")

    def test_thanksgiving_and_saturday_july4_are_observed(self):
        holidays_2024 = us_fed_holidays(2024)
        self.assertIn(datetime(2024, 11, 28).date(), holidays_2024)
        holidays_2026 = us_fed_holidays(2026)
        # 2026-07-04 is Saturday; Fed observes Friday 2026-07-03.
        self.assertIn(datetime(2026, 7, 3).date(), holidays_2026)
        self.assertNotIn(datetime(2026, 7, 4).date(), holidays_2026)

        calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        thanksgiving = ts(2024, 11, 28, 10, 0)
        self.assertEqual(calendar.value_date(thanksgiving).isoformat(), "2024-11-29")
        observed_july4 = ts(2026, 7, 3, 10, 0)
        self.assertEqual(calendar.value_date(observed_july4).isoformat(), "2026-07-06")


class WireIntegrityContractTests(unittest.TestCase):
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
                "checkin": {"Account": 2001, "Balance": 2000},
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

        policy = WirePolicy(
            outbound_fee=Decimal("25.00"),
            dual_control_threshold=Decimal("10000.00"),
            min_amount=Decimal("10.00"),
            max_amount=Decimal("1000.00"),
        )
        self.service = WireService(
            policy,
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
            default_account=kwargs.pop("default", "1001" if owner == "alice" else "2001"),
            **kwargs,
        )

    def test_customer_cannot_originate_someone_elses_beneficiary(self):
        alice = self._add()
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="bob",
                actor="bob",
                actor_type="customer",
                beneficiary_id=alice.beneficiary_id,
                amount="40.00",
            )
        self.assertEqual(ctx.exception.code, "wire_forbidden")
        self.assertEqual(self.debits, [])

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice = self._add()
        bob = self._add(
            owner="bob",
            nickname="Ally",
            account_number="11223344",
            default="2001",
        )
        first, created = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=alice.beneficiary_id,
            amount="40.00",
            trace_id="wire-shared",
        )
        self.assertTrue(created)
        replay, created_again = self.service.originate(
            owner_userid="bob",
            actor="bob",
            actor_type="customer",
            beneficiary_id=bob.beneficiary_id,
            amount="90.00",
            trace_id="wire-shared",
        )
        self.assertFalse(created_again)
        self.assertEqual(replay.wire_id, first.wire_id)
        self.assertEqual(replay.userid, "alice")
        self.assertEqual(len([row for row in self.debits if row[1] == "40.00"]), 1)
        self.assertEqual(self.debits[0][0], "1001")

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        bene = self._add()
        self.service.debit_fn = lambda account, amount, remark: generate_receipt(
            {"from_account": account, "amount": amount, "status": "done"}
        )
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="alice",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="40.00",
                trace_id="rcpt-w1",
            )
        self.assertEqual(ctx.exception.code, "failed")
        stored = self.service.store.get_wire_by_trace("rcpt-w1")
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.imad, "")

    def test_amount_bounds_reject_below_min_and_above_max(self):
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
                amount="1000.01",
            )
        self.assertEqual(ctx.exception.code, "amount_out_of_range")
        self.assertEqual(self.debits, [])

    def test_fee_nsf_still_sends_principal(self):
        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            if "wire fee" in str(remark):
                return "Insufficient Balance"
            return "Amount Debited"

        self.service.debit_fn = debit_fn
        bene = self._add()
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
        self.assertEqual(self.debits[0][1], "40.00")
        self.assertEqual(self.debits[1][1], "25.00")

    def test_preview_does_not_debit_and_customer_waive_is_ignored(self):
        bene = self._add()
        preview = self.service.preview(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            waive_fee=True,
        )
        self.assertEqual(preview["amount"], "40.00")
        self.assertEqual(preview["fee"], "25.00")
        self.assertEqual(preview["total"], "65.00")
        self.assertNotIn("account_number", preview["beneficiary"])
        self.assertEqual(self.debits, [])

        staff_preview = self.service.preview(
            owner_userid="alice",
            actor="teller",
            actor_type="tier1",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            waive_fee=True,
        )
        self.assertEqual(staff_preview["fee"], "0.00")
        self.assertEqual(staff_preview["total"], "40.00")
        self.assertEqual(self.debits, [])

    def test_waive_fee_is_staff_only_and_blocked_after_send(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        queued, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="waive-1",
        )
        self.assertEqual(queued.status, "queued")
        self.assertEqual(queued.fee, "25.00")
        with self.assertRaises(WireError) as ctx:
            self.service.waive_fee(
                wire_id=queued.wire_id, actor="alice", actor_type="customer"
            )
        self.assertEqual(ctx.exception.code, "wire_forbidden")
        waived = self.service.waive_fee(
            wire_id=queued.wire_id, actor="teller", actor_type="tier1"
        )
        self.assertEqual(waived.fee, "0.00")
        self.assertEqual(waived.fee_status, "waived")

        self.now[0] = ts(2024, 6, 14, 11, 0)
        sent, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="15.00",
            trace_id="waive-sent",
        )
        self.assertEqual(sent.status, "sent")
        with self.assertRaises(WireError) as ctx:
            self.service.waive_fee(
                wire_id=sent.wire_id, actor="teller", actor_type="tier1"
            )
        self.assertEqual(ctx.exception.code, "invalid_status")

    def test_same_day_imads_are_unique(self):
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
        self.assertNotEqual(first.imad, second.imad)
        self.assertTrue(first.imad.endswith("000001"))
        self.assertTrue(second.imad.endswith("000002"))

    def test_hmac_receipt_from_credit_fails_recall_without_marking_recalled(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="recall-rcpt",
        )
        self.service.credit_fn = lambda account, amount, remark: generate_receipt(
            {"to_account": account, "amount": amount, "status": "done"}
        )
        with self.assertRaises(WireError) as ctx:
            self.service.recall_wire(
                wire_id=wire.wire_id, actor="teller", actor_type="tier2"
            )
        self.assertEqual(ctx.exception.code, "recall_failed")
        stored = self.service.store.get_wire(wire.wire_id)
        self.assertEqual(stored.status, "sent")


if __name__ == "__main__":
    unittest.main()
