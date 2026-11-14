import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.jprecv import (
    JpClock,
    JprecvPolicy,
    JprecvService,
    MemoryJprecvStore,
    attach_jprecv_routes,
    compose_e2e,
    compose_zgn1,
)

JST = timezone(timedelta(hours=9))
RECEIVER = '0398'
SENDER = '0001'
PHONE = '+819012345678'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=JST).timestamp()


def e2e(seq=1):
    return compose_e2e('moretime', '20240614', seq)


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
            'ZenginIn': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'ZenginIn': service.snapshot(values.get('customer_id')),
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

    attach_jprecv_routes(app, service)
    return app


class JprecvRouteTests(unittest.TestCase):
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

        self.service = JprecvService(
            JprecvPolicy(
                dual_control_threshold=Decimal('1000000.00'),
                receiver_bank=RECEIVER,
                jpyusd=Decimal('0.0067'),
            ),
            MemoryJprecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': 'None',
            } if userid == 'alice' else {},
            calendar=JpClock(),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_is_401(self):
        response = self.client.post('/listZenginIns', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self.login()
        response = self.client.post('/listZenginIns', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_enroll_staff_ingest_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addZenginRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada mobile', 'proxy_type': 'phone',
            'proxy': PHONE, 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        key_payload = added.get_json()['key']
        self.assertEqual(key_payload['proxy_masked'], '+81****5678')
        self.assertNotIn('proxy', key_payload)
        listed = self.client.post('/listZenginRecvKeys', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['ZenginIn']['keys'][0]['nickname'], 'Ada mobile')

        ingest = self.client.post('/ingestZenginIn', json={
            'userid': 'alice',
            'end_to_end_id': e2e(1),
            'amount': '10000.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'proxy_type': 'phone',
            'proxy': PHONE,
        })
        self.assertEqual(ingest.status_code, 403)

        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestZenginIn', json={
            'userid': 'teller',
            'end_to_end_id': e2e(1),
            'amount': '10000.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'proxy_type': 'phone',
            'proxy': PHONE,
        })
        self.assertEqual(ingested.status_code, 201)
        inbound = ingested.get_json()['inbound']
        self.assertEqual(inbound['status'], 'posted')
        self.assertNotIn('e2e', inbound)
        self.assertEqual(inbound['e2e_masked'][-4:], e2e(1)[-4:])

        again = self.client.post('/ingestZenginIn', json={
            'userid': 'teller',
            'end_to_end_id': e2e(1),
            'amount': '10000.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'proxy_type': 'phone',
            'proxy': PHONE,
        })
        self.assertEqual(again.status_code, 200)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['ZenginIn']['inbounds'][0]['sender_name'], 'Ada Lovelace')

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
        wire = self.client.post('/sendWire', json={'userid': 'teller', 'amount': '1'})
        self.assertEqual(wire.status_code, 200)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_staff_file_ingest_ofac_dual_control_and_export(self):
        self.login()
        self.client.post('/addZenginRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada mobile', 'proxy_type': 'phone',
            'proxy': PHONE, 'destination_account': '1001',
        })
        self.login('teller', 'tier1')
        record = compose_zgn1(
            e2e=e2e(21),
            rail='moretime',
            proxy_type='phone',
            proxy=PHONE,
            bank=RECEIVER,
            account_number='1001001',
            extra='',
            amount=Decimal('10000.00'),
            purpose='goods',
            status='posted',
            mask=False,
        )
        held = self.client.post('/ingestZenginInFile', json={
            'userid': 'teller', 'file': record, 'name': 'Blocked Person',
        })
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbounds'][0]['inbound_id']
        self.assertEqual(held.get_json()['inbounds'][0]['status'], 'held')

        self.login('alice')
        denied = self.client.post('/overrideZenginInOfac', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        self.login('teller', 'tier1')
        overridden = self.client.post('/overrideZenginInOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        exported = self.client.post('/exportZenginIn', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(exported.status_code, 200)
        self.assertIn('+81****5678', exported.get_json()['file'])
        self.assertNotIn('9012345678', exported.get_json()['file'])

        unmatched = self.client.post('/listUnmatchedZenginIns', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)

        self.login()
        forbidden = self.client.post('/listUnmatchedZenginIns', json={'userid': 'alice'})
        self.assertEqual(forbidden.status_code, 403)

    def test_customer_cannot_release_and_staff_missing_customer_id(self):
        self.login()
        added = self.client.post('/addZenginRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada mobile', 'proxy_type': 'phone',
            'proxy': PHONE, 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestZenginIn', json={
            'userid': 'teller',
            'end_to_end_id': e2e(31),
            'amount': '10000.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'proxy_type': 'phone',
            'proxy': PHONE,
        })
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.login()
        denied = self.client.post('/releaseZenginIn', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier1')
        missing = self.client.post('/addZenginRecvKey', json={
            'userid': 'teller', 'nickname': 'Ops', 'proxy_type': 'phone',
            'proxy': PHONE, 'destination_account': '1001',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'missing_customer_id')

    def test_customer_proxy_claim_route(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestZenginIn', json={
            'userid': 'teller',
            'end_to_end_id': e2e(41),
            'amount': '80.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'proxy_type': 'phone',
            'proxy': PHONE,
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['inbound']['status'], 'unmatched')
        self.login()
        claimed = self.client.post('/claimZenginIn', json={
            'userid': 'alice',
            'proxy_type': 'phone',
            'proxy': PHONE,
            'destination_account': '1001',
            'nickname': 'Ada mobile',
        })
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()['inbound']['status'], 'posted')
        self.assertNotIn('proxy', claimed.get_json()['inbound'])


if __name__ == '__main__':
    unittest.main()
