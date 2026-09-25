"""High-risk Fedwire contracts missing from the original PR #73 tests.

Global trace replay, empty-account ownership bypass, HMAC-receipt
classification, fee-NSF still-sent, and dual-control cutoff release.
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


class WireIntegrityContractTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
        self.debits = []
        self.credits = []
        self.accounts = {
            "alice": {
                "checkin": {"Account": 1001, "Balance": 5000},
                "savings": {"Account": 1002, "Balance": 80},
                "credit": {"Account": 1003, "Balance": -20},
            },
            "bob": {
                "checkin": {"Account": 2001, "Balance": 200},
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
            default_account=kwargs.pop("default_account", "1001"),
            **kwargs,
        )

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice = self._add()
        bob = self._add(
            owner="bob",
            nickname="Ally",
            legal_name="Bob Jones",
            account_number="11223344",
            default_account="2001",
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
        principal = [row for row in self.debits if row[1] == "40.00"]
        self.assertEqual(len(principal), 1)
        self.assertEqual(principal[0][0], "1001")
        self.assertFalse(any(row[1] == "90.00" for row in self.debits))

    def test_empty_account_directory_skips_ownership_and_credit_checks(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default_account="9999")
        self.assertEqual(bene.default_account, "9999")
        credit = self._add(
            nickname="Visa",
            legal_name="Ada Lovelace",
            account_number="41110003",
            default_account="1003",
        )
        self.assertEqual(credit.default_account, "1003")

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
                trace_id="rcpt-1",
            )
        self.assertEqual(ctx.exception.code, "failed")
        stored = self.service.store.get_wire_by_trace("rcpt-1")
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.imad, "")

    def test_fee_nsf_still_marks_wire_sent_with_imad(self):
        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            if "fee" in (remark or ""):
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
        self.assertTrue(wire.imad)
        self.assertEqual(wire.fee_status, "nsf")

    def test_pending_release_still_transmits_after_cutoff(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="maker",
            actor_type="tier1",
            beneficiary_id=bene.beneficiary_id,
            amount="10000.00",
            trace_id="hv-late",
        )
        self.assertEqual(wire.status, "pending_release")
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 18, 0)
        released = self.service.release_wire(
            wire_id=wire.wire_id, actor="checker", actor_type="tier2"
        )
        self.assertEqual(released.status, "sent")
        self.assertTrue(released.imad)
        self.assertEqual(self.debits[0][1], "10000.00")

    def test_queued_release_after_cutoff_requeues_instead_of_sending(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="late-q",
        )
        self.assertEqual(wire.status, "queued")
        released = self.service.release_wire(
            wire_id=wire.wire_id, actor="teller", actor_type="tier2"
        )
        self.assertEqual(released.status, "queued")
        self.assertEqual(released.imad, "")
        self.assertEqual(self.debits, [])


if __name__ == "__main__":
    unittest.main()
