import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.present import (
    MemoryPresentStore,
    PresentPolicy,
    PresentService,
    SettlementClock,
    attach_present_routes,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


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
            'CardPresents': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'CardPresents': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 200

    attach_present_routes(app, service)
    return app


class PresentRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = PresentService(
            PresentPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryPresentStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=SettlementClock(cutoff_hour=22, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_is_401(self):
        response = self.client.post('/addCardPresentKey', json={
            'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._session()
        response = self.client.post('/addCardPresentKey', json={
            'userid': 'bob', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._session()
        added = self.client.post('/addCardPresentKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        ingest = self.client.post('/ingestCardPresent', json={
            'userid': 'alice', 'amount': '10.00', 'merchant': 'Shop', 'pan': VISA,
        })
        self.assertEqual(ingest.status_code, 403)
        self.assertEqual(ingest.get_json()['error'], 'present_forbidden')

    def test_staff_ingest_and_customer_snapshot(self):
        self._session()
        self.client.post('/addCardPresentKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self._session('teller', 'tier1')
        ingest = self.client.post('/ingestCardPresent', json={
            'userid': 'teller', 'amount': '22.00', 'merchant': 'ACME', 'pan': VISA, 'mcc': '5999',
        })
        self.assertEqual(ingest.status_code, 201)
        body = ingest.get_json()
        self.assertEqual(body['presentment']['status'], 'posted')
        self.assertNotIn(VISA, str(body))
        self._session()
        dash = self.client.post('/loadCustomer')
        self.assertEqual(dash.status_code, 200)
        snap = dash.get_json()['CardPresents']
        self.assertEqual(snap['ytd_posted'], '22.00')
        self.assertEqual(snap['presentments'][0]['merchant'], 'ACME')

    def test_existing_money_routes_unchanged(self):
        self._session()
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        withdraw = self.client.post('/withdrawAmount', json={'userid': 'alice'})
        self.assertEqual(withdraw.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.status_code, 200)

    def test_file_ingest_and_export_masks_arn(self):
        self._session()
        self.client.post('/addCardPresentKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self._session('teller', 'tier1')
        ingest = self.client.post('/ingestCardPresent', json={
            'userid': 'teller', 'amount': '9.00', 'merchant': 'Cafe', 'pan': VISA,
        })
        presentment_id = ingest.get_json()['presentment']['presentment_id']
        exported = self.client.post('/exportCardPresent', json={
            'userid': 'teller', 'presentment_id': presentment_id,
        })
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.get_json()['file'].startswith('IPM1|'))
        self.assertIn('****', exported.get_json()['file'])


if __name__ == '__main__':
    unittest.main()
