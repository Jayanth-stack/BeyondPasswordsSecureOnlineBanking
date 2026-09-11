import unittest

from flask import Flask, jsonify, request, session

from utility.overdraft import (
    OverdraftPolicy,
    OverdraftService,
    MemoryOverdraftStore,
    attach_overdraft_routes,
    enforce_overdraft,
    set_service,
)


BALANCES = {'9001': ('credit', '0.00'), '1001': ('checkin', '50.00')}


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['1001']

    def _maybe(operation, account, amount, reserve=False, consume=False, owner_userid=None):
        owner = owner_userid if owner_userid is not None else (
            session.get('userid') if session.get('usertype') == 'customer' else None
        )
        if consume:
            service.capture_matching(account, amount)
        blocked = enforce_overdraft(
            service,
            operation=operation,
            account=account,
            amount=amount,
            userid=owner,
            reserve=reserve,
        )
        if not blocked:
            return None
        body, status = blocked
        return jsonify(body), status

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        if values.get('userid') != session['userid']:
            return jsonify({'message': 'User ID mismatch'}), 401
        gated = _maybe('transfer', values.get('fromAccount'), values.get('amount'), reserve=True)
        if gated is not None:
            return gated
        executed.append(('transfer', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('withdraw', values.get('account'), values.get('amount'))
        if gated is not None:
            return gated
        executed.append(('withdraw', values.get('account'), values.get('amount')))
        return jsonify({'message': 'done'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('cheque', values.get('from_account'), values.get('amount'), reserve=True)
        if gated is not None:
            return gated
        executed.append(('cheque', values.get('from_account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('deposit', values.get('account'), values.get('amount'))
        if gated is not None:
            return gated
        executed.append(('deposit', values.get('account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/approveRequest', methods=['POST'])
    def approve():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe(
            'approve',
            values.get('from_account'),
            values.get('amount'),
            consume=True,
            owner_userid=values.get('customer_id'),
        )
        if gated is not None:
            return gated
        executed.append(('approve', values.get('from_account')))
        return jsonify({'message': 'done'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {
                'savings': 'None',
                'checkin': {'Account': 1001, 'Balance': 50},
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Overdrafts': service.snapshot(session['userid'], balances={'1001': 50}),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        owner = values.get('customer_id')
        return jsonify({
            'Accounts': {
                'savings': 'None',
                'checkin': {'Account': 1001, 'Balance': 50},
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'Overdrafts': service.snapshot(owner, balances={'1001': 50}),
        }), 200

    attach_overdraft_routes(app, service, own_accounts_loader=_own)
    return app


class OverdraftRouteTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.service = OverdraftService(
            OverdraftPolicy(),
            MemoryOverdraftStore(),
            is_deposit_loader=lambda account, userid: str(account) in {'1001', '1001.0'},
            balance_loader=lambda account: BALANCES.get(str(int(float(account)))),
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

    def test_unauthenticated_set_limit_401(self):
        response = self.client.post('/setOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'limit': '200'
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/listOverdrafts', json={'userid': 'eve'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_set_limit(self):
        self.login()
        response = self.client.post('/setOverdraft', json={
            'userid': 'alice', 'account': '1001', 'limit': '200'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'overdraft_forbidden')

    def test_staff_sets_limit(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'limit': '200'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['facility']['base_limit'], '200.00')
        self.assertTrue(response.get_json()['facility']['enrolled'])

    def test_unenrolled_over_balance_transfer_403(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '80'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'overdraft_exceeded')
        self.assertEqual(self.executed, [])

    def test_enrolled_over_balance_transfer_ok(self):
        self.login('t1', 'tier1')
        self.client.post('/setOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'limit': '200'
        })
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '80'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.executed), 1)

    def test_within_limit_transfer_reserves(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '40'
        })
        self.assertEqual(response.status_code, 200)
        second = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '20'
        })
        self.assertEqual(second.status_code, 403)

    def test_credit_withdraw_not_gated(self):
        self.login()
        response = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '9001', 'amount': '40'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.executed, [('withdraw', '9001', '40')])

    def test_deposit_still_allowed_when_overdrawn(self):
        self.login('t1', 'tier1')
        self.client.post('/setOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'limit': '200'
        })
        self.login()
        response = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '9000'
        })
        self.assertEqual(response.status_code, 200)

    def test_approve_consumes_reservation(self):
        self.login()
        self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '40'
        })
        response = self.client.post('/approveRequest', json={
            'userid': 'alice', 'customer_id': 'alice', 'from_account': '1001', 'amount': '40'
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(('approve', '1001'), self.executed)

    def test_customer_request_overdraft_201(self):
        self.login()
        response = self.client.post('/requestOverdraft', json={
            'userid': 'alice', 'account': '1001', 'requested_limit': '200', 'reason': 'float',
            'own_accounts': ['1001'],
        })
        self.assertEqual(response.status_code, 201)
        request_id = response.get_json()['request']['request_id']
        self.login('t1', 'tier1')
        decided = self.client.post('/decideOverdraftRequest', json={
            'userid': 't1', 'request_id': request_id, 'decision': 'approve'
        })
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.get_json()['request']['status'], 'approved')

    def test_staff_revoke(self):
        self.login('t1', 'tier1')
        self.client.post('/setOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001', 'limit': '200'
        })
        response = self.client.post('/revokeOverdraft', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '1001'
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['facility']['enrolled'])

    def test_load_customer_includes_snapshot(self):
        self.login()
        response = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn('Overdrafts', body)
        self.assertEqual(body['Overdrafts']['facilities'][0]['effective_limit'], '0.00')

    def test_get_customer_staff_snapshot(self):
        self.login('t1', 'tier1')
        response = self.client.post('/getCustomer', json={'userid': 't1', 'customer_id': 'alice'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Overdrafts', response.get_json())

    def test_invalid_amount_400(self):
        self.login()
        response = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '-5'
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_amount')

    def test_employee_charge_still_limited(self):
        self.login('t1', 'tier1')
        response = self.client.post('/fundTransfer', json={
            'userid': 't1', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '80'
        })
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
