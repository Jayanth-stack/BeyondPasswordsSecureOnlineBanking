import unittest

from flask import Flask, jsonify, request, session

from utility.dispute import (
    DisputePolicy,
    DisputeService,
    MemoryDisputeStore,
    attach_dispute_routes,
)


class Ledger:
    def __init__(self):
        self.credits = []
        self.debits = []

    def credit(self, account, amount, remark):
        self.credits.append((account, amount, remark))
        return True

    def debit(self, account, amount, remark):
        self.debits.append((account, amount, remark))
        return True


def build_app(service):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['1001', '1002']

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Disputes': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Disputes': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/getTransactionHistory', methods=['POST'])
    def history():
        return jsonify({'transactions': [['$40.00 debited']]}), 200

    attach_dispute_routes(app, service, own_accounts_loader=_own)
    return app


class DisputeRouteTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.now = [1_700_000_000.0]
        self.service = DisputeService(
            DisputePolicy(),
            MemoryDisputeStore(),
            clock=lambda: self.now[0],
            credit_fn=self.ledger.credit,
            debit_fn=self.ledger.debit,
        )
        self.service.observe(
            '1001', '40.00', 'withdraw', direction='debit',
            userid='alice', source_id='wd:1001:40',
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_open_401(self):
        response = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/openDispute', json={'userid': 'bob', 'source_id': 'wd:1001:40'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_files_then_staff_credits_and_history_unchanged(self):
        self.login()
        opened = self.client.post('/openDispute', json={
            'userid': 'alice',
            'source_id': 'wd:1001:40',
            'reason': 'unauthorized',
            'evidence': 'atm skimmer',
        })
        self.assertEqual(opened.status_code, 201)
        body = opened.get_json()
        self.assertEqual(body['dispute']['status'], 'open')
        dispute_id = body['dispute']['dispute_id']

        listed = self.client.post('/listDisputes', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Disputes']['open_count'], 1)

        challengeable = self.client.post('/listChallengeable', json={'userid': 'alice'})
        self.assertEqual(challengeable.status_code, 200)
        self.assertEqual(challengeable.get_json()['challengeable'], [])

        dash = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['Disputes']['open_count'], 1)

        history = self.client.post('/getTransactionHistory', json={'userid': 'alice', 'account_no': '1001'})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()['transactions'][0][0], '$40.00 debited')

        self.login('teller', 'tier1')
        investigated = self.client.post('/investigateDispute', json={
            'userid': 'teller', 'customer_id': 'alice', 'dispute_id': dispute_id,
        })
        self.assertEqual(investigated.status_code, 200)
        self.assertEqual(investigated.get_json()['dispute']['status'], 'investigating')

        credited = self.client.post('/grantProvisionalCredit', json={
            'userid': 'teller', 'customer_id': 'alice', 'dispute_id': dispute_id,
        })
        self.assertEqual(credited.status_code, 200)
        self.assertEqual(credited.get_json()['dispute']['credit_status'], 'provisional')
        self.assertEqual(len(self.ledger.credits), 1)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Disputes']['provisional_total'], '40.00')

        self.login('mgr', 'tier2')
        upheld = self.client.post('/decideDispute', json={
            'userid': 'mgr', 'customer_id': 'alice', 'dispute_id': dispute_id,
            'decision': 'uphold',
        })
        self.assertEqual(upheld.status_code, 200)
        self.assertEqual(upheld.get_json()['dispute']['status'], 'upheld')
        self.assertEqual(self.ledger.debits, [])

    def test_customer_cannot_grant_provisional(self):
        self.login()
        opened = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        dispute_id = opened.get_json()['dispute']['dispute_id']
        denied = self.client.post('/grantProvisionalCredit', json={
            'userid': 'alice', 'dispute_id': dispute_id,
        })
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error'], 'dispute_forbidden')
        self.assertEqual(self.ledger.credits, [])

    def test_staff_deny_claws_back(self):
        self.login()
        opened = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        dispute_id = opened.get_json()['dispute']['dispute_id']
        self.login('teller', 'tier1')
        self.client.post('/grantProvisionalCredit', json={
            'userid': 'teller', 'customer_id': 'alice', 'dispute_id': dispute_id,
        })
        denied = self.client.post('/decideDispute', json={
            'userid': 'teller', 'customer_id': 'alice', 'dispute_id': dispute_id,
            'decision': 'deny',
        })
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(denied.get_json()['dispute']['status'], 'denied')
        self.assertEqual(denied.get_json()['dispute']['credit_status'], 'clawed')
        self.assertEqual(len(self.ledger.debits), 1)

    def test_missing_movement_404(self):
        self.login()
        response = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'nope'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'movement_not_found')

    def test_duplicate_409(self):
        self.login()
        first = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        self.assertEqual(first.status_code, 201)
        second = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()['error'], 'dispute_duplicate')

    def test_staff_missing_customer_id(self):
        self.login('teller', 'tier1')
        response = self.client.post('/openDispute', json={
            'userid': 'teller', 'source_id': 'wd:1001:40',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'missing_customer_id')

    def test_staff_files_on_behalf(self):
        self.login('teller', 'tier1')
        opened = self.client.post('/openDispute', json={
            'userid': 'teller', 'customer_id': 'alice', 'source_id': 'wd:1001:40',
            'reason': 'duplicate',
        })
        self.assertEqual(opened.status_code, 201)
        self.assertEqual(opened.get_json()['dispute']['userid'], 'alice')
        self.assertEqual(opened.get_json()['dispute']['actor_type'], 'tier1')

    def test_withdraw_then_reload(self):
        self.login()
        opened = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        dispute_id = opened.get_json()['dispute']['dispute_id']
        withdrawn = self.client.post('/withdrawDispute', json={'userid': 'alice', 'dispute_id': dispute_id})
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(withdrawn.get_json()['dispute']['status'], 'withdrawn')
        again = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        self.assertEqual(again.status_code, 201)

    def test_invalid_reason_400(self):
        self.login()
        response = self.client.post('/openDispute', json={
            'userid': 'alice', 'source_id': 'wd:1001:40', 'reason': 'vibes',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_reason')

    def test_get_dispute_other_customer_403(self):
        self.login()
        opened = self.client.post('/openDispute', json={'userid': 'alice', 'source_id': 'wd:1001:40'})
        dispute_id = opened.get_json()['dispute']['dispute_id']
        self.login('bob', 'customer')
        response = self.client.post('/getDispute', json={'userid': 'bob', 'dispute_id': dispute_id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'dispute_forbidden')


if __name__ == '__main__':
    unittest.main()
