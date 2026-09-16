import unittest

from flask import Flask, jsonify, request, session

from utility.tax import (
    MemoryTaxStore,
    TaxPolicy,
    TaxService,
    attach_tax_routes,
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
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'TaxForms': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'TaxForms': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/getTransactionHistory', methods=['POST'])
    def history():
        return jsonify({'transactions': [['$40.00 direct deposited']]}), 200

    attach_tax_routes(app, service)
    return app


class TaxRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]  # 2024-06-15
        self.service = TaxService(
            TaxPolicy(),
            MemoryTaxStore(),
            clock=lambda: self.now[0],
            recipient_fn=lambda userid: {'name': 'Ada Lovelace', 'tin_last4': '4321'},
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_generate_401(self):
        response = self.client.post('/generateTaxForm', json={'userid': 'alice', 'tax_year': 2023})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/generateTaxForm', json={'userid': 'bob', 'tax_year': 2023})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_post_reportable(self):
        self.login()
        response = self.client.post('/postReportable', json={
            'userid': 'alice', 'account': '1001', 'amount': '11.00', 'box': 'interest',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'tax_forbidden')

    def test_staff_post_generate_file_and_history_unchanged(self):
        self.login('teller', 'tier1')
        posted = self.client.post('/postReportable', json={
            'userid': 'teller',
            'customer_id': 'alice',
            'account': '1001',
            'amount': '25.00',
            'box': 'interest',
            'tax_year': 2023,
        })
        self.assertEqual(posted.status_code, 201)
        self.assertEqual(posted.get_json()['entry']['box'], 'interest')

        generated = self.client.post('/generateTaxForm', json={
            'userid': 'teller',
            'customer_id': 'alice',
            'tax_year': 2023,
        })
        self.assertEqual(generated.status_code, 201)
        form = generated.get_json()['form']
        self.assertEqual(form['box1'], '25.00')
        self.assertEqual(form['status'], 'issued')
        self.assertTrue(form['required'])
        form_id = form['form_id']

        listed = self.client.post('/listTaxForms', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.get_json()['TaxForms']['forms']), 1)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['TaxForms']['forms'][0]['box1'], '25.00')

        history = self.client.post('/getTransactionHistory', json={'userid': 'teller', 'account_no': '1001'})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()['transactions'][0][0], '$40.00 direct deposited')

        self.login('alice', 'customer')
        dash = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['TaxForms']['forms'][0]['form_id'], form_id)

        copy = self.client.post('/requestTaxCopy', json={
            'userid': 'alice', 'form_id': form_id, 'channel': 'mail',
        })
        self.assertEqual(copy.status_code, 201)
        request_id = copy.get_json()['request']['request_id']

        fetched = self.client.post('/getTaxForm', json={'userid': 'alice', 'form_id': form_id})
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.get_json()['form']['recipient']['tin_last4'], '4321')

        self.login('mgr', 'tier2')
        filed = self.client.post('/fileTaxForm', json={
            'userid': 'mgr', 'customer_id': 'alice', 'form_id': form_id,
        })
        self.assertEqual(filed.status_code, 200)
        self.assertEqual(filed.get_json()['form']['status'], 'filed')

        fulfilled = self.client.post('/decideTaxCopy', json={
            'userid': 'mgr', 'customer_id': 'alice', 'request_id': request_id, 'decision': 'fulfill',
        })
        self.assertEqual(fulfilled.status_code, 200)
        self.assertEqual(fulfilled.get_json()['request']['status'], 'fulfilled')

        again = self.client.post('/generateTaxForm', json={
            'userid': 'mgr', 'customer_id': 'alice', 'tax_year': 2023, 'force': True,
        })
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()['error'], 'already_filed')

    def test_missing_form_404(self):
        self.login()
        response = self.client.post('/getTaxForm', json={'userid': 'alice', 'form_id': 'nope'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'form_not_found')

    def test_invalid_year_400(self):
        self.login('teller', 'tier1')
        response = self.client.post('/generateTaxForm', json={
            'userid': 'teller', 'customer_id': 'alice', 'tax_year': 1999,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_year')

    def test_observe_interest_feeds_ytd_without_touching_history(self):
        self.service.observe(
            '1001', '18.00', 'interest', direction='credit',
            userid='alice', description='interest credited for 2024-01',
            source_id='int:1001:2024-01',
        )
        self.login()
        dash = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['TaxForms']['box1'], '18.00')
        history = self.client.post('/getTransactionHistory', json={'userid': 'alice', 'account_no': '1001'})
        self.assertEqual(history.get_json()['transactions'][0][0], '$40.00 direct deposited')


if __name__ == '__main__':
    unittest.main()
