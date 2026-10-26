import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.acs import (
    AcsClock,
    AcsPolicy,
    AcsService,
    MemoryAcsStore,
    attach_acs_routes,
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
            'Card3ds': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Card3ds': service.snapshot(values.get('customer_id')),
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

    attach_acs_routes(app, service)
    return app


class AcsRouteTests(unittest.TestCase):
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

        self.service = AcsService(
            AcsPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryAcsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': 'None',
            },
            calendar=AcsClock(challenge_seconds=600, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_401_without_session(self):
        response = self.client.post('/addCard3dsKey', json={'nickname': 'Travel', 'pan': VISA, 'default_account': '1001'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch(self):
        self._session()
        response = self.client.post('/addCard3dsKey', json={
            'userid': 'bob', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_enroll_and_staff_ingest(self):
        self._session()
        added = self.client.post('/addCard3dsKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        self.assertNotIn('fingerprint', added.get_json()['key'])
        self.assertNotIn(VISA, str(added.get_json()))
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestCard3ds', json={
            'userid': 'teller',
            'amount': '25.00',
            'merchant': 'ACME',
            'pan': VISA,
            'channel': 'app',
            'method_completed': True,
        })
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['auth']['status'], 'authenticated')
        self.assertEqual(body['auth']['eci'], '05')
        self.assertTrue(body['auth']['cavv_masked'].startswith('*'))
        self.assertNotIn(VISA, str(body))
        self.assertEqual(self.debits, [])

    def test_customer_cannot_ingest(self):
        self._session()
        self.client.post('/addCard3dsKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        response = self.client.post('/ingestCard3ds', json={
            'userid': 'alice', 'amount': '10.00', 'merchant': 'Shop', 'pan': VISA, 'channel': 'app',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'acs_forbidden')

    def test_challenge_respond_and_existing_money_routes(self):
        self._session()
        self.client.post('/addCard3dsKey', json={
            'userid': 'alice', 'nickname': 'Travel', 'pan': VISA, 'default_account': '1001',
        })
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestCard3ds', json={
            'userid': 'teller', 'amount': '20.00', 'merchant': 'Shop', 'pan': VISA,
            'channel': 'brw', 'acs_code': '123456',
        })
        auth_id = ingested.get_json()['auth']['auth_id']
        self._session()
        bad = self.client.post('/respondCard3ds', json={
            'userid': 'alice', 'auth_id': auth_id, 'acs_code': '000001',
        })
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post('/respondCard3ds', json={
            'userid': 'alice', 'auth_id': auth_id, 'acs_code': '123456',
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()['auth']['status'], 'authenticated')
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        withdraw = self.client.post('/withdrawAmount', json={'userid': 'alice'})
        self.assertEqual(withdraw.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.status_code, 200)

    def test_xml_file_rejected(self):
        self._session('teller', 'tier1')
        response = self.client.post('/ingestCard3dsFile', json={
            'userid': 'teller', 'file': '<?xml version="1.0"?><acs/>',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_file')


if __name__ == '__main__':
    unittest.main()
