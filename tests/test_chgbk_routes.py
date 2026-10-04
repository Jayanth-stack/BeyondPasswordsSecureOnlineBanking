import unittest
from datetime import datetime, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.chgbk import (
    ChgbkPolicy,
    ChgbkService,
    MemoryChgbkStore,
    attach_chgbk_routes,
    compose_arn,
    compose_vcr_file,
    message_from_values,
)


def ts(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


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
            'Chargebacks': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Chargebacks': service.snapshot(values.get('customer_id')),
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

    attach_chgbk_routes(app, service)
    return app


class ChgbkRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Amount Credited'

        self.service = ChgbkService(
            ChgbkPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryChgbkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            directory_fn=lambda account: 'alice' if str(account) == '1001' else None,
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _payload(self, **overrides):
        body = {
            'userid': 'teller',
            'customer_id': 'alice',
            'network': 'visa',
            'reason': '13.1',
            'amount': '25.00',
            'ica': '400000',
            'card_bin': '411111',
            'card_last4': '1111',
            'merchant_account': '1001',
            'cardholder': 'Jane Cardholder',
            'merchant': 'Acme Store',
            'chargeback_date': '2024-06-14',
            'sequence': 1,
        }
        body.update(overrides)
        return body

    def test_unauthenticated_ingest_401(self):
        response = self.client.post('/ingestChgbk', json=self._payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login('teller', 'tier1')
        response = self.client.post('/ingestChgbk', json=self._payload(userid='alice'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest_or_release(self):
        self.login()
        response = self.client.post('/ingestChgbk', json={
            'userid': 'alice', 'network': 'visa', 'reason': '13.1', 'amount': '25.00',
            'ica': '400000', 'card_bin': '411111', 'card_last4': '1111',
            'merchant_account': '1001', 'cardholder': 'Jane Cardholder',
            'merchant': 'Acme Store', 'chargeback_date': '2024-06-14',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'chgbk_forbidden')

    def test_staff_ingest_customer_evidence_represent_win(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestChgbk', json=self._payload())
        self.assertEqual(ingested.status_code, 201)
        case = ingested.get_json()['case']
        self.assertEqual(case['status'], 'posted')
        self.assertNotIn('merchant_account', case)
        self.assertEqual(case['account_last4'], '1001')
        case_id = case['case_id']

        again = self.client.post('/ingestChgbk', json=self._payload())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()['case']['case_id'], case_id)

        self.login()
        evidence = self.client.post('/addChgbkEvidence', json={
            'userid': 'alice', 'case_id': case_id, 'kind': 'receipt', 'ref': 'INV-1',
        })
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.get_json()['case']['status'], 'evidence')
        represented = self.client.post('/representChgbk', json={'userid': 'alice', 'case_id': case_id})
        self.assertEqual(represented.status_code, 200)
        self.assertEqual(represented.get_json()['case']['status'], 'represented')

        listed = self.client.post('/listChgbks', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Chargebacks']['ytd_clawed'], '25.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Chargebacks']['cases'][0]['merchant'], 'Acme Store')

        won = self.client.post('/recordChgbkWin', json={'userid': 'teller', 'case_id': case_id})
        self.assertEqual(won.status_code, 200)
        self.assertEqual(won.get_json()['case']['status'], 'won')
        self.assertEqual(len(self.credits), 1)

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
        wire = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(wire.status_code, 200)

    def test_file_ingest_and_ofac_staff_gates(self):
        self.login('teller', 'tier1')
        message = message_from_values({
            'network': 'visa', 'reason': 'fraud', 'amount': '40.00', 'ica': '400000',
            'card_bin': '411111', 'card_last4': '4242', 'merchant_account': '1001',
            'cardholder': 'Blocked Person', 'merchant': 'Acme Store',
            'chargeback_date': '20240614', 'arn': compose_arn('400000', '2024-06-14', 22),
        })
        uploaded = self.client.post('/ingestChgbkFile', json={
            'userid': 'teller', 'file': compose_vcr_file([message]),
        })
        self.assertEqual(uploaded.status_code, 201)
        case = uploaded.get_json()['case']
        self.assertEqual(case['status'], 'held')
        customer_override = self.client.post('/overrideChgbkOfac', json={
            'userid': 'alice', 'case_id': case['case_id'],
        })
        self.assertEqual(customer_override.status_code, 403)
        self.login()
        blocked = self.client.post('/overrideChgbkOfac', json={
            'userid': 'alice', 'case_id': case['case_id'],
        })
        self.assertEqual(blocked.status_code, 403)
        self.login('teller', 'tier1')
        overridden = self.client.post('/overrideChgbkOfac', json={
            'userid': 'teller', 'case_id': case['case_id'],
        })
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['case']['status'], 'posted')

        missing = self.client.post('/ingestChgbkFile', json={'userid': 'teller'})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'missing_file')
        xml = self.client.post('/ingestChgbkFile', json={'userid': 'teller', 'file': '<?xml version="1.0"?>'})
        self.assertEqual(xml.status_code, 400)
        self.assertEqual(xml.get_json()['error'], 'invalid_file')
