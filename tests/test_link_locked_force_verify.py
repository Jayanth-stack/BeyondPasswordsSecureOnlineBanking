"""Staff force-verify of a locked micro still bypasses the attempt lock.

Unique vs still-open coverage PRs #205/#203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock force-verify of expired/prenote-wait/paused/rejected/verified/closed
and staff resend-reset of a locked challenge back to pending. They do not lock
force_verify while status is still `locked`, which skips the reset and
immediately enables ACH push.
"""
from decimal import Decimal
import unittest

import tests  # noqa: F401

from utility.link import (
    LinkError,
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
)


class LinkLockedForceVerifyTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.debits = []
        self.credits = []
        self.amounts = [(Decimal("0.12"), Decimal("0.47"))]
        self.accounts = {
            "alice": {
                "checkin": {"Account": 1001, "Balance": 500},
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

        self.service = LinkService(
            LinkPolicy(
                challenge_secret="unit-secret",
                pending_ttl_seconds=86400,
                prenote_wait_seconds=86400,
                max_attempts=3,
                max_resends=1,
                max_links=8,
                min_amount=Decimal("1.00"),
                max_amount=Decimal("100.00"),
            ),
            MemoryLinkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: self.accounts.get(userid, {}),
            amount_fn=lambda: self.amounts[0],
        )

    def _add(self, owner="alice", actor=None, actor_type="customer", **kwargs):
        return self.service.add_link(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop("nickname", "Chase Checking"),
            default_account=kwargs.pop("default", "1001"),
            routing_last4=kwargs.pop("routing_last4", "0210"),
            account_last4=kwargs.pop("account_last4", "7788"),
            **kwargs,
        )

    def _lock(self, link):
        for _ in range(2):
            with self.assertRaises(LinkError) as ctx:
                self.service.confirm_micro(
                    link_id=link.link_id,
                    actor="alice",
                    actor_type="customer",
                    amount1="0.01",
                    amount2="0.02",
                )
            self.assertEqual(ctx.exception.code, "amounts_incorrect")
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id,
                actor="alice",
                actor_type="customer",
                amount1="0.01",
                amount2="0.02",
            )
        self.assertEqual(ctx.exception.code, "link_locked")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "locked")
        return stored

    def test_force_verify_of_locked_unlocks_and_allows_push(self):
        link = self._lock(self._add())
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id,
                actor="alice",
                actor_type="customer",
                amount1="0.12",
                amount2="0.47",
            )
        self.assertEqual(ctx.exception.code, "link_locked")
        forced = self.service.force_verify(
            link_id=link.link_id, actor="teller", actor_type="tier1",
        )
        self.assertEqual(forced.status, "verified")
        self.assertEqual(forced.attempts, 0)
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "verified")
        movement, created = self.service.move(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            link_id=link.link_id,
            amount="40.00",
            direction="push",
            trace_id="after-lock-bypass",
        )
        self.assertTrue(created)
        self.assertEqual(movement.status, "sent")
        self.assertEqual(self.debits[0][0], "1001")
        self.assertEqual(self.debits[0][1], "40.00")

    def test_customer_cannot_force_verify_locked(self):
        link = self._lock(self._add())
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor="alice", actor_type="customer",
            )
        self.assertEqual(ctx.exception.code, "link_forbidden")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "locked")
        self.assertEqual(self.debits, [])


if __name__ == "__main__":
    unittest.main()
