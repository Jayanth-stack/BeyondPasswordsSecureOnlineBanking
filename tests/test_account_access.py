import unittest

from utility.account_access import (
    AccountAccess,
    AccountRecord,
    AccountStoreUnavailable,
    ChequeRecord,
    MemoryAccountRepository,
    PURPOSE_RULES,
    flask_error,
    parse_account_no,
    parse_cheque_no,
    set_access,
)


class FailingRepository(MemoryAccountRepository):
    def fetch_account(self, account_no: int):
        raise AccountStoreUnavailable('down')

    def fetch_cheque(self, cheque_no: int):
        raise AccountStoreUnavailable('down')


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryAccountRepository(accounts=[
            AccountRecord(1001, 'alice', 'checkin', True, 500),
            AccountRecord(1002, 'alice', 'savings', False, 50),
            AccountRecord(2001, 'bob', 'checkin', True, 800),
        ], cheques=[
            ChequeRecord(55, 'bob', 1001, 2001, 25.0, True),
            ChequeRecord(66, 'alice', 2001, 1001, 40.0, True),
        ])
        self.access = AccountAccess(self.repo)
        self.alice = {'userid': 'alice', 'usertype': 'customer'}
        self.bob = {'userid': 'bob', 'usertype': 'customer'}
        self.teller = {'userid': 'emp1', 'usertype': 'tier1'}
        self.admin = {'userid': 'admin1', 'usertype': 'admin'}

    def test_customer_can_debit_own_active_account(self):
        decision = self.access.authorize(self.alice, 1001, 'debit')
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.account.customer_id, 'alice')

    def test_customer_cannot_debit_someone_elses_account(self):
        decision = self.access.authorize(self.alice, 2001, 'debit')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status_code, 403)
        self.assertEqual(decision.error, 'account_not_owned')

    def test_customer_missing_account_is_indistinguishable_from_not_owned(self):
        decision = self.access.authorize(self.alice, 9999, 'debit')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status_code, 403)
        self.assertEqual(decision.error, 'account_not_owned')

    def test_customer_cannot_debit_own_inactive_account(self):
        decision = self.access.authorize(self.alice, 1002, 'debit')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.error, 'account_inactive')

    def test_customer_can_transfer_to_another_customers_account(self):
        decision = self.access.authorize(self.alice, 2001, 'transfer_to')
        self.assertTrue(decision.allowed)

    def test_customer_cannot_transfer_from_another_customers_account(self):
        decision = self.access.authorize(self.alice, 2001, 'transfer_from')
        self.assertEqual(decision.error, 'account_not_owned')

    def test_request_funds_requires_owning_destination(self):
        own_dest = self.access.authorize(self.alice, 1001, 'request_to')
        other_dest = self.access.authorize(self.alice, 2001, 'request_to')
        other_source = self.access.authorize(self.alice, 2001, 'request_from')
        self.assertTrue(own_dest.allowed)
        self.assertEqual(other_dest.error, 'account_not_owned')
        self.assertTrue(other_source.allowed)

    def test_teller_can_debit_any_active_account(self):
        decision = self.access.authorize(self.teller, 2001, 'debit')
        self.assertTrue(decision.allowed)

    def test_teller_missing_account_is_404(self):
        decision = self.access.authorize(self.teller, 9999, 'debit')
        self.assertEqual(decision.status_code, 404)
        self.assertEqual(decision.error, 'account_not_found')

    def test_admin_is_a_teller(self):
        decision = self.access.authorize(self.admin, 1001, 'credit')
        self.assertTrue(decision.allowed)

    def test_unauthenticated_is_401(self):
        decision = self.access.authorize({}, 1001, 'debit')
        self.assertEqual(decision.status_code, 401)
        self.assertEqual(decision.error, 'unauthenticated')

    def test_invalid_account_number(self):
        decision = self.access.authorize(self.alice, 'abc', 'debit')
        self.assertEqual(decision.status_code, 400)
        self.assertEqual(decision.error, 'invalid_account')

    def test_zero_account_number_rejected(self):
        decision = self.access.authorize(self.alice, 0, 'debit')
        self.assertEqual(decision.error, 'invalid_account')

    def test_unknown_purpose(self):
        decision = self.access.authorize(self.alice, 1001, 'launch_missiles')
        self.assertEqual(decision.error, 'unknown_purpose')

    def test_issue_cheque_requires_own_source(self):
        own = self.access.authorize(self.alice, 1001, 'issue_cheque_from')
        other = self.access.authorize(self.alice, 2001, 'issue_cheque_from')
        dest = self.access.authorize(self.alice, 2001, 'issue_cheque_to')
        self.assertTrue(own.allowed)
        self.assertEqual(other.error, 'account_not_owned')
        self.assertTrue(dest.allowed)

    def test_cheque_credit_requires_owning_destination(self):
        # cheque 55 pays alice's 1001
        ok = self.access.authorize_cheque_credit(self.alice, 55)
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.cheque.cheque_no, 55)

        stolen = self.access.authorize_cheque_credit(self.alice, 66)
        self.assertFalse(stolen.allowed)
        self.assertEqual(stolen.error, 'account_not_owned')

    def test_unknown_cheque_falls_through_to_existing_handler(self):
        decision = self.access.authorize_cheque_credit(self.alice, 404)
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.cheque)

    def test_teller_may_deposit_any_resolved_cheque(self):
        decision = self.access.authorize_cheque_credit(self.teller, 66)
        self.assertTrue(decision.allowed)

    def test_store_outage_is_503(self):
        access = AccountAccess(FailingRepository())
        decision = access.authorize(self.alice, 1001, 'debit')
        self.assertEqual(decision.status_code, 503)
        cheque = access.authorize_cheque_credit(self.alice, 55)
        self.assertEqual(cheque.status_code, 503)

    def test_customer_owns_helper(self):
        self.assertTrue(self.access.customer_owns('alice', 1001))
        self.assertFalse(self.access.customer_owns('alice', 2001))
        self.assertFalse(self.access.customer_owns('alice', 1))

    def test_list_owned_accounts(self):
        owned = self.access.list_owned_accounts('alice')
        self.assertEqual({a.account_no for a in owned}, {1001, 1002})

    def test_as_json_includes_error_code(self):
        decision = self.access.authorize(self.alice, 2001, 'debit')
        payload = decision.as_json()
        self.assertEqual(payload['message'], 'Not authorized to operate on this account')
        self.assertEqual(payload['error'], 'account_not_owned')

    def test_parse_helpers(self):
        self.assertEqual(parse_account_no('1001'), 1001)
        self.assertEqual(parse_cheque_no('55'), 55)
        with self.assertRaises(ValueError):
            parse_account_no(-3)
        with self.assertRaises(ValueError):
            parse_cheque_no('nope')

    def test_every_documented_purpose_exists_for_both_roles(self):
        self.assertEqual(set(PURPOSE_RULES['customer']), set(PURPOSE_RULES['teller']))

    def test_flask_error_none_when_allowed(self):
        decision = self.access.authorize(self.alice, 1001, 'debit')
        self.assertIsNone(flask_error(decision))

    def test_userid_without_usertype_is_treated_as_customer(self):
        decision = self.access.authorize({'userid': 'alice'}, 2001, 'debit')
        self.assertEqual(decision.error, 'account_not_owned')


class AccessInjectionTests(unittest.TestCase):
    def tearDown(self):
        set_access(None)

    def test_set_access_is_used_by_module_helpers(self):
        from utility import account_access as mod
        repo = MemoryAccountRepository(accounts=[
            AccountRecord(7, 'alice', 'checkin', True, 1),
        ])
        set_access(AccountAccess(repo))
        decision = mod.authorize_account({'userid': 'alice', 'usertype': 'customer'}, 7, 'debit')
        self.assertTrue(decision.allowed)


if __name__ == '__main__':
    unittest.main()
