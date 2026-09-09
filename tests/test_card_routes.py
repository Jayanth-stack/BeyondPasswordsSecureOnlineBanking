import unittest

from flask import Flask, jsonify, request, session

from utility.card import (
    CardPolicy,
    CardService,
    MemoryCardStore,
    attach_card_routes,
    enforce_card,
)


def _hash(pin):
    return 'h:' + pin


def _verify(pin, hashed):
    return hashed == 'h:' + pin


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['9001']

    def _maybe(operation, account, pin=None, pin_token=None, owner_userid=None):
        blocked = enforce_card(
            service,
            operation=operation,
            account=account,
            userid=owner_userid if owner_userid is not None else (
                session.get('userid') if session.get('usertype') == 'customer' else None
            ),
            actor_type=session.get('usertype') or 'customer',
            pin=pin,
            pin_token=pin_token,
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
        gated = _maybe(
            'transfer',
            values.get('fromAccount'),
            pin=values.get('pin'),
            pin_token=values.get('pin_token'),
        )
        if gated is not None:
            return gated
        executed.append(('transfer', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('withdraw', values.get('account'), pin=values.get('pin'))
        if gated is not None:
            return gated
        executed.append(('withdraw', values.get('account'), values.get('amount')))
        return jsonify({'message': 'done'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('cheque', values.get('from_account'), pin=values.get('pin'))
        if gated is not None:
            return gated
        executed.append(('cheque', values.get('from_account'), values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('deposit', values.get('account'))
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
            pin=values.get('pin'),
            owner_userid=values.get('customer_id'),
        )
        if gated is not None:
            return gated
        executed.append(('approve', values.get('from_account')))
        return jsonify({'message': 'done'}), 200

    @app.route('/approveRequestEmp', methods=['POST'])
    def approve_emp():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        gated = _maybe('approve', values.get('from_account'), owner_userid=values.get('customer_id'))
        if gated is not None:
            return gated
        executed.append(('approve_emp', values.get('from_account')))
        return jsonify({'message': 'done'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {
                'savings': {'Account': 1001, 'Balance': 50},
                'checkin': 'None',
                'credit': {'Account': 9001, 'Balance': -10},
            },
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Cards': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {
                'savings': {'Account': 1001, 'Balance': 50},
                'checkin': 'None',
                'credit': {'Account': 9001, 'Balance': -10},
            },
            'Info': {'first_name': 'Ada'},
            'Cards': service.snapshot(values.get('customer_id')),
        }), 200

    attach_card_routes(app, service, own_accounts_loader=_own)
    return app


class CardRouteTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.service = CardService(
            CardPolicy(),
            MemoryCardStore(),
            hash_pin=_hash,
            verify_pin=_verify,
            is_credit_loader=lambda account, userid: str(account) in {'9001', '9001.0'},
        )
        self.app = build_app(self.service, self.executed)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_set_pin_401(self):
        response = self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/setPin', json={'userid': 'bob', 'account': '9001', 'pin': '1234'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_set_pin_then_transfer_with_pin(self):
        self.login()
        created = self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.get_json()['card']['pin_set'])

        blocked = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001', 'amount': '25',
        })
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['error'], 'pin_required')
        self.assertEqual(self.executed, [])

        ok = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001',
            'amount': '25', 'pin': '1234',
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.executed[-1][0], 'transfer')

    def test_savings_transfer_unaffected(self):
        self.login()
        ok = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10',
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.executed[-1], ('transfer', '1001', '10'))

    def test_lock_blocks_withdraw_and_cheque(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        locked = self.client.post('/lockCard', json={
            'userid': 'alice', 'account': '9001', 'reason': 'lost',
        })
        self.assertEqual(locked.status_code, 200)

        withdraw = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '9001', 'amount': '20', 'pin': '1234',
        })
        self.assertEqual(withdraw.status_code, 403)
        self.assertEqual(withdraw.get_json()['error'], 'card_locked')

        cheque = self.client.post('/getCashierCheque', json={
            'userid': 'alice', 'from_account': '9001', 'to_account': '2002',
            'amount': '20', 'pin': '1234',
        })
        self.assertEqual(cheque.status_code, 403)

        deposit = self.client.post('/depositAmount', json={
            'userid': 'alice', 'account': '9001', 'amount': '50',
        })
        self.assertEqual(deposit.status_code, 200)

    def test_unlock_with_pin_then_charge(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        self.client.post('/lockCard', json={'userid': 'alice', 'account': '9001', 'reason': 'stolen'})
        bad = self.client.post('/unlockCard', json={
            'userid': 'alice', 'account': '9001', 'pin': '0000',
        })
        self.assertEqual(bad.status_code, 403)
        ok = self.client.post('/unlockCard', json={
            'userid': 'alice', 'account': '9001', 'pin': '1234',
        })
        self.assertEqual(ok.status_code, 200)
        charge = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001',
            'amount': '5', 'pin': '1234',
        })
        self.assertEqual(charge.status_code, 200)

    def test_verify_pin_token_flow(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        verified = self.client.post('/verifyPin', json={
            'userid': 'alice', 'account': '9001', 'pin': '1234',
        })
        self.assertEqual(verified.status_code, 200)
        token = verified.get_json()['pin_token']
        ok = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001',
            'amount': '5', 'pin_token': token,
        })
        self.assertEqual(ok.status_code, 200)

    def test_employee_fraud_lock_and_reset(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        self.login('emp1', 'tier2')
        locked = self.client.post('/lockCard', json={
            'userid': 'emp1', 'customer_id': 'alice', 'account': '9001', 'reason': 'fraud',
        })
        self.assertEqual(locked.status_code, 200)
        self.login()
        unlock = self.client.post('/unlockCard', json={
            'userid': 'alice', 'account': '9001', 'pin': '1234',
        })
        self.assertEqual(unlock.status_code, 403)
        self.assertEqual(unlock.get_json()['error'], 'card_lock_locked')

        self.login('emp1', 'tier2')
        unlocked = self.client.post('/unlockCard', json={
            'userid': 'emp1', 'customer_id': 'alice', 'account': '9001',
        })
        self.assertEqual(unlocked.status_code, 200)
        reset = self.client.post('/resetPin', json={
            'userid': 'emp1', 'customer_id': 'alice', 'account': '9001',
        })
        self.assertEqual(reset.status_code, 200)
        self.login()
        blocked = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '9001', 'toAccount': '1001',
            'amount': '5', 'pin': '1234',
        })
        self.assertEqual(blocked.get_json()['error'], 'pin_not_set')

    def test_employee_approve_skips_pin_not_lock(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        self.login('emp1', 'tier1')
        ok = self.client.post('/approveRequestEmp', json={
            'userid': 'emp1', 'from_account': '9001', 'customer_id': 'alice',
        })
        self.assertEqual(ok.status_code, 200)
        self.login()
        self.client.post('/lockCard', json={'userid': 'alice', 'account': '9001', 'reason': 'lost'})
        self.login('emp1', 'tier1')
        blocked = self.client.post('/approveRequestEmp', json={
            'userid': 'emp1', 'from_account': '9001', 'customer_id': 'alice',
        })
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['error'], 'card_locked')

    def test_load_customer_snapshot(self):
        self.login()
        self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '1234'})
        loaded = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(loaded.status_code, 200)
        cards = loaded.get_json()['Cards']
        self.assertTrue(cards['enabled'])
        self.assertEqual(cards['pin_set_accounts'], ['9001'])

    def test_invalid_pin_400(self):
        self.login()
        response = self.client.post('/setPin', json={'userid': 'alice', 'account': '9001', 'pin': '12'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_pin')

    def test_unowned_account_forbidden(self):
        self.login()
        response = self.client.post('/setPin', json={
            'userid': 'alice', 'account': '9999', 'pin': '1234', 'own_accounts': ['9001'],
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'card_forbidden')


if __name__ == '__main__':
    unittest.main()
