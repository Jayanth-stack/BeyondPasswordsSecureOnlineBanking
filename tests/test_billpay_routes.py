import unittest

from flask import Flask, jsonify, request, session

from utility.billpay import (
    BillPayPolicy,
    BillPayService,
    MemoryBillPayStore,
    attach_billpay_routes,
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
            'BillPay': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'BillPay': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    attach_billpay_routes(app, service)
    return app


class BillPayRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = BillPayService(
            BillPayPolicy(),
            MemoryBillPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
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
        response = self.client.post('/addBiller', json={
            'userid': 'alice', 'nickname': 'Power', 'default_from_account': '1001',
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addBiller', json={
            'userid': 'bob', 'nickname': 'Power', 'default_from_account': '1001',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_return(self):
        self.login()
        added = self.client.post('/addBiller', json={
            'userid': 'alice', 'nickname': 'Power', 'default_from_account': '1001', 'category': 'utility',
        })
        self.assertEqual(added.status_code, 201)
        biller_id = added.get_json()['biller']['biller_id']
        paid = self.client.post('/payBill', json={
            'userid': 'alice', 'biller_id': biller_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(paid.status_code, 201)
        payment_id = paid.get_json()['payment']['payment_id']
        returned = self.client.post('/returnOutboundAch', json={
            'userid': 'alice', 'payment_id': payment_id,
        })
        self.assertEqual(returned.status_code, 403)
        self.assertEqual(returned.get_json()['error'], 'billpay_forbidden')

    def test_staff_return_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addBiller', json={
            'userid': 'alice',
            'nickname': 'Landlord',
            'default_from_account': '1001',
            'category': 'rent',
        })
        self.assertEqual(added.status_code, 201)
        biller_id = added.get_json()['biller']['biller_id']

        paid = self.client.post('/payBill', json={
            'userid': 'alice', 'biller_id': biller_id, 'amount': '400.00', 'trace_id': 'rent-1',
        })
        self.assertEqual(paid.status_code, 201)
        payment = paid.get_json()['payment']
        self.assertEqual(payment['status'], 'sent')
        self.assertEqual(payment['amount'], '400.00')

        listed = self.client.post('/listBillers', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['BillPay']['ytd'], '400.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['BillPay']['billers'][0]['nickname'], 'Landlord')

        returned = self.client.post('/returnOutboundAch', json={
            'userid': 'teller', 'payment_id': payment['payment_id'], 'reason': 'unauthorized',
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['payment']['status'], 'returned')
        self.assertEqual(self.credits, [('1001', '400.00', 'bill pay returned from Landlord')])

        transfer = self.client.post('/fundTransfer', json={
            'userid': 'teller', 'fromAccount': '1001', 'toAccount': '1002', 'amount': '10',
        })
        self.assertEqual(transfer.status_code, 200)
        self.assertIn('approved', transfer.get_json()['message'])

        withdraw = self.client.post('/withdrawAmount', json={'userid': 'teller', 'account': '1001', 'amount': '10'})
        self.assertEqual(withdraw.status_code, 200)
        self.assertEqual(withdraw.get_json()['message'], 'Amount Debited')

        self.login('alice', 'customer')
        dash = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['BillPay']['ytd'], '0.00')
        self.assertEqual(dash.get_json()['BillPay']['returned_ytd'], '400.00')

        dup = self.client.post('/payBill', json={
            'userid': 'alice', 'biller_id': biller_id, 'amount': '400.00', 'trace_id': 'rent-1',
        })
        self.assertEqual(dup.status_code, 200)
        self.assertEqual(dup.get_json()['payment']['payment_id'], payment['payment_id'])
        self.assertEqual(len(self.debits), 1)

    def test_missing_biller_404(self):
        self.login()
        response = self.client.post('/payBill', json={
            'userid': 'alice', 'biller_id': 'nope', 'amount': '10',
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'biller_not_found')

    def test_invalid_amount_400(self):
        self.login()
        added = self.client.post('/addBiller', json={
            'userid': 'alice', 'nickname': 'Power', 'default_from_account': '1001',
        })
        biller_id = added.get_json()['biller']['biller_id']
        response = self.client.post('/payBill', json={
            'userid': 'alice', 'biller_id': biller_id, 'amount': '-5',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_amount')


if __name__ == '__main__':
    unittest.main()
