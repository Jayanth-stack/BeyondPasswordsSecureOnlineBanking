"""Exercise the same account-access gate the money-moving routes use.

This does not import app.py (MySQL connects at module import). It mounts the
identical helper pattern onto a tiny Flask app so we can prove the feature
path: owned debit succeeds, foreign debit is 403, teller debit succeeds.
"""

import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from utility.account_access import (
    AccountAccess,
    AccountRecord,
    MemoryAccountRepository,
    authorize_account,
    flask_error,
    set_access,
)


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _require_account(account_no, purpose):
        return flask_error(authorize_account(session, account_no, purpose))

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _require_account(values['fromAccount'], 'transfer_from')
        if denied:
            return denied
        denied = _require_account(values['toAccount'], 'transfer_to')
        if denied:
            return denied
        return jsonify({'message': 'queued'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _require_account(values['account'], 'debit')
        if denied:
            return denied
        return jsonify({'message': 'Amount Debited'}), 200

    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class MoneyRouteGateTests(unittest.TestCase):
    def setUp(self):
        repo = MemoryAccountRepository(accounts=[
            AccountRecord(1001, 'alice', 'checkin', True, 500),
            AccountRecord(2001, 'bob', 'checkin', True, 800),
        ])
        set_access(AccountAccess(repo))
        self.app = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        set_access(None)

    def _login(self, userid, usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_customer_withdraw_own_account(self):
        self._login('alice')
        res = self.client.post('/withdrawAmount', json={'account': 1001})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['message'], 'Amount Debited')

    def test_customer_cannot_withdraw_foreign_account(self):
        self._login('alice')
        res = self.client.post('/withdrawAmount', json={'account': 2001})
        self.assertEqual(res.status_code, 403)
        body = res.get_json()
        self.assertEqual(body['error'], 'account_not_owned')
        self.assertEqual(body['message'], 'Not authorized to operate on this account')

    def test_customer_cannot_transfer_from_foreign_account(self):
        self._login('alice')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 2001,
            'toAccount': 1001,
        })
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()['error'], 'account_not_owned')

    def test_customer_can_transfer_from_own_to_other(self):
        self._login('alice')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001,
            'toAccount': 2001,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['message'], 'queued')

    def test_teller_can_transfer_between_any_accounts(self):
        self._login('emp1', 'tier1')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001,
            'toAccount': 2001,
        })
        self.assertEqual(res.status_code, 200)

    def test_unauthenticated_withdraw_rejected_before_policy(self):
        res = self.client.post('/withdrawAmount', json={'account': 1001})
        self.assertEqual(res.status_code, 401)


if __name__ == '__main__':
    unittest.main()
