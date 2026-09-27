import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.inrtp import (
    InRtpPolicy,
    InRtpService,
    InstantClock,
    MemoryInRtpStore,
    attach_inrtp_routes,
    compose_pacs008,
)

ET = timezone(timedelta(hours=-4))
OUR_ABA = '021000021'
SENDER_ABA = '026009593'
UETR_A = '550e8400-e29b-41d4-a716-446655440000'
UETR_B = '550e8400-e29b-41d4-a716-446655440001'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def pacs_message(uetr=UETR_A, amount='1250.00', account='1001', **extra):
    fields = {
        'uetr': uetr,
        'end_to_end_id': extra.get('end_to_end_id', 'E2E' + uetr[-6:]),
        'msg_id': extra.get('msg_id', 'MSG' + uetr[-6:]),
        'tx_id': extra.get('tx_id', 'TX' + uetr[-6:]),
        'rail': extra.get('rail', 'fednow'),
        'amount': amount,
        'sender_aba': extra.get('sender_aba', SENDER_ABA),
        'receiver_aba': extra.get('receiver_aba', OUR_ABA),
        'beneficiary_account': account,
        'beneficiary_name': extra.get('beneficiary_name', 'ADA LOVELACE'),
        'originator_name': extra.get('originator_name', 'ACME CORP'),
    }
    return compose_pacs008(fields)


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
            'InRtps': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'checkin': {'Account': 1001, 'Balance': 50}},
            'Info': {'first_name': 'Ada'},
            'InRtps': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 201

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    attach_inrtp_routes(app, service)
    return app


class InRtpRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 23, 0)]
        self.credits = []
        self.debits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = InRtpService(
            InRtpPolicy(receiver_aba=OUR_ABA, dual_control_threshold=Decimal('10000.00')),
            MemoryInRtpStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
            },
            lookup_fn=lambda account: 'alice' if str(account) in {'1001', '1002'} else None,
            instant_clock=InstantClock(tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_anonymous_is_401(self):
        response = self.client.post('/listInRtps', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._login()
        response = self.client.post('/listInRtps', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._login()
        response = self.client.post('/ingestInRtp', json={'file': pacs_message()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'inrtp_forbidden')
        self.assertEqual(self.credits, [])

    def test_staff_ingest_and_customer_snapshot(self):
        self._login('teller', 'tier1')
        ingested = self.client.post('/ingestInRtp', json={'file': pacs_message(), 'userid': 'teller'})
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['inbound']['status'], 'posted')
        self.assertEqual(body['inbound']['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', body['inbound'])

        again = self.client.post('/ingestInRtp', json={'file': pacs_message(), 'userid': 'teller'})
        self.assertEqual(again.status_code, 200)

        self._login('alice', 'customer')
        listed = self.client.post('/listInRtps', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        snap = listed.get_json()['InRtps']
        self.assertEqual(snap['ytd_posted'], '1250.00')
        dash = self.client.post('/loadCustomer')
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['InRtps']['posted_count'], 1)

    def test_staff_file_ingest_and_unmatched_assign(self):
        self._login('teller', 'tier1')
        batch = self.client.post('/ingestInRtpFile', json={
            'userid': 'teller',
            'file': pacs_message(account='404404404') + pacs_message(uetr=UETR_B),
        })
        self.assertEqual(batch.status_code, 201)
        payload = batch.get_json()
        self.assertEqual(payload['accepted_count'], 2)
        unmatched = self.client.post('/listUnmatchedInRtps', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)
        rows = unmatched.get_json()['InRtps']['unmatched']
        self.assertEqual(len(rows), 1)
        assigned = self.client.post('/assignInRtp', json={
            'userid': 'teller',
            'inbound_id': rows[0]['inbound_id'],
            'customer_id': 'alice',
            'account': '1001',
        })
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.get_json()['inbound']['status'], 'posted')

    def test_customer_return_and_staff_ofac_release(self):
        self._login('teller', 'tier1')
        held = self.client.post('/ingestInRtp', json={
            'userid': 'teller',
            'originator_name': 'OFAC TESTNAME',
            'uetr': '550e8400-e29b-41d4-a716-446655440008',
            'end_to_end_id': 'E2E8',
            'amount': '200.00',
            'sender_aba': SENDER_ABA,
            'receiver_aba': OUR_ABA,
            'beneficiary_account': '1001',
            'beneficiary_name': 'Ada Lovelace',
            'rail': 'rtp',
        })
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbound']['inbound_id']
        self.assertEqual(held.get_json()['inbound']['status'], 'held')
        self.assertEqual(self.credits, [])

        denied = self.client.post('/releaseInRtp', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        overridden = self.client.post('/overrideInRtpOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        self._login('alice', 'customer')
        returned = self.client.post('/requestInRtpReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id, 'reason': 'CUST',
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
