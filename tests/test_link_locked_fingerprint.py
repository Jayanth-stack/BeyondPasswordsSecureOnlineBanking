"""Locked/expired occupy the fingerprint; reject-prenote frees it.

Unique vs still-open coverage PRs #211/#209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock close/archive of a still-pending micro (terminal, fingerprint
freed), pause/resume/hold of pending as link_not_verified, and force-verify of
locked/expired/closed. They do not lock that locked and expired stay in
OPEN_STATUSES so the same routing+account last4 cannot be re-linked until
close/cancel, or that staff reject_prenote of a pending prenote is rejected
(not open) so the same last4 can be re-linked without close.
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


class LinkLockedFingerprintTests(unittest.TestCase):
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
        for _ in range(self.service.policy.max_attempts):
            with self.assertRaises(LinkError):
                self.service.confirm_micro(
                    link_id=link.link_id,
                    actor="alice",
                    actor_type="customer",
                    amount1="0.01",
                    amount2="0.02",
                )
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "locked")
        return stored

    def test_locked_occupies_fingerprint_until_customer_closes(self):
        link = self._add()
        self._lock(link)
        with self.assertRaises(LinkError) as ctx:
            self._add(nickname="Chase Relink")
        self.assertEqual(ctx.exception.code, "link_duplicate")
        closed = self.service.set_link_status(
            link_id=link.link_id, actor="alice", actor_type="customer", status="close",
        )
        self.assertEqual(closed.status, "closed")
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "already_closed")
        again = self._add(nickname="Chase Relink")
        self.assertEqual(again.status, "pending")
        self.assertNotEqual(again.link_id, link.link_id)
        self.assertEqual(self.debits, [])

    def test_cancel_alias_of_expired_also_closes_and_frees_fingerprint(self):
        link = self._add(
            nickname="Old Micro", routing_last4="1210", account_last4="9001",
        )
        self.now[0] += 86400 + 1
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id,
                actor="alice",
                actor_type="customer",
                amount1="0.12",
                amount2="0.47",
            )
        self.assertEqual(ctx.exception.code, "link_expired")
        with self.assertRaises(LinkError) as ctx:
            self._add(
                nickname="Old Micro Relink", routing_last4="1210", account_last4="9001",
            )
        self.assertEqual(ctx.exception.code, "link_duplicate")
        cancelled = self.service.set_link_status(
            link_id=link.link_id, actor="alice", actor_type="customer", status="cancel",
        )
        self.assertEqual(cancelled.status, "closed")
        again = self._add(
            nickname="Old Micro Relink", routing_last4="1210", account_last4="9001",
        )
        self.assertEqual(again.status, "pending")
        self.assertNotEqual(again.link_id, link.link_id)

    def test_reject_prenote_frees_fingerprint_without_close(self):
        link = self._add(
            nickname="Prenote", routing_last4="0114", account_last4="3344", method="prenote",
        )
        rejected = self.service.reject_prenote(
            link_id=link.link_id, actor="teller", actor_type="tier1",
        )
        self.assertEqual(rejected.status, "rejected")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "rejected")
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(
                link_id=link.link_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "invalid_status")
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "already_closed")
        again = self._add(
            nickname="Prenote Relink", routing_last4="0114", account_last4="3344", method="prenote",
        )
        self.assertEqual(again.status, "pending")
        self.assertNotEqual(again.link_id, link.link_id)
        self.assertEqual(self.debits, [])


if __name__ == "__main__":
    unittest.main()
