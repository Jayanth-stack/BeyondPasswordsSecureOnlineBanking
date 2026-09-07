import os
import tempfile
import unittest

from utility.freeze import (
    AccountError,
    ChequeError,
    FreezeError,
    FreezePolicy,
    FreezeService,
    MemoryFreezeStore,
    SqliteFreezeStore,
    enforce_freeze,
    enforce_stop,
    normalize_account,
    normalize_cheque,
    own_accounts_from_customer_payload,
)


class FreezeCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.service = FreezeService(
            FreezePolicy(),
            MemoryFreezeStore(),
            clock=lambda: self.now[0],
        )

    def freeze(self, account='1001', **kwargs):
        defaults = dict(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            account=account,
            own_accounts=['1001', '1002'],
        )
        defaults.update(kwargs)
        return self.service.freeze_account(**defaults)

    def test_normalize_account_strips_float_suffix(self):
        self.assertEqual(normalize_account('1001.0'), '1001')
        with self.assertRaises(AccountError):
            normalize_account('abc')

    def test_normalize_cheque_rejects_blank(self):
        with self.assertRaises(ChequeError):
            normalize_cheque('')
        self.assertEqual(normalize_cheque('42.0'), '42')

    def test_customer_freeze_blocks_outbound_not_deposit(self):
        freeze = self.freeze()
        self.assertEqual(freeze.status, 'active')
        blocked = enforce_freeze(self.service, operation='transfer', account='1001', userid='alice')
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked[1], 403)
        self.assertEqual(blocked[0]['error'], 'account_frozen')
        self.assertIsNone(enforce_freeze(self.service, operation='deposit', account='1001', userid='alice'))
        self.assertIsNone(enforce_freeze(self.service, operation='transfer', account='1002', userid='alice'))

    def test_withdraw_and_cheque_and_request_blocked(self):
        self.freeze()
        for operation in ('withdraw', 'cheque', 'request', 'approve'):
            blocked = enforce_freeze(self.service, operation=operation, account='1001', userid='alice')
            self.assertEqual(blocked[0]['error'], 'account_frozen', operation)

    def test_other_account_unaffected(self):
        self.freeze('1001')
        self.assertIsNone(enforce_freeze(self.service, operation='transfer', account='1002', userid='alice'))

    def test_customer_cannot_freeze_unowned_account(self):
        with self.assertRaises(FreezeError) as ctx:
            self.freeze(account='9999')
        self.assertEqual(ctx.exception.code, 'freeze_forbidden')

    def test_customer_cannot_apply_fraud_reason(self):
        with self.assertRaises(FreezeError) as ctx:
            self.freeze(reason='fraud')
        self.assertEqual(ctx.exception.code, 'freeze_forbidden')

    def test_employee_can_apply_fraud_and_customer_cannot_unfreeze(self):
        freeze = self.service.freeze_account(
            owner_userid='alice',
            actor='emp1',
            actor_type='tier2',
            account='1001',
            reason='fraud',
        )
        with self.assertRaises(FreezeError) as ctx:
            self.service.unfreeze(
                freeze_id=freeze.freeze_id,
                actor='alice',
                actor_type='customer',
                owner_userid='alice',
            )
        self.assertEqual(ctx.exception.code, 'freeze_locked')
        released = self.service.unfreeze(
            freeze_id=freeze.freeze_id,
            actor='emp1',
            actor_type='tier2',
        )
        self.assertEqual(released.status, 'released')
        self.assertIsNone(enforce_freeze(self.service, operation='transfer', account='1001', userid='alice'))

    def test_customer_can_unfreeze_own_customer_reason(self):
        freeze = self.freeze()
        released = self.service.unfreeze(
            freeze_id=freeze.freeze_id,
            actor='alice',
            actor_type='customer',
            owner_userid='alice',
        )
        self.assertEqual(released.status, 'released')

    def test_duplicate_freeze_409(self):
        self.freeze()
        with self.assertRaises(FreezeError) as ctx:
            self.freeze()
        self.assertEqual(ctx.exception.code, 'freeze_duplicate')

    def test_customer_scope_blocks_all_accounts(self):
        self.service.freeze_account(
            owner_userid='alice',
            actor='emp1',
            actor_type='tier1',
            scope='customer',
            reason='legal',
        )
        blocked = enforce_freeze(self.service, operation='withdraw', account='1001', userid='alice')
        self.assertEqual(blocked[0]['error'], 'account_frozen')
        blocked2 = enforce_freeze(self.service, operation='transfer', account='8888', userid='alice')
        self.assertEqual(blocked2[0]['error'], 'account_frozen')

    def test_employee_skips_ownership_check(self):
        freeze = self.service.freeze_account(
            owner_userid='bob',
            actor='emp1',
            actor_type='admin',
            account='5555',
            reason='employee',
        )
        self.assertEqual(freeze.account, '5555')

    def test_stop_payment_blocks_deposit(self):
        stop = self.service.stop_payment(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            cheque_no='77',
            account='1001',
        )
        self.assertEqual(stop.status, 'active')
        blocked = enforce_stop(self.service, cheque_no='77')
        self.assertEqual(blocked[1], 403)
        self.assertEqual(blocked[0]['error'], 'cheque_stopped')
        self.assertIsNone(enforce_stop(self.service, cheque_no='78'))

    def test_duplicate_stop(self):
        self.service.stop_payment(
            owner_userid='alice', actor='alice', actor_type='customer', cheque_no='9',
        )
        with self.assertRaises(FreezeError) as ctx:
            self.service.stop_payment(
                owner_userid='alice', actor='alice', actor_type='customer', cheque_no='9',
            )
        self.assertEqual(ctx.exception.code, 'stop_duplicate')

    def test_cancel_stop_allows_deposit(self):
        stop = self.service.stop_payment(
            owner_userid='alice', actor='alice', actor_type='customer', cheque_no='12',
        )
        self.service.cancel_stop(stop_id=stop.stop_id, actor='alice', actor_type='customer', owner_userid='alice')
        self.assertIsNone(enforce_stop(self.service, cheque_no='12'))

    def test_cancel_stop_wrong_user_forbidden(self):
        stop = self.service.stop_payment(
            owner_userid='alice', actor='alice', actor_type='customer', cheque_no='13',
        )
        with self.assertRaises(FreezeError) as ctx:
            self.service.cancel_stop(stop_id=stop.stop_id, actor='bob', actor_type='customer', owner_userid='bob')
        self.assertEqual(ctx.exception.code, 'stop_forbidden')

    def test_disabled_policy_does_not_block(self):
        service = FreezeService(FreezePolicy(enabled=False), MemoryFreezeStore())
        with self.assertRaises(FreezeError) as ctx:
            service.freeze_account(
                owner_userid='alice', actor='alice', actor_type='customer', account='1001',
            )
        self.assertEqual(ctx.exception.code, 'freeze_disabled')

    def test_inbound_blocked_when_configured(self):
        service = FreezeService(
            FreezePolicy(inbound_blocked=True),
            MemoryFreezeStore(),
        )
        service.freeze_account(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='1001', own_accounts=['1001'],
        )
        blocked = enforce_freeze(service, operation='deposit', account='1001', userid='alice')
        self.assertEqual(blocked[0]['error'], 'account_frozen')

    def test_invalid_account_on_enforce(self):
        body, status = enforce_freeze(self.service, operation='transfer', account='nope')
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_account')

    def test_snapshot_lists_active_accounts(self):
        self.freeze('1001')
        self.service.stop_payment(
            owner_userid='alice', actor='alice', actor_type='customer', cheque_no='3',
        )
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['open_freeze_count'], 1)
        self.assertEqual(snap['frozen_accounts'], ['1001'])
        self.assertEqual(snap['open_stop_count'], 1)
        self.assertTrue(snap['enabled'])

    def test_own_accounts_from_payload(self):
        accounts = {
            'savings': {'Account': 1001, 'Balance': 10},
            'checkin': 'None',
            'credit': {'Account': '2002.0', 'Balance': 1},
        }
        self.assertEqual(own_accounts_from_customer_payload(accounts), ['1001', '2002'])

    def test_sqlite_reopen_preserves_freeze_and_stop(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        os.unlink(path)
        try:
            store = SqliteFreezeStore(path)
            service = FreezeService(FreezePolicy(), store, clock=lambda: 5.0)
            freeze = service.freeze_account(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', own_accounts=['1001'],
            )
            service.stop_payment(
                owner_userid='alice', actor='alice', actor_type='customer', cheque_no='44',
            )
            reopened = FreezeService(FreezePolicy(), SqliteFreezeStore(path))
            self.assertEqual(reopened.find_active(account='1001').freeze_id, freeze.freeze_id)
            self.assertIsNotNone(enforce_stop(reopened, cheque_no='44'))
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass

    def test_unfreeze_missing_404(self):
        with self.assertRaises(FreezeError) as ctx:
            self.service.unfreeze(freeze_id='nope', actor='alice', actor_type='customer', owner_userid='alice')
        self.assertEqual(ctx.exception.code, 'freeze_not_found')

    def test_stop_not_found(self):
        with self.assertRaises(FreezeError) as ctx:
            self.service.cancel_stop(stop_id='nope', actor='alice', actor_type='customer', owner_userid='alice')
        self.assertEqual(ctx.exception.code, 'stop_not_found')


if __name__ == '__main__':
    unittest.main()
