import unittest

from flask import Flask, jsonify, request, session

from utility.interest import (
    InterestPolicy,
    InterestService,
    MemoryInterestStore,
    attach_interest_routes,
    set_service,
)


BALANCES = {'2002': ('savings', '10000.00'), '1001': ('checkin', '50.00'), '9001': ('credit', '0.00')}


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['2002']

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        if values.get('userid') != session['userid']:
            return jsonify({'message': 'User ID mismatch'}), 401
        executed.append(('transfer', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        executed.append(('deposit', values.get('account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {
                'savings': {'Account': 2002, 'Balance': 10000},
                'checkin': {'Account': 1001, 'Balance': 50},
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Interest': service.snapshot(session['userid'], balances={'2002': 10000}),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        owner = values.get('customer_id')
        return jsonify({
            'Accounts': {
                'savings': {'Account': 2002, 'Balance': 10000},
                'checkin': {'Account': 1001, 'Balance': 50},
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'Interest': service.snapshot(owner, balances={'2002': 10000}),
        }), 200

    attach_interest_routes(app, service, own_accounts_loader=_own)
    return app


class InterestRouteTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.credits = []
        self.now = [datetime_ts(2026, 1, 15)]
        self.service = InterestService(
            InterestPolicy(),
            MemoryInterestStore(),
            clock=lambda: self.now[0],
            is_savings_loader=lambda account, userid=None: str(account) in {'2002', '2002.0'},
            balance_loader=lambda account: BALANCES.get(str(int(float(account)))),
            credit_executor=lambda account, amount, remark: self.credits.append((account, str(amount))),
        )
        set_service(self.service)
        self.app = build_app(self.service, self.executed)
        self.client = self.app.test_client()

    def tearDown(self):
        set_service(None)

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_set_apy_401(self):
        response = self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '2.00'
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/listInterest', json={'userid': 'eve'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_set_apy(self):
        self.login()
        response = self.client.post('/setApy', json={
            'userid': 'alice', 'account': '2002', 'apy': '2.00'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'interest_forbidden')

    def test_staff_sets_apy(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '2.00'
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['account']['base_apy'], '2.00')
        self.assertTrue(body['account']['enrolled'])

    def test_checking_set_apy_403(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'apy': '2.00'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'not_savings')

    def test_invalid_apy_400(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': 'nope'
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_apy')

    def test_customer_request_201(self):
        self.login()
        response = self.client.post('/requestApy', json={
            'userid': 'alice', 'account': '2002', 'apy': '1.25', 'reason': 'promo'
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['request']['status'], 'pending')

    def test_staff_approve_request(self):
        self.login()
        created = self.client.post('/requestApy', json={
            'userid': 'alice', 'account': '2002', 'apy': '1.25'
        }).get_json()
        self.login('t1', 'tier1')
        response = self.client.post('/decideApyRequest', json={
            'userid': 't1', 'request_id': created['request']['request_id'], 'decision': 'approve'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['request']['status'], 'approved')
        listed = self.client.post('/listInterest', json={'userid': 't1', 'customer_id': 'alice'})
        self.assertEqual(listed.get_json()['Interest']['accounts'][0]['base_apy'], '1.25')

    def test_missing_request_404(self):
        self.login('t1', 'tier1')
        response = self.client.post('/decideApyRequest', json={
            'userid': 't1', 'request_id': 'missing', 'decision': 'deny'
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'request_not_found')

    def test_grant_and_revoke_promo(self):
        self.login('t1', 'tier1')
        self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '2.00'
        })
        granted = self.client.post('/grantPromoApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '1.00', 'seconds': 3600
        })
        self.assertEqual(granted.status_code, 200)
        self.assertEqual(granted.get_json()['account']['effective_apy'], '3.00')
        revoked = self.client.post('/revokePromoApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002'
        })
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.get_json()['account']['effective_apy'], '2.00')

    def test_load_customer_includes_interest_snapshot(self):
        self.login()
        response = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 200)
        snap = response.get_json()['Interest']
        self.assertIn('accounts', snap)
        self.assertIn('policy', snap)
        self.assertEqual(snap['accounts'][0]['account'], '2002')

    def test_get_customer_includes_interest_snapshot(self):
        self.login('t1', 'tier1')
        response = self.client.post('/getCustomer', json={'userid': 't1', 'customer_id': 'alice'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Interest', response.get_json())

    def test_deposit_and_transfer_still_succeed(self):
        self.login()
        transfer = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '2002', 'toAccount': '1001', 'amount': '25'
        })
        self.assertEqual(transfer.status_code, 200)
        deposit = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '2002', 'amount': '50'
        })
        self.assertEqual(deposit.status_code, 200)
        self.assertEqual(self.executed, [
            ('transfer', '2002', '25'),
            ('deposit', '2002', '50'),
        ])

    def test_post_due_after_month_end(self):
        self.login('t1', 'tier1')
        self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '4.25'
        })
        self.now[0] = datetime_ts(2026, 2, 1, 1)
        response = self.client.post('/postDueInterest', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()['posted']), 1)
        self.assertEqual(len(self.credits), 1)

    def test_customer_force_is_ignored(self):
        self.login('t1', 'tier1')
        self.client.post('/setApy', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '2002', 'apy': '4.25'
        })
        self.login()
        response = self.client.post('/postDueInterest', json={
            'userid': 'alice', 'account': '2002', 'force': True
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['posted'], [])


def datetime_ts(year, month, day, hour=0):
    from datetime import datetime, timezone
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


if __name__ == '__main__':
    unittest.main()
