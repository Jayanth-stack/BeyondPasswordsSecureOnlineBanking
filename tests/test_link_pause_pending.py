"""Pause/resume of a still-pending micro is rejected.

Unique vs still-open coverage PRs #207/#205/#203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock force-verify of locked/expired/prenote-wait/paused/rejected/verified/closed,
hold/unpause/archive aliases on a *verified* link, and already_paused of an
already-paused verified row. They do not lock pause/resume while status is
still `pending`, which must stay `link_not_verified` / `invalid_status`.
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


class LinkPausePendingTests(unittest.TestCase):
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

    def test_customer_cannot_pause_or_resume_pending(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor="alice", actor_type="customer", status="pause",
            )
        self.assertEqual(ctx.exception.code, "link_not_verified")
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor="alice", actor_type="customer", status="resume",
            )
        self.assertEqual(ctx.exception.code, "invalid_status")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(self.debits, [])

    def test_staff_cannot_pause_pending_either(self):
        link = self._add()
        with self.assertRaises(LinkError) as ctx:
            self.service.set_link_status(
                link_id=link.link_id, actor="teller", actor_type="tier1", status="hold",
            )
        self.assertEqual(ctx.exception.code, "link_not_verified")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "pending")


if __name__ == "__main__":
    unittest.main()
