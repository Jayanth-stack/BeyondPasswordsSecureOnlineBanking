"""Unit tests for the payee allowlist capability."""

import os
import tempfile
import unittest

from utility.payee import (
    MemoryPayeeStore,
    PayeeError,
    PayeePolicy,
    PayeeService,
    SqlitePayeeStore,
    normalize_account,
    normalize_nickname,
    owned_account_numbers,
)


def _svc(cooling=0, max_payees=20, enabled=True, now=None):
    clock = {'t': 1_000_000.0}

    def _now():
        return clock['t'] if now is None else now()

    policy = PayeePolicy(
        enabled=enabled,
        cooling_seconds=cooling,
        max_payees=max_payees,
        customer_only=True,
        allow_own_accounts=True,
    )
    svc = PayeeService(store=MemoryPayeeStore(), policy=policy, now=_now if now is None else now)
    svc.clock = clock
    return svc


class NormalizeTests(unittest.TestCase):
    def test_account_accepts_int_and_padded_string(self):
        self.assertEqual(normalize_account(1001), '1001')
        self.assertEqual(normalize_account('01001'), '1001')
        self.assertEqual(normalize_account('1,001'), '1001')

    def test_account_rejects_junk(self):
        for bad in (None, True, '', 'abc', '0', -4, 1.5, '12.0'):
            with self.assertRaises(PayeeError) as ctx:
                normalize_account(bad)
            self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_nickname_normalizes_and_rejects(self):
        self.assertEqual(normalize_nickname('  Rent  Check  '), 'Rent Check')
        with self.assertRaises(PayeeError) as ctx:
            normalize_nickname('$$$')
        self.assertEqual(ctx.exception.code, 'invalid_nickname')
        with self.assertRaises(PayeeError):
            normalize_nickname('')


class RegistryTests(unittest.TestCase):
    def test_add_then_authorize(self):
        svc = _svc()
        added = svc.add('alice', 2001, 'Landlord')
        self.assertTrue(added.allowed)
        self.assertEqual(added.payee.account, '2001')
        decision = svc.authorize('alice', '2001', owned_accounts={'1001'})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, 'ok')

    def test_unregistered_destination_denied(self):
        svc = _svc()
        decision = svc.authorize('alice', 2001, owned_accounts={'1001'})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, 'payee_not_registered')

    def test_own_account_skips_registry(self):
        svc = _svc()
        decision = svc.authorize('alice', 1001, owned_accounts={'1001', '1002'})
        self.assertTrue(decision.allowed)

    def test_employee_skips_registry(self):
        svc = _svc()
        decision = svc.authorize('teller1', 9999, role='employee', owned_accounts=set())
        self.assertTrue(decision.allowed)

    def test_disabled_policy_skips(self):
        svc = _svc(enabled=False)
        self.assertTrue(svc.authorize('alice', 2001, owned_accounts=set()).allowed)

    def test_duplicate_account_and_nickname(self):
        svc = _svc()
        self.assertTrue(svc.add('alice', 2001, 'Rent').allowed)
        dup_acct = svc.add('alice', '2001', 'Other')
        self.assertFalse(dup_acct.allowed)
        self.assertEqual(dup_acct.code, 'payee_duplicate_account')
        dup_nick = svc.add('alice', 2002, 'rent')
        self.assertFalse(dup_nick.allowed)
        self.assertEqual(dup_nick.code, 'payee_duplicate_nickname')

    def test_owners_are_isolated(self):
        svc = _svc()
        svc.add('alice', 2001, 'Rent')
        denied = svc.authorize('bob', 2001, owned_accounts=set())
        self.assertEqual(denied.code, 'payee_not_registered')

    def test_max_payees(self):
        svc = _svc(max_payees=2)
        self.assertTrue(svc.add('alice', 2001, 'A').allowed)
        self.assertTrue(svc.add('alice', 2002, 'B').allowed)
        blocked = svc.add('alice', 2003, 'C')
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, 'payee_limit')

    def test_cooling_then_ready(self):
        svc = _svc(cooling=60)
        svc.add('alice', 2001, 'Rent')
        cooling = svc.authorize('alice', 2001, owned_accounts={'1001'})
        self.assertEqual(cooling.code, 'payee_cooling')
        self.assertEqual(cooling.retry_after, 60)
        svc.clock['t'] += 60
        ready = svc.authorize('alice', 2001, owned_accounts={'1001'})
        self.assertTrue(ready.allowed)

    def test_remove_then_deny_and_readd(self):
        svc = _svc()
        added = svc.add('alice', 2001, 'Rent')
        removed = svc.remove('alice', added.payee.payee_id)
        self.assertTrue(removed.allowed)
        self.assertEqual(svc.authorize('alice', 2001, owned_accounts=set()).code, 'payee_not_registered')
        again = svc.add('alice', 2001, 'Rent')
        self.assertTrue(again.allowed)
        self.assertNotEqual(again.payee.payee_id, added.payee.payee_id)

    def test_remove_wrong_owner(self):
        svc = _svc()
        added = svc.add('alice', 2001, 'Rent')
        missed = svc.remove('bob', added.payee.payee_id)
        self.assertEqual(missed.code, 'payee_not_found')
        self.assertTrue(svc.authorize('alice', 2001, owned_accounts=set()).allowed)

    def test_withdraw_operation_not_gated(self):
        svc = _svc()
        self.assertTrue(svc.authorize('alice', 2001, owned_accounts=set(), operation='withdraw').allowed)

    def test_owned_account_numbers_from_load_payload(self):
        accounts = {
            'savings': {'Account': 1001, 'Balance': 10},
            'checkin': 'None',
            'credit': {'Account': '01002', 'Balance': 0},
        }
        self.assertEqual(owned_account_numbers(accounts), {'1001', '1002'})

    def test_sqlite_survives_reopen(self):
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        try:
            first = PayeeService(store=SqlitePayeeStore(path), policy=PayeePolicy())
            first.add('alice', 2001, 'Rent')
            first.store.close()
            second = PayeeService(store=SqlitePayeeStore(path), policy=PayeePolicy())
            decision = second.authorize('alice', 2001, owned_accounts=set())
            self.assertTrue(decision.allowed)
            self.assertEqual(second.snapshot('alice')['count'], 1)
            second.store.close()
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
