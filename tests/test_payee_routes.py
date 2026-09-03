"""Feature-path tests for payee allowlist on money-moving routes.

Does not import app.py (MySQL connects at import). Mounts the same
enforce_payee / attach_payee_routes helpers the real routes use onto a tiny
Flask app.
"""

import os
import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from utility.payee import (
    MemoryPayeeStore,
    PayeePolicy,
    PayeeService,
    attach_payee_routes,
    enforce_payee,
    owned_account_numbers,
    payee_snapshot,
    set_owned_resolver,
    set_service,
)


ALICE_ACCOUNTS = {
    'checkin': {'Account': 1001, 'Balance': 900},
    'savings': {'Account': 1002, 'Balance': 400},
    'credit': 'None',
}


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _denied(destination, operation):
        owned = owned_account_numbers(ALICE_ACCOUNTS) if session.get('userid') == 'alice' else set()
        result = enforce_payee(session, destination, owned_accounts=owned, operation=operation)
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
        denied = _denied(values['toAccount'], 'transfer')
        if denied:
            return denied
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json()
        denied = _denied(values['to_account'], 'cheque')
        if denied:
            return denied
        return jsonify({'message': 'Success'}), 200

    @app.route('/requestFunds', methods=['POST'])
    def request_funds():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'message': 'Request Sent'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/loadCustomer', methods=['GET', 'POST'])
    def load_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        return jsonify({
            'Accounts': ALICE_ACCOUNTS,
            'Payees': payee_snapshot(session),
        }), 200

    attach_payee_routes(app)
    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class PayeeRouteTests(unittest.TestCase):
    def setUp(self):
        self.svc = PayeeService(store=MemoryPayeeStore(), policy=PayeePolicy(cooling_seconds=0))
        set_service(self.svc)
        set_owned_resolver(lambda userid: {'1001', '1002'} if userid == 'alice' else set())
        self.app = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        set_service(None)
        set_owned_resolver(None)

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_transfer_to_unknown_is_403(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '40.00',
        })
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()['error'], 'payee_not_registered')

    def test_add_payee_then_transfer(self):
        self._login()
        added = self.client.post('/addPayee', json={
            'userid': 'alice', 'account': 2001, 'nickname': 'Landlord',
        })
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.get_json()['payee']['account'], '2001')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '40.00',
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn('approved', res.get_json()['message'])

    def test_internal_transfer_does_not_need_payee(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 1002, 'amount': '10.00',
        })
        self.assertEqual(res.status_code, 200)

    def test_teller_skips_allowlist(self):
        self._login('teller1', 'tier1')
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 9999, 'amount': '300.00',
        })
        self.assertEqual(res.status_code, 200)

    def test_cheque_is_gated_deposit_and_request_are_not(self):
        self._login()
        cheque = self.client.post('/getCashierCheque', json={
            'from_account': 1001, 'to_account': 2001, 'amount': '70.00',
        })
        self.assertEqual(cheque.status_code, 403)
        self.assertEqual(self.client.post('/depositAmount', json={'account': 1001, 'amount': '5'}).status_code, 200)
        self.assertEqual(self.client.post('/requestFunds', json={
            'fromAccount': 2001, 'toAccount': 1001, 'amount': '5',
        }).status_code, 200)
        self.assertEqual(self.client.post('/withdrawAmount', json={'account': 1001, 'amount': '5'}).status_code, 200)

    def test_cooling_sets_retry_after(self):
        set_service(PayeeService(
            store=MemoryPayeeStore(),
            policy=PayeePolicy(cooling_seconds=120),
        ))
        self._login()
        self.client.post('/addPayee', json={'userid': 'alice', 'account': 2001, 'nickname': 'Rent'})
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '10.00',
        })
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()['error'], 'payee_cooling')
        self.assertTrue(res.headers.get('Retry-After'))

    def test_invalid_destination_is_400(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 'nope', 'amount': '10.00',
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['error'], 'invalid_account')

    def test_unauthenticated_rejected_before_allowlist(self):
        res = self.client.post('/fundTransfer', json={
            'fromAccount': 1001, 'toAccount': 2001, 'amount': '10.00',
        })
        self.assertEqual(res.status_code, 401)

    def test_add_list_remove_auth(self):
        self._login()
        added = self.client.post('/addPayee', json={
            'userid': 'alice', 'account': '2001', 'nickname': 'Rent',
        })
        self.assertEqual(added.status_code, 200)
        listed = self.client.post('/listPayees', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['count'], 1)
        payee_id = added.get_json()['payee']['payee_id']
        removed = self.client.post('/removePayee', json={'userid': 'alice', 'payee_id': payee_id})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.post('/listPayees', json={'userid': 'alice'}).get_json()['count'], 0)

    def test_employee_cannot_manage_payees(self):
        self._login('teller1', 'tier1')
        res = self.client.post('/addPayee', json={
            'userid': 'teller1', 'account': 2001, 'nickname': 'X',
        })
        self.assertEqual(res.status_code, 403)

    def test_userid_mismatch_on_add(self):
        self._login()
        res = self.client.post('/addPayee', json={
            'userid': 'bob', 'account': 2001, 'nickname': 'X',
        })
        self.assertEqual(res.status_code, 401)

    def test_load_customer_includes_payees(self):
        self._login()
        self.client.post('/addPayee', json={'userid': 'alice', 'account': 2001, 'nickname': 'Rent'})
        res = self.client.post('/loadCustomer', json={})
        self.assertEqual(res.status_code, 200)
        payees = res.get_json()['Payees']
        self.assertTrue(payees['enabled'])
        self.assertEqual(payees['count'], 1)
        self.assertEqual(payees['payees'][0]['nickname'], 'Rent')

    def test_app_py_wires_enforce_and_routes(self):
        root = os.path.join(os.path.dirname(__file__), '..')
        with open(os.path.join(root, 'app.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('enforce_payee', source)
        self.assertIn('attach_payee_routes', source)
        self.assertIn('payee_snapshot', source)
        with open(os.path.join(root, 'static/main_js/customer.js'), encoding='utf-8') as handle:
            js = handle.read()
        self.assertIn('addPayee', js)
        self.assertIn('destinationAllowed', js)


if __name__ == '__main__':
    unittest.main()
