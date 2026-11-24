"""Close/archive of a still-pending micro is allowed and frees the fingerprint.

Unique vs still-open coverage PRs #209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock pause/resume/hold of pending as link_not_verified / invalid_status,
force-verify of locked/expired/prenote-wait/paused/rejected/verified/closed,
and hold/unpause/archive aliases on a *verified* link. They do not lock
close/archive/cancel while status is still `pending`, which succeeds (unlike
pause), is terminal, and drops the row from OPEN_STATUSES so the same
routing+account last4 can be re-linked.
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


class LinkClosePendingTests(unittest.TestCase):
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

    def test_customer_close_of_pending_is_terminal_and_frees_fingerprint(self):
        link = self._add()
        closed = self.service.set_link_status(
            link_id=link.link_id, actor="alice", actor_type="customer", status="close",
        )
        self.assertEqual(closed.status, "closed")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "closed")
        with self.assertRaises(LinkError) as ctx:
            self.service.confirm_micro(
                link_id=link.link_id,
                actor="alice",
                actor_type="customer",
                amount1="0.12",
                amount2="0.47",
            )
        self.assertEqual(ctx.exception.code, "invalid_status")
        with self.assertRaises(LinkError) as ctx:
            self.service.force_verify(
                link_id=link.link_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "already_closed")
        again = self._add(nickname="Chase Relink")
        self.assertEqual(again.status, "pending")
        self.assertNotEqual(again.link_id, link.link_id)
        self.assertEqual(self.debits, [])

    def test_archive_alias_of_pending_also_closes(self):
        link = self._add(
            nickname="Prenote", routing_last4="1210", account_last4="9001", method="prenote",
        )
        archived = self.service.set_link_status(
            link_id=link.link_id, actor="teller", actor_type="tier1", status="archive",
        )
        self.assertEqual(archived.status, "closed")
        stored = self.service.store.get_link(link.link_id)
        self.assertEqual(stored.status, "closed")
        with self.assertRaises(LinkError) as ctx:
            self.service.accept_prenote(
                link_id=link.link_id, actor="teller", actor_type="tier1",
            )
        self.assertEqual(ctx.exception.code, "invalid_status")


if __name__ == "__main__":
    unittest.main()
