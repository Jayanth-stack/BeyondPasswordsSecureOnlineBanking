import unittest

from flask import Flask, jsonify, request, session

from utility.ach import (
    AchPolicy,
    AchService,
    MemoryAchStore,
    attach_ach_routes,
)


def build_app(service):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'DirectDeposit': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'DirectDeposit': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/getTransactionHistory', methods=['POST'])
    def history():
        return jsonify({'transactions': [['$40.00 direct deposited']]}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    attach_ach_routes(app, service)
    return app


class AchRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.credits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = AchService(
            AchPolicy(),
            MemoryAchStore(),
            clock=lambda: self.now[0],
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addAchSource', json={
            'userid': 'alice', 'nickname': 'Work', 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addAchSource', json={
            'userid': 'bob', 'nickname': 'Work', 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_post_inbound(self):
        self.login()
        added = self.client.post('/addAchSource', json={
            'userid': 'alice', 'nickname': 'Work', 'default_account': '1001', 'company_id': 'ACME',
        })
        self.assertEqual(added.status_code, 201)
        source_id = added.get_json()['source']['source_id']
        posted = self.client.post('/postInboundAch', json={
            'userid': 'alice', 'source_id': source_id, 'amount': '100.00',
        })
        self.assertEqual(posted.status_code, 403)
        self.assertEqual(posted.get_json()['error'], 'ach_forbidden')

    def test_staff_post_split_and_history_unchanged(self):
        self.login()
        added = self.client.post('/addAchSource', json={
            'userid': 'alice',
            'nickname': 'Payroll',
            'default_account': '1001',
            'company_id': 'ACME',
        })
        self.assertEqual(added.status_code, 201)
        source_id = added.get_json()['source']['source_id']

        allocated = self.client.post('/setAchAllocation', json={
            'userid': 'alice',
            'source_id': source_id,
            'legs': [{'account': '1002', 'kind': 'percent', 'value': '25'}],
        })
        self.assertEqual(allocated.status_code, 200)

        preview = self.client.post('/previewAchAllocation', json={
            'userid': 'alice', 'source_id': source_id, 'amount': '400.00',
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            {row['account']: row['amount'] for row in preview.get_json()['splits']},
            {'1002': '100.00', '1001': '300.00'},
        )

        self.login('teller', 'tier1')
        posted = self.client.post('/postInboundAch', json={
            'userid': 'teller',
            'customer_id': 'alice',
            'source_id': source_id,
            'amount': '400.00',
            'trace_id': 'pay-1',
        })
        self.assertEqual(posted.status_code, 201)
        inbound = posted.get_json()['inbound']
        self.assertEqual(inbound['status'], 'posted')
        self.assertEqual(inbound['splits'][0]['amount'], '100.00')

        listed = self.client.post('/listAchSources', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['DirectDeposit']['ytd'], '400.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['DirectDeposit']['sources'][0]['nickname'], 'Payroll')

        history = self.client.post('/getTransactionHistory', json={'userid': 'teller', 'account_no': '1001'})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()['transactions'][0][0], '$40.00 direct deposited')

        cash = self.client.post('/depositAmount', json={'userid': 'teller', 'account': '1001', 'amount': '40'})
        self.assertEqual(cash.status_code, 200)
        self.assertIn('approved', cash.get_json()['message'])

        self.login('alice', 'customer')
        dash = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['DirectDeposit']['ytd'], '400.00')

        again = self.client.post('/postInboundAch', json={
            'userid': 'alice', 'source_id': source_id, 'amount': '400.00', 'trace_id': 'pay-1',
        })
        self.assertEqual(again.status_code, 403)

        self.login('teller', 'tier1')
        dup = self.client.post('/postInboundAch', json={
            'userid': 'teller',
            'customer_id': 'alice',
            'source_id': source_id,
            'amount': '400.00',
            'trace_id': 'pay-1',
        })
        self.assertEqual(dup.status_code, 200)
        self.assertEqual(dup.get_json()['inbound']['inbound_id'], inbound['inbound_id'])
        self.assertEqual(len(self.credits), 2)

    def test_missing_source_404(self):
        self.login()
        response = self.client.post('/setAchAllocation', json={
            'userid': 'alice', 'source_id': 'nope', 'legs': [],
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'source_not_found')

    def test_invalid_percent_400(self):
        self.login()
        added = self.client.post('/addAchSource', json={
            'userid': 'alice', 'nickname': 'Work', 'default_account': '1001',
        })
        source_id = added.get_json()['source']['source_id']
        response = self.client.post('/setAchAllocation', json={
            'userid': 'alice',
            'source_id': source_id,
            'legs': [{'account': '1002', 'kind': 'percent', 'value': '150'}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_percent')


if __name__ == '__main__':
    unittest.main()
