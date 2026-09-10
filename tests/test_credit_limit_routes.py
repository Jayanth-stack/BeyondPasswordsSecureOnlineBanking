import unittest

from flask import Flask, jsonify, request, session

from utility.credit_limit import (
    CreditLimitPolicy,
    CreditLimitService,
    MemoryCreditLimitStore,
    attach_credit_limit_routes,
    enforce_credit_limit,
    set_service,
)


BALANCES = {'9001': ( 'credit', '0.00'), '1001': ('savings', '50.00')}


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['9001']

    def _maybe(operation, account, amount, reserve=False, consume=False, owner_userid=None):
        owner = owner_userid if owner_userid is not None else (
            session.get('userid') if session.get('usertype') == 'customer' else None
        )
        if consume:
            service.capture_matching(account, amount)
        blocked = enforce_credit_limit(
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
        service.ensure_facility(userid=session['userid'], account='9001')
        return jsonify({
            'Accounts': {
                'savings': {'Account': 1001, 'Balance': 50},
                'checkin': 'None',
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'CreditLimits': service.snapshot(session['userid'], balances={'9001': 0}),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        owner = values.get('customer_id')
        service.ensure_facility(userid=owner, account='9001')
        return jsonify({
            'Accounts': {
                'savings': {'Account': 1001, 'Balance': 50},
                'checkin': 'None',
                'credit': {'Account': 9001, 'Balance': 0},
            },
            'Info': {'first_name': 'Ada'},
            'CreditLimits': service.snapshot(owner, balances={'9001': 0}),
        }), 200

    attach_credit_limit_routes(app, service, own_accounts_loader=_own)
    return app


class CreditLimitRouteTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.service = CreditLimitService(
            CreditLimitPolicy(),
            MemoryCreditLimitStore(),
            is_credit_loader=lambda account, userid: str(account) in {'9001', '9001.0'},
            balance_loader=lambda account: BALANCES.get(str(int(float(account)))),
        )
        self.service.ensure_facility(userid='alice', account='9001')
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
        response = self.client.post('/setCreditLimit', json={'userid': 't1', 'customer_id': 'alice', 'account': '9001', 'limit': '6000'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/listCreditLimits', json={'userid': 'eve'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_set_limit(self):
        self.login()
        response = self.client.post('/setCreditLimit', json={
            'userid': 'alice', 'account': '9001', 'limit': '8000'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'limit_forbidden')

    def test_staff_sets_limit(self):
        self.login('t1', 'tier1')
        response = self.client.post('/setCreditLimit', json={
            'userid': 't1', 'customer_id': 'alice', 'account': '9001', 'limit': '8000'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['facility']['base_limit'], '8000.00')

    def test_over_limit_transfer_403(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '6000'
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'credit_limit_exceeded')
        self.assertEqual(self.executed, [])

    def test_within_limit_transfer_reserves(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '4000'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.executed), 1)
        second = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '2000'
        })
        self.assertEqual(second.status_code, 403)

    def test_savings_withdraw_not_gated(self):
        self.login()
        response = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '40'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.executed, [('withdraw', '1001', '40')])

    def test_deposit_still_allowed_on_maxed_card(self):
        self.login()
        response = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '9001', 'amount': '9000'
        })
        self.assertEqual(response.status_code, 200)

    def test_approve_consumes_reservation(self):
        self.login()
        self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '4000'
        })
        response = self.client.post('/approveRequest', json={
            'userid': 'alice', 'customer_id': 'alice', 'from_account': '9001', 'amount': '4000'
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(('approve', '9001'), self.executed)

    def test_customer_request_increase_201(self):
        self.login()
        response = self.client.post('/requestCreditLimitIncrease', json={
            'userid': 'alice', 'account': '9001', 'requested_limit': '7000', 'reason': 'travel',
            'own_accounts': ['9001'],
        })
        self.assertEqual(response.status_code, 201)
        request_id = response.get_json()['request']['request_id']
        self.login('t1', 'tier1')
        decided = self.client.post('/decideCreditLimitRequest', json={
            'userid': 't1', 'request_id': request_id, 'decision': 'approve'
        })
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.get_json()['request']['status'], 'approved')

    def test_load_customer_includes_snapshot(self):
        self.login()
        response = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn('CreditLimits', body)
        self.assertEqual(body['CreditLimits']['facilities'][0]['effective_limit'], '5000.00')

    def test_get_customer_staff_snapshot(self):
        self.login('t1', 'tier1')
        response = self.client.post('/getCustomer', json={'userid': 't1', 'customer_id': 'alice'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('CreditLimits', response.get_json())

    def test_invalid_amount_400(self):
        self.login()
        response = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '9001', 'amount': '-5'
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_amount')

    def test_employee_charge_still_limited(self):
        self.login('t1', 'tier1')
        response = self.client.post('/fundTransfer', json={
            'userid': 't1', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '6000'
        })
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
