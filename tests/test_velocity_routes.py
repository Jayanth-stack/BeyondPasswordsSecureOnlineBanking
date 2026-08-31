"""Feature-path tests for outbound velocity on money-moving routes.

Does not import app.py (MySQL connects at import). Mounts the same
enforce_velocity helper the real routes use onto a tiny Flask app.
"""

import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from decimal import Decimal

from utility.velocity import (
    LimitBand,
    MemoryVelocityStore,
    VelocityPolicy,
    VelocityService,
    attach_account_snapshots,
    enforce_velocity,
    set_service,
)


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _denied(operation, account, amount):
        result = enforce_velocity(session, operation, account, amount)
        if result is None:
            return None
        payload, status, headers = result
        resp = jsonify(payload)
        resp.status_code = status
        for key, value in headers.items():
            resp.headers[key] = value
        return resp

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _denied('transfer', values['fromAccount'], values['amount'])
        if denied:
            return denied
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _denied('withdraw', values['account'], values['amount'])
        if denied:
            return denied
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _denied('cheque', values['from_account'], values['amount'])
        if denied:
            return denied
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/loadCustomer', methods=['GET', 'POST'])
    def load_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        accounts = {
            'checkin': {'Account': 1001, 'Balance': 900},
            'savings': 'None',
            'credit': 'None',
        }
        return jsonify({
            'Accounts': accounts,
            'Velocity': attach_account_snapshots(session, accounts),
        }), 200

    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class VelocityRouteTests(unittest.TestCase):
    def setUp(self):
        policy = VelocityPolicy({
            ('customer', 'transfer'): LimitBand(Decimal('100.00'), 3, Decimal('80.00'), 86400),
            ('customer', 'withdraw'): LimitBand(Decimal('50.00'), 2, Decimal('40.00'), 86400),
            ('customer', 'cheque'): LimitBand(Decimal('70.00'), 2, Decimal('70.00'), 86400),
            ('customer', 'outbound'): LimitBand(Decimal('150.00'), 5, None, 86400),
            ('employee', 'transfer'): LimitBand(Decimal('500.00'), 10, Decimal('400.00'), 86400),
            ('employee', 'withdraw'): LimitBand(Decimal('200.00'), 10, Decimal('200.00'), 86400),
            ('employee', 'cheque'): LimitBand(Decimal('500.00'), 10, Decimal('400.00'), 86400),
        })
        self.svc = VelocityService(store=MemoryVelocityStore(), policy=policy)
        set_service(self.svc)
        self.app = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        set_service(None)

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_transfer_under_limit_queues(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '40.00',
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn('approved', res.get_json()['message'])

    def test_second_transfer_over_daily_is_429(self):
        self._login()
        first = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '80.00',
        })
        self.assertEqual(first.status_code, 200)
        second = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '40.00',
        })
        self.assertEqual(second.status_code, 429)
        body = second.get_json()
        self.assertEqual(body['error'], 'daily_amount_exceeded')
        self.assertEqual(body['remaining_amount'], '20.00')
        self.assertIn('Remaining: $20.00', body['message'])
        self.assertTrue(second.headers.get('Retry-After'))

    def test_per_txn_is_400_or_429_without_consuming(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '80.01',
        })
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.get_json()['error'], 'per_txn_exceeded')
        follow = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '80.00',
        })
        self.assertEqual(follow.status_code, 200)

    def test_invalid_amount_is_400(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': 'nope',
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['error'], 'invalid_amount')

    def test_withdraw_count_limit(self):
        self._login()
        self.assertEqual(self.client.post('/withdrawAmount', json={'account': 1001, 'amount': '10'}).status_code, 200)
        self.assertEqual(self.client.post('/withdrawAmount', json={'account': 1001, 'amount': '10'}).status_code, 200)
        blocked = self.client.post('/withdrawAmount', json={'account': 1001, 'amount': '10'})
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.get_json()['error'], 'daily_count_exceeded')

    def test_cheque_uses_cheque_band(self):
        self._login()
        ok = self.client.post('/getCashierCheque', json={
            'from_account': 1001, 'to_account': 2001, 'amount': '70.00',
        })
        self.assertEqual(ok.status_code, 200)
        blocked = self.client.post('/getCashierCheque', json={
            'from_account': 1001, 'to_account': 2001, 'amount': '1.00',
        })
        self.assertEqual(blocked.status_code, 429)

    def test_deposit_is_not_velocity_gated(self):
        self._login()
        self.svc.consume('customer', 'transfer', '80.00', account=1001, userid='alice')
        res = self.client.post('/depositAmount', json={'account': 1001, 'amount': '9999'})
        self.assertEqual(res.status_code, 200)

    def test_teller_can_move_more_than_customer(self):
        self._login('teller1', 'tier1')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '300.00',
        })
        self.assertEqual(res.status_code, 200)

    def test_unauthenticated_rejected_before_velocity(self):
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '10.00',
        })
        self.assertEqual(res.status_code, 401)

    def test_load_customer_includes_remaining(self):
        self._login()
        self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '40.00',
        })
        res = self.client.post('/loadCustomer', json={})
        self.assertEqual(res.status_code, 200)
        velocity = res.get_json()['Velocity']
        self.assertEqual(velocity['1001']['transfer']['used_amount'], '40.00')
        self.assertEqual(velocity['1001']['transfer']['remaining_amount'], '60.00')
        self.assertEqual(velocity['outbound']['used_amount'], '40.00')


if __name__ == '__main__':
    unittest.main()
