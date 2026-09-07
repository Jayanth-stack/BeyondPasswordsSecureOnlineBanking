import os
import unittest

from flask import Flask, jsonify, request, session

from utility.freeze import (
    FreezePolicy,
    FreezeService,
    MemoryFreezeStore,
    attach_freeze_routes,
    enforce_freeze,
    enforce_stop,
    own_accounts_from_customer_payload,
)


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['1001', '1002']

    def _owner():
        if session.get('usertype') == 'customer':
            return session.get('userid')
        return None

    def _maybe(operation, account):
        blocked = enforce_freeze(
            service,
            operation=operation,
            account=account,
            userid=_owner(),
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
        frozen = _maybe('transfer', values.get('fromAccount'))
        if frozen is not None:
            return frozen
        executed.append(('transfer', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        frozen = _maybe('withdraw', values.get('account'))
        if frozen is not None:
            return frozen
        executed.append(('withdraw', values.get('account'), values.get('amount')))
        return jsonify({'message': 'done'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        frozen = _maybe('cheque', values.get('from_account'))
        if frozen is not None:
            return frozen
        executed.append(('cheque', values.get('from_account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        frozen = _maybe('deposit', values.get('account'))
        if frozen is not None:
            return frozen
        executed.append(('deposit', values.get('account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/requestFunds', methods=['POST'])
    def request_funds():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        frozen = _maybe('request', values.get('fromAccount'))
        if frozen is not None:
            return frozen
        executed.append(('request', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request Sent'}), 200

    @app.route('/depositCheck', methods=['POST'])
    def deposit_check():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        stopped = enforce_stop(service, cheque_no=values.get('cheque_no'))
        if stopped is not None:
            return jsonify(stopped[0]), stopped[1]
        executed.append(('depositCheck', values.get('cheque_no')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/approveRequestEmp', methods=['POST'])
    def approve_emp():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        frozen = _maybe('approve', values.get('from_account'))
        if frozen is not None:
            return frozen
        executed.append(('approve', values.get('from_account')))
        return jsonify({'message': 'done'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Freezes': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Freezes': service.snapshot(values.get('customer_id')),
        }), 200

    attach_freeze_routes(app, service, own_accounts_loader=_own)
    return app


class FreezeRouteTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.service = FreezeService(FreezePolicy(), MemoryFreezeStore())
        self.app = build_app(self.service, self.executed)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_freeze_401(self):
        response = self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/freezeAccount', json={'userid': 'bob', 'account': '1001'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_freeze_then_transfer_403_then_unfreeze(self):
        self.login()
        frozen = self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(frozen.get_json()['freeze']['status'], 'active')

        blocked = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '25',
        })
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['error'], 'account_frozen')
        self.assertEqual(self.executed, [])

        freeze_id = frozen.get_json()['freeze']['freeze_id']
        released = self.client.post('/unfreezeAccount', json={'userid': 'alice', 'freeze_id': freeze_id})
        self.assertEqual(released.status_code, 200)
        ok = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '25',
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.executed[-1][0], 'transfer')

    def test_withdraw_and_cheque_blocked_deposit_allowed(self):
        self.login()
        self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        self.assertEqual(self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '10',
        }).status_code, 403)
        self.assertEqual(self.client.post('/getCashierCheque', json={
            'userid': 'alice', 'from_account': '1001', 'to_account': '2002', 'amount': '10',
        }).status_code, 403)
        self.assertEqual(self.client.post('/requestFunds', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10',
        }).status_code, 403)
        deposit = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '10',
        })
        self.assertEqual(deposit.status_code, 200)
        self.assertEqual(self.executed, [('deposit', '1001', '10')])

    def test_other_account_still_transfers(self):
        self.login()
        self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        ok = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1002', 'toAccount': '2002', 'amount': '5',
        })
        self.assertEqual(ok.status_code, 200)

    def test_stop_payment_blocks_deposit_check(self):
        self.login()
        placed = self.client.post('/stopPayment', json={'userid': 'alice', 'cheque_no': '77', 'account': '1001'})
        self.assertEqual(placed.status_code, 200)
        blocked = self.client.post('/depositCheck', json={'userid': 'alice', 'cheque_no': '77'})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['error'], 'cheque_stopped')
        other = self.client.post('/depositCheck', json={'userid': 'alice', 'cheque_no': '78'})
        self.assertEqual(other.status_code, 200)
        stop_id = placed.get_json()['stop']['stop_id']
        cancelled = self.client.post('/cancelStopPayment', json={'userid': 'alice', 'stop_id': stop_id})
        self.assertEqual(cancelled.status_code, 200)
        allowed = self.client.post('/depositCheck', json={'userid': 'alice', 'cheque_no': '77'})
        self.assertEqual(allowed.status_code, 200)

    def test_list_and_load_customer_snapshot(self):
        self.login()
        self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        listed = self.client.post('/listFreezes', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Freezes']['open_freeze_count'], 1)
        loaded = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(loaded.status_code, 200)
        self.assertIn('1001', loaded.get_json()['Freezes']['frozen_accounts'])

    def test_unfreeze_missing_404(self):
        self.login()
        response = self.client.post('/unfreezeAccount', json={'userid': 'alice', 'freeze_id': 'nope'})
        self.assertEqual(response.status_code, 404)

    def test_employee_freeze_and_get_customer(self):
        self.login('emp1', 'tier2')
        frozen = self.client.post('/freezeAccount', json={
            'userid': 'emp1', 'customer_id': 'alice', 'account': '1001', 'reason': 'fraud',
        })
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(frozen.get_json()['freeze']['reason'], 'fraud')
        looked = self.client.post('/getCustomer', json={'userid': 'emp1', 'customer_id': 'alice'})
        self.assertEqual(looked.get_json()['Freezes']['open_freeze_count'], 1)

        self.login('alice', 'customer')
        blocked = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '1',
        })
        self.assertEqual(blocked.status_code, 403)
        unlock = self.client.post('/unfreezeAccount', json={
            'userid': 'alice', 'freeze_id': frozen.get_json()['freeze']['freeze_id'],
        })
        self.assertEqual(unlock.status_code, 403)
        self.assertEqual(unlock.get_json()['error'], 'freeze_locked')

    def test_employee_approve_blocked_on_frozen_from_account(self):
        self.login('alice')
        self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        self.login('emp1', 'tier1')
        blocked = self.client.post('/approveRequestEmp', json={
            'userid': 'emp1', 'transaction_no': '9', 'from_account': '1001',
        })
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.executed, [])

    def test_unowned_account_forbidden(self):
        self.login()
        response = self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '9999'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'freeze_forbidden')

    def test_duplicate_freeze_409(self):
        self.login()
        self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        again = self.client.post('/freezeAccount', json={'userid': 'alice', 'account': '1001'})
        self.assertEqual(again.status_code, 409)

    def test_own_accounts_helper_used_in_payload(self):
        payload = {'savings': {'Account': 1001, 'Balance': 1}, 'checkin': 'None', 'credit': 'None'}
        self.assertEqual(own_accounts_from_customer_payload(payload), ['1001'])

    def test_app_wiring_scan(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        with open(os.path.join(root, 'app.py')) as handle:
            app_src = handle.read()
        self.assertIn('enforce_freeze', app_src)
        self.assertIn('enforce_stop', app_src)
        self.assertIn('attach_freeze_routes', app_src)
        self.assertIn("'Freezes': freeze_service.snapshot", app_src)
        with open(os.path.join(root, 'static/main_js/customer.js')) as handle:
            js = handle.read()
        self.assertIn('freezeAccount', js)
        self.assertIn('stopPayment', js)
        with open(os.path.join(root, 'templates/customer.html')) as handle:
            html = handle.read()
        self.assertIn('account_controls_pane', html)
        self.assertIn('account_controls_menu', html)


if __name__ == '__main__':
    unittest.main()
