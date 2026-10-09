import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.pospay import (
    MemoryPosPayStore,
    PosPayPolicy,
    PosPayService,
    attach_pospay_routes,
    compose_presentment_record,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))


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
            'PosPay': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'PosPay': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 201

    @app.route('/depositCheck', methods=['POST'])
    def deposit():
        return jsonify({'message': 'Check deposited'}), 200

    attach_pospay_routes(app, service)
    return app


class PosPayRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = PosPayService(
            PosPayPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryPosPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_is_401_and_userid_mismatch_is_403(self):
        response = self.client.post('/listPosPays', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 401)
        self._session()
        mismatch = self.client.post('/listPosPays', json={'userid': 'bob'})
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(mismatch.get_json()['error'], 'userid_mismatch')

    def test_customer_issue_and_staff_ingest_and_loadcustomer(self):
        self._session()
        added = self.client.post('/addPosPayIssue', json={
            'userid': 'alice', 'account': '1001', 'serial': '44',
            'amount': '15.00', 'payee': 'Acme', 'issue_date': '20240601',
        })
        self.assertEqual(added.status_code, 201)
        self.assertNotIn('account', added.get_json()['issue'])
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestPosPay', json={
            'userid': 'teller', 'customer_id': 'alice', 'serial': '44',
            'amount': '15.00', 'payee': 'Acme', 'account': '1001', 'trace_id': 'r1',
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['item']['status'], 'paid')
        self._session()
        listed = self.client.post('/listPosPays', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['PosPay']['ytd_paid'], '15.00')
        lookup = self.client.post('/getCustomer', json={'userid': 'alice', 'customer_id': 'alice'})
        self.assertEqual(lookup.get_json()['PosPay']['items'][0]['serial'], '44')

    def test_exception_pay_and_existing_money_routes_untouched(self):
        self._session()
        self.client.post('/addPosPayIssue', json={
            'userid': 'alice', 'account': '1001', 'serial': '9',
            'amount': '8.00', 'payee': 'Acme', 'issue_date': '20240601',
        })
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestPosPay', json={
            'userid': 'teller', 'customer_id': 'alice', 'serial': '9',
            'amount': '9.00', 'payee': 'Acme', 'account': '1001', 'trace_id': 'ex1',
        })
        self.assertEqual(ingested.get_json()['item']['status'], 'exception')
        item_id = ingested.get_json()['item']['item_id']
        self._session()
        paid = self.client.post('/payPosPay', json={'userid': 'alice', 'item_id': item_id})
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.get_json()['item']['status'], 'paid')
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.status_code, 201)
        deposit = self.client.post('/depositCheck', json={'userid': 'alice'})
        self.assertEqual(deposit.status_code, 200)

    def test_staff_file_ingest_and_missing_customer_id(self):
        self._session('teller', 'tier1')
        missing = self.client.post('/enrollPosPay', json={'userid': 'teller', 'account': '1001'})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'missing_customer_id')
        self._session()
        self.client.post('/addPosPayIssue', json={
            'userid': 'alice', 'account': '1001', 'serial': '3',
            'amount': '4.00', 'payee': 'Ada', 'issue_date': '20240601',
        })
        body = compose_presentment_record(
            serial='3', amount=Decimal('4.00'), account='1001',
            payee='Ada', presentment_id='F1', present_date='20240614',
        )
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestPosPayFile', json={
            'userid': 'teller', 'customer_id': 'alice', 'file': body,
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['items'][0]['status'], 'paid')


if __name__ == '__main__':
    unittest.main()
