import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.inach import (
    InAchPolicy,
    InAchService,
    MemoryInAchStore,
    attach_inach_routes,
    compose_nacha,
    compose_trace,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))
OUR_ABA = '021000021'
SENDER_ABA = '026009593'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def nacha_file(seq=42, amount='1250.00', account='1001', rail='sameday', **extra):
    fields = {
        'trace': compose_trace(SENDER_ABA, seq),
        'amount': amount,
        'sender_aba': SENDER_ABA,
        'receiver_aba': OUR_ABA,
        'beneficiary_account': account,
        'beneficiary_name': 'ADA LOVELACE',
        'originator_name': extra.get('originator_name', 'ACME CORP'),
        'sec': 'PPD',
        'rail': rail,
        'txn_code': '22',
        'creation_date': '240614',
        'effective_date': '240614',
    }
    return compose_nacha(fields)


def build_app(service):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}},
            'Info': {'first_name': 'Ada'},
            'InAchs': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'checkin': {'Account': 1001, 'Balance': 50}},
            'Info': {'first_name': 'Ada'},
            'InAchs': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 201

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    attach_inach_routes(app, service)
    return app


class InAchRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.credits = []
        self.debits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = InAchService(
            InAchPolicy(receiver_aba=OUR_ABA, dual_control_threshold=Decimal('10000.00')),
            MemoryInAchStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
            },
            lookup_fn=lambda account: 'alice' if str(account) in {'1001', '1002'} else None,
            calendar=WireCalendar(cutoff_hour=16, cutoff_minute=45, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_anonymous_is_401(self):
        response = self.client.post('/listInAchs', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._login()
        response = self.client.post('/listInAchs', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._login()
        response = self.client.post('/ingestInAch', json={'file': nacha_file()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'inach_forbidden')
        self.assertEqual(self.credits, [])

    def test_staff_ingest_and_customer_snapshot(self):
        self._login('teller', 'tier1')
        ingested = self.client.post('/ingestInAch', json={'file': nacha_file(), 'userid': 'teller'})
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['inbound']['status'], 'posted')
        self.assertEqual(body['inbound']['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', body['inbound'])

        again = self.client.post('/ingestInAch', json={'file': nacha_file(), 'userid': 'teller'})
        self.assertEqual(again.status_code, 200)

        self._login('alice', 'customer')
        listed = self.client.post('/listInAchs', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        snap = listed.get_json()['InAchs']
        self.assertEqual(snap['ytd_posted'], '1250.00')
        dash = self.client.post('/loadCustomer')
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['InAchs']['posted_count'], 1)

    def test_staff_file_ingest_and_unmatched_assign(self):
        self._login('teller', 'tier1')
        batch = self.client.post('/ingestInAchFile', json={
            'userid': 'teller',
            'file': nacha_file(account='404404404') + '\n' + nacha_file(seq=43),
        })
        self.assertEqual(batch.status_code, 201)
        payload = batch.get_json()
        self.assertEqual(payload['accepted_count'], 2)
        unmatched = self.client.post('/listUnmatchedInAchs', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)
        rows = unmatched.get_json()['InAchs']['unmatched']
        self.assertEqual(len(rows), 1)
        assigned = self.client.post('/assignInAch', json={
            'userid': 'teller',
            'inbound_id': rows[0]['inbound_id'],
            'customer_id': 'alice',
            'account': '1001',
        })
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.get_json()['inbound']['status'], 'posted')

    def test_customer_return_and_staff_ofac_release(self):
        self._login('teller', 'tier1')
        held = self.client.post('/ingestInAch', json={
            'userid': 'teller',
            'originator_name': 'OFAC TESTNAME',
            'trace': compose_trace(SENDER_ABA, 8),
            'amount': '200.00',
            'sender_aba': SENDER_ABA,
            'receiver_aba': OUR_ABA,
            'beneficiary_account': '1001',
            'beneficiary_name': 'Ada Lovelace',
            'rail': 'sameday',
        })
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbound']['inbound_id']
        self.assertEqual(held.get_json()['inbound']['status'], 'held')
        self.assertEqual(self.credits, [])

        denied = self.client.post('/releaseInAch', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        overridden = self.client.post('/overrideInAchOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        self._login('alice', 'customer')
        returned = self.client.post('/requestInAchReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id, 'reason': 'cust',
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['inbound']['status'], 'returned')
        self.assertEqual(len(self.debits), 1)

    def test_existing_money_routes_unchanged(self):
        self._login()
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.status_code, 201)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')
