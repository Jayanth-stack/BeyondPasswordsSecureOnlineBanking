import os
import unittest

from flask import Flask, jsonify, request, session

from utility.settlement import (
    HoldPolicy,
    MemorySettlementStore,
    SettlementService,
    attach_hold_routes,
    enforce_hold,
    own_accounts_from_customer_payload,
    remember_if_destination,
)


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own():
        return request.get_json(silent=True).get('own_accounts') or ['1001', '1002']

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        if values.get('userid') != session['userid']:
            return jsonify({'message': 'User ID mismatch'}), 401
        held = enforce_hold(
            service,
            userid=session['userid'],
            usertype=session.get('usertype', 'customer'),
            operation='transfer',
            from_account=values.get('fromAccount'),
            to_account=values.get('toAccount'),
            amount=values.get('amount'),
            own_accounts=_own(),
        )
        if held:
            body, status, headers = held
            response = jsonify(body)
            response.status_code = status
            for key, value in headers.items():
                response.headers[key] = value
            return response
        executed.append(('transfer', values.get('toAccount'), values.get('amount')))
        remember_if_destination(service, session['userid'], values.get('toAccount'))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        held = enforce_hold(
            service,
            userid=session['userid'],
            usertype=session.get('usertype', 'customer'),
            operation='withdraw',
            from_account=values.get('account'),
            to_account='',
            amount=values.get('amount'),
        )
        if held:
            return jsonify(held[0]), held[1]
        executed.append(('withdraw', values.get('account'), values.get('amount')))
        return jsonify({'message': 'done'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        held = enforce_hold(
            service,
            userid=session['userid'],
            usertype=session.get('usertype', 'customer'),
            operation='cheque',
            from_account=values.get('from_account'),
            to_account=values.get('to_account'),
            amount=values.get('amount'),
            own_accounts=_own(),
        )
        if held:
            return jsonify(held[0]), held[1]
        executed.append(('cheque', values.get('to_account'), values.get('amount')))
        remember_if_destination(service, session['userid'], values.get('to_account'))
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        executed.append(('deposit', values.get('account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/requestFunds', methods=['POST'])
    def request_funds():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        executed.append(('request', None, None))
        return jsonify({'message': 'Request Sent'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        service.settle_due(lambda hold: executed.append(('settled', hold.operation, hold.amount)) or 'queued')
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Holds': service.snapshot(session['userid']),
        }), 200

    def executor(hold):
        executed.append(('settled', hold.operation, hold.amount))
        return 'queued'

    attach_hold_routes(app, service, executor=executor)
    return app


class SettlementRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.executed = []
        self.service = SettlementService(
            HoldPolicy(new_destination_seconds=50, base_hold_seconds=0),
            MemorySettlementStore(),
            clock=lambda: self.now[0],
        )
        self.app = build_app(self.service, self.executed)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_transfer_401(self):
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10',
        })
        self.assertEqual(response.status_code, 401)

    def test_new_dest_transfer_held_then_list_then_cancel(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '25',
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()['error'], 'held')
        self.assertEqual(self.executed, [])
        hold_id = response.get_json()['hold_id']

        listed = self.client.post('/listHolds', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Holds']['open_count'], 1)

        cancelled = self.client.post('/cancelHold', json={'userid': 'alice', 'hold_id': hold_id})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()['hold']['status'], 'cancelled')
        self.now[0] = 2_000
        self.client.post('/settleDue', json={'userid': 'alice'})
        self.assertEqual(self.executed, [])

    def test_settle_due_executes_once(self):
        self.login()
        self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '25',
        })
        self.now[0] = 1_080
        settled = self.client.post('/settleDue', json={'userid': 'alice'})
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(len(settled.get_json()['settled']), 1)
        self.assertEqual(self.executed, [('settled', 'transfer', '25.00')])
        self.client.post('/settleDue', json={'userid': 'alice'})
        self.assertEqual(self.executed, [('settled', 'transfer', '25.00')])

        follow = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '8',
        })
        self.assertEqual(follow.status_code, 200)
        self.assertIn(('transfer', '2002', '8'), self.executed)

    def test_internal_transfer_and_teller_skip(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '1002', 'amount': '12',
            'own_accounts': ['1001', '1002'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(('transfer', '1002', '12'), self.executed)

        self.login('emp1', 'tier1')
        response = self.client.post('/fundTransfer', json={
            'userid': 'emp1', 'fromAccount': '1001', 'toAccount': '9999', 'amount': '12',
        })
        self.assertEqual(response.status_code, 200)

    def test_withdraw_immediate_when_base_zero_cheque_held(self):
        self.login()
        withdraw = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '20',
        })
        self.assertEqual(withdraw.status_code, 200)
        cheque = self.client.post('/getCashierCheque', json={
            'userid': 'alice', 'from_account': '1001', 'to_account': '3003', 'amount': '15',
        })
        self.assertEqual(cheque.status_code, 202)
        deposit = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '40',
        })
        self.assertEqual(deposit.status_code, 200)
        request_funds = self.client.post('/requestFunds', json={
            'userid': 'alice', 'fromAccount': '9', 'toAccount': '1001', 'amount': '5',
        })
        self.assertEqual(request_funds.status_code, 200)
        self.assertIn(('withdraw', '1001', '20'), self.executed)
        self.assertIn(('deposit', '1001', '40'), self.executed)
        self.assertIn(('request', None, None), self.executed)

    def test_load_customer_snapshot_and_mismatch(self):
        self.login()
        self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '25',
        })
        loaded = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(loaded.status_code, 200)
        snapshot = loaded.get_json()['Holds']
        self.assertTrue(snapshot['enabled'])
        self.assertEqual(snapshot['open_count'], 1)
        self.assertEqual(snapshot['new_destination_seconds'], 50)

        denied = self.client.post('/listHolds', json={'userid': 'bob'})
        self.assertEqual(denied.status_code, 403)
        missing = self.client.post('/cancelHold', json={'userid': 'alice', 'hold_id': 'nope'})
        self.assertEqual(missing.status_code, 404)

    def test_own_accounts_helper_and_wiring_scan(self):
        accounts = {
            'savings': {'Account': 1001, 'Balance': 10},
            'checkin': 'None',
            'credit': {'Account': '2002', 'Balance': 0},
        }
        self.assertEqual(own_accounts_from_customer_payload(accounts), ['1001', '2002'])

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'app.py'), encoding='utf-8') as handle:
            app_src = handle.read()
        with open(os.path.join(root, 'static/main_js/customer.js'), encoding='utf-8') as handle:
            js_src = handle.read()
        with open(os.path.join(root, 'templates/customer.html'), encoding='utf-8') as handle:
            html_src = handle.read()
        self.assertIn('enforce_hold', app_src)
        self.assertIn('attach_hold_routes', app_src)
        self.assertIn("_maybe_hold('transfer'", app_src)
        self.assertIn("_maybe_hold('withdraw'", app_src)
        self.assertIn("_maybe_hold('cheque'", app_src)
        self.assertIn('held_transfers_menu', js_src)
        self.assertIn('cancelHold', js_src)
        self.assertIn('held_transfers_pane', html_src)
        self.assertIn('Holds', app_src)


if __name__ == '__main__':
    unittest.main()
