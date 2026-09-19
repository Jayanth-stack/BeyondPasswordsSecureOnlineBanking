import unittest
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.link import (
    LinkPolicy,
    LinkService,
    MemoryLinkStore,
    attach_link_routes,
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
            'LinkedAccounts': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'LinkedAccounts': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    attach_link_routes(app, service)
    return app


class LinkRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.debits = []
        self.credits = []
        self.amounts = [(Decimal('0.12'), Decimal('0.47'))]

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = LinkService(
            LinkPolicy(
                challenge_secret='unit-secret',
                prenote_wait_seconds=86400,
                pending_ttl_seconds=86400,
            ),
            MemoryLinkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            amount_fn=lambda: self.amounts[0],
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'Chase', 'default_account': '1001',
            'routing_last4': '0210', 'account_last4': '7788',
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addLinkedAccount', json={
            'userid': 'bob', 'nickname': 'Chase', 'default_account': '1001',
            'routing_last4': '0210', 'account_last4': '7788',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_return_or_force_verify(self):
        self.login()
        added = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'Chase', 'default_account': '1001',
            'routing_last4': '0210', 'account_last4': '7788',
        })
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('challenge_digest', body['link'])
        link_id = body['link']['link_id']
        confirmed = self.client.post('/confirmLinkedAccount', json={
            'userid': 'alice', 'link_id': link_id, 'amount1': '0.12', 'amount2': '0.47',
        })
        self.assertEqual(confirmed.status_code, 200)
        pushed = self.client.post('/pushToLinked', json={
            'userid': 'alice', 'link_id': link_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(pushed.status_code, 201)
        movement_id = pushed.get_json()['movement']['movement_id']
        returned = self.client.post('/returnLinkedAch', json={
            'userid': 'alice', 'movement_id': movement_id,
        })
        self.assertEqual(returned.status_code, 403)
        self.assertEqual(returned.get_json()['error'], 'link_forbidden')
        pending = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'CapOne', 'default_account': '1001',
            'routing_last4': '0310', 'account_last4': '1122',
        })
        self.assertEqual(pending.status_code, 201)
        forced = self.client.post('/forceVerifyLinkedAccount', json={
            'userid': 'alice', 'link_id': pending.get_json()['link']['link_id'],
        })
        self.assertEqual(forced.status_code, 403)
        self.assertEqual(forced.get_json()['error'], 'link_forbidden')

    def test_staff_return_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addLinkedAccount', json={
            'userid': 'alice',
            'nickname': 'Ally',
            'default_account': '1001',
            'routing_last4': '0110',
            'account_last4': '3344',
        })
        self.assertEqual(added.status_code, 201)
        link_id = added.get_json()['link']['link_id']
        confirmed = self.client.post('/confirmLinkedAccount', json={
            'userid': 'alice', 'link_id': link_id, 'amount1': '0.47', 'amount2': '0.12',
        })
        self.assertEqual(confirmed.status_code, 200)

        pulled = self.client.post('/pullFromLinked', json={
            'userid': 'alice', 'link_id': link_id, 'amount': '25.00', 'trace_id': 'in-1',
        })
        self.assertEqual(pulled.status_code, 201)
        movement = pulled.get_json()['movement']
        self.assertEqual(movement['status'], 'sent')
        self.assertEqual(movement['amount'], '25.00')

        listed = self.client.post('/listLinkedAccounts', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['LinkedAccounts']['ytd_pull'], '25.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['LinkedAccounts']['links'][0]['nickname'], 'Ally')

        returned = self.client.post('/returnLinkedAch', json={
            'userid': 'teller', 'movement_id': movement['movement_id'], 'reason': 'unauthorized',
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['movement']['status'], 'returned')

        transfer = self.client.post('/fundTransfer', json={
            'userid': 'teller', 'fromAccount': '1001', 'toAccount': '1002', 'amount': '1',
        })
        self.assertEqual(transfer.status_code, 200)
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        withdraw = self.client.post('/withdrawAmount', json={
            'userid': 'teller', 'account': '1001', 'amount': '1',
        })
        self.assertEqual(withdraw.status_code, 200)
        self.assertEqual(withdraw.get_json()['message'], 'Amount Debited')

    def test_missing_last4_400_and_wrong_amounts_400(self):
        self.login()
        missing = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'Chase', 'default_account': '1001',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_last4')
        added = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'Chase', 'default_account': '1001',
            'routing_last4': '0210', 'account_last4': '7788',
        })
        self.assertEqual(added.status_code, 201)
        wrong = self.client.post('/confirmLinkedAccount', json={
            'userid': 'alice',
            'link_id': added.get_json()['link']['link_id'],
            'amount1': '0.01',
            'amount2': '0.02',
        })
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.get_json()['error'], 'amounts_incorrect')

    def test_staff_reject_prenote_customer_cannot(self):
        self.login()
        added = self.client.post('/addLinkedAccount', json={
            'userid': 'alice', 'nickname': 'Wells', 'default_account': '1001',
            'routing_last4': '1210', 'account_last4': '9001', 'method': 'prenote',
        })
        self.assertEqual(added.status_code, 201)
        link_id = added.get_json()['link']['link_id']
        denied = self.client.post('/rejectPrenote', json={'userid': 'alice', 'link_id': link_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectPrenote', json={'userid': 'teller', 'link_id': link_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['link']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
