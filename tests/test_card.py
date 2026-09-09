import os
import tempfile
import unittest

from utility.card import (
    AccountError,
    CardError,
    CardPolicy,
    CardService,
    MemoryCardStore,
    PinError,
    SqliteCardStore,
    credit_accounts_from_customer_payload,
    enforce_card,
    normalize_account,
    normalize_pin,
)


def _hash(pin):
    return 'h:' + pin


def _verify(pin, hashed):
    return hashed == 'h:' + pin


class CardCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.service = CardService(
            CardPolicy(),
            MemoryCardStore(),
            clock=lambda: self.now[0],
            hash_pin=_hash,
            verify_pin=_verify,
            is_credit_loader=lambda account, userid: account in {'9001', '9002'},
        )

    def set_pin(self, account='9001', pin='1234', **kwargs):
        defaults = dict(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            account=account,
            pin=pin,
            own_accounts=['9001'],
        )
        defaults.update(kwargs)
        return self.service.set_pin(**defaults)

    def test_normalize_account_strips_float_suffix(self):
        self.assertEqual(normalize_account('9001.0'), '9001')
        with self.assertRaises(AccountError):
            normalize_account('abc')

    def test_normalize_pin_rejects_short_and_letters(self):
        with self.assertRaises(PinError):
            normalize_pin('12')
        with self.assertRaises(PinError):
            normalize_pin('abcd')
        self.assertEqual(normalize_pin('0123'), '0123')
        self.assertEqual(normalize_pin('999999'), '999999')

    def test_credit_payload_helper(self):
        self.assertEqual(
            credit_accounts_from_customer_payload(
                {'credit': {'Account': 9001, 'Balance': -20}, 'savings': 'None'}
            ),
            ['9001'],
        )
        self.assertEqual(credit_accounts_from_customer_payload({'credit': 'None'}), [])

    def test_savings_not_gated(self):
        self.assertIsNone(enforce_card(
            self.service, operation='transfer', account='1001', userid='alice', pin='1234',
        ))

    def test_charge_requires_pin_once_credit(self):
        blocked = enforce_card(
            self.service, operation='transfer', account='9001', userid='alice',
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked[1], 403)
        self.assertEqual(blocked[0]['error'], 'pin_not_set')

        self.set_pin()
        blocked = enforce_card(
            self.service, operation='transfer', account='9001', userid='alice',
        )
        self.assertEqual(blocked[0]['error'], 'pin_required')

        self.assertIsNone(enforce_card(
            self.service, operation='transfer', account='9001', userid='alice', pin='1234',
        ))

    def test_deposit_not_gated(self):
        self.set_pin()
        self.service.lock_card(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', reason='lost', own_accounts=['9001'],
        )
        self.assertIsNone(enforce_card(
            self.service, operation='deposit', account='9001', userid='alice',
        ))
        blocked = enforce_card(
            self.service, operation='transfer', account='9001', userid='alice', pin='1234',
        )
        self.assertEqual(blocked[0]['error'], 'card_locked')

    def test_wrong_pin_then_lockout(self):
        self.set_pin()
        for _ in range(2):
            blocked = enforce_card(
                self.service, operation='withdraw', account='9001', userid='alice', pin='0000',
            )
            self.assertEqual(blocked[0]['error'], 'pin_incorrect')
        blocked = enforce_card(
            self.service, operation='withdraw', account='9001', userid='alice', pin='0000',
        )
        self.assertEqual(blocked[0]['error'], 'card_locked')
        card = self.service.store.get_by_account('9001')
        self.assertTrue(card.locked)
        self.assertEqual(card.lock_reason, 'pin_lockout')

    def test_pin_token_single_use(self):
        self.set_pin()
        _card, token = self.service.verify_and_issue_token(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', pin='1234', own_accounts=['9001'],
        )
        self.assertIsNone(enforce_card(
            self.service, operation='cheque', account='9001', userid='alice',
            pin_token=token.token,
        ))
        blocked = enforce_card(
            self.service, operation='cheque', account='9001', userid='alice',
            pin_token=token.token,
        )
        self.assertEqual(blocked[0]['error'], 'pin_token_invalid')

    def test_expired_pin_token(self):
        self.set_pin()
        _card, token = self.service.verify_and_issue_token(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', pin='1234', own_accounts=['9001'],
        )
        self.now[0] = 1_000.0 + 301
        blocked = enforce_card(
            self.service, operation='transfer', account='9001', userid='alice',
            pin_token=token.token,
        )
        self.assertEqual(blocked[0]['error'], 'pin_token_expired')

    def test_customer_cannot_lock_unowned(self):
        with self.assertRaises(CardError) as ctx:
            self.service.lock_card(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9002', reason='lost', own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'card_forbidden')

    def test_customer_cannot_apply_fraud(self):
        with self.assertRaises(CardError) as ctx:
            self.service.lock_card(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', reason='fraud', own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'card_forbidden')

    def test_employee_fraud_lock_blocks_customer_unlock(self):
        self.set_pin()
        card = self.service.lock_card(
            owner_userid='alice', actor='emp1', actor_type='tier2',
            account='9001', reason='fraud',
        )
        with self.assertRaises(CardError) as ctx:
            self.service.unlock_card(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', pin='1234', own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'card_lock_locked')
        released = self.service.unlock_card(
            owner_userid='alice', actor='emp1', actor_type='tier2', account='9001',
        )
        self.assertFalse(released.locked)
        self.assertIsNone(enforce_card(
            self.service, operation='transfer', account='9001', userid='alice', pin='1234',
        ))
        self.assertEqual(card.lock_reason, 'fraud')

    def test_customer_lock_and_unlock_with_pin(self):
        self.set_pin()
        locked = self.service.lock_card(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', reason='stolen', own_accounts=['9001'],
        )
        self.assertTrue(locked.locked)
        with self.assertRaises(CardError) as ctx:
            self.service.unlock_card(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', pin='0000', own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'pin_incorrect')
        unlocked = self.service.unlock_card(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', pin='1234', own_accounts=['9001'],
        )
        self.assertFalse(unlocked.locked)

    def test_duplicate_lock(self):
        self.service.lock_card(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', reason='lost', own_accounts=['9001'],
        )
        with self.assertRaises(CardError) as ctx:
            self.service.lock_card(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', reason='lost', own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'lock_duplicate')

    def test_change_pin_requires_current(self):
        self.set_pin()
        with self.assertRaises(CardError) as ctx:
            self.service.change_pin(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', current_pin='0000', new_pin='4321',
                own_accounts=['9001'],
            )
        self.assertEqual(ctx.exception.code, 'pin_incorrect')
        changed = self.service.change_pin(
            owner_userid='alice', actor='alice', actor_type='customer',
            account='9001', current_pin='1234', new_pin='4321',
            own_accounts=['9001'],
        )
        self.assertTrue(changed.pin_hash)
        self.assertIsNone(enforce_card(
            self.service, operation='transfer', account='9001', userid='alice', pin='4321',
        ))

    def test_employee_reset_pin_then_charge_blocked(self):
        self.set_pin()
        self.service.reset_pin(
            owner_userid='alice', actor='emp1', actor_type='tier1', account='9001',
        )
        blocked = enforce_card(
            self.service, operation='approve', account='9001', userid='alice', pin='1234',
        )
        self.assertEqual(blocked[0]['error'], 'pin_not_set')

    def test_customer_cannot_reset_pin(self):
        self.set_pin()
        with self.assertRaises(CardError) as ctx:
            self.service.reset_pin(
                owner_userid='alice', actor='alice', actor_type='customer', account='9001',
            )
        self.assertEqual(ctx.exception.code, 'card_forbidden')

    def test_employee_skips_pin_but_not_lock(self):
        self.set_pin()
        self.assertIsNone(enforce_card(
            self.service, operation='transfer', account='9001', userid='alice',
            actor_type='tier1',
        ))
        self.service.lock_card(
            owner_userid='alice', actor='emp1', actor_type='tier1',
            account='9001', reason='employee',
        )
        blocked = enforce_card(
            self.service, operation='transfer', account='9001', userid='alice',
            actor_type='tier1',
        )
        self.assertEqual(blocked[0]['error'], 'card_locked')

    def test_set_pin_duplicate(self):
        self.set_pin()
        with self.assertRaises(CardError) as ctx:
            self.set_pin()
        self.assertEqual(ctx.exception.code, 'pin_already_set')

    def test_public_dict_omits_hash(self):
        card = self.set_pin()
        payload = card.to_dict()
        self.assertNotIn('pin_hash', payload)
        self.assertTrue(payload['pin_set'])

    def test_disabled_policy(self):
        service = CardService(
            CardPolicy(enabled=False),
            MemoryCardStore(),
            hash_pin=_hash,
            verify_pin=_verify,
        )
        with self.assertRaises(CardError) as ctx:
            service.set_pin(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', pin='1234',
            )
        self.assertEqual(ctx.exception.code, 'card_disabled')
        self.assertIsNone(enforce_card(
            service, operation='transfer', account='9001', userid='alice',
        ))

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            service = CardService(
                CardPolicy(),
                SqliteCardStore(path),
                hash_pin=_hash,
                verify_pin=_verify,
                is_credit_loader=lambda account, userid: account == '9001',
            )
            service.set_pin(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='9001', pin='2468',
            )
            reloaded = CardService(
                CardPolicy(),
                SqliteCardStore(path),
                hash_pin=_hash,
                verify_pin=_verify,
                is_credit_loader=lambda account, userid: account == '9001',
            )
            self.assertIsNone(enforce_card(
                reloaded, operation='transfer', account='9001', userid='alice', pin='2468',
            ))
            snapshot = reloaded.snapshot('alice')
            self.assertEqual(snapshot['pin_set_accounts'], ['9001'])
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
