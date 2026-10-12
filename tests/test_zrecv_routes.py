import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.zrecv import (
    MemoryZrecvStore,
    ZelleClock,
    ZrecvPolicy,
    ZrecvService,
    attach_zrecv_routes,
    compose_zel1,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '021000021'


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
            'ZelleIn': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'ZelleIn': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'unchanged'}), 200

    attach_zrecv_routes(app, service)
    return app


class ZrecvRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.credits = []
        self.debits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = ZrecvService(
            ZrecvPolicy(
                dual_control_threshold=Decimal('10000.00'),
                receiver_routing=RECEIVER,
            ),
            MemoryZrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=ZelleClock(tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _add_token(self):
        self._session()
        return self.client.post('/addZelleToken', json={
            'userid': 'alice',
            'nickname': 'Home',
            'kind': 'email',
            'alias': 'ada@example.com',
            'destination_account': '1001',
        })

    def test_unauthenticated_is_401(self):
        response = self.client.post('/listZelleIns', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._session()
        response = self.client.post('/listZelleIns', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._add_token()
        denied = self.client.post('/ingestZelleIn', json={
            'userid': 'alice',
            'payment_id': 'ZEL202406140001',
            'amount': '25.00',
            'sender_name': 'Ada Lovelace',
            'alias_type': 'email',
            'alias': 'ada@example.com',
            'rail': 'enrolled',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error'], 'zrecv_forbidden')

    def test_staff_ingest_enrolled_and_customer_snapshot(self):
        added = self._add_token()
        self.assertEqual(added.status_code, 201)
        self.assertNotIn('value', added.get_json()['token'])
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestZelleIn', json={
            'userid': 'teller',
            'payment_id': 'ZEL202406140001',
            'amount': '25.00',
            'sender_name': 'Ada Lovelace',
            'alias_type': 'email',
            'alias': 'ada@example.com',
            'rail': 'enrolled',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['inbound']['status'], 'posted')
        self.assertNotIn('alias_value', body['inbound'])
        self.assertEqual(self.credits[0][1], '25.00')
        again = self.client.post('/ingestZelleIn', json={
            'userid': 'teller',
            'payment_id': 'ZEL202406140001',
            'amount': '25.00',
            'sender_name': 'Ada Lovelace',
            'alias_type': 'email',
            'alias': 'ada@example.com',
            'rail': 'enrolled',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.credits), 1)

        record = compose_zel1(
            payment_id='ZEL202406140002',
            amount=Decimal('10.00'),
            sender_name='Ada Lovelace',
            sender_fi=RECEIVER,
            kind='email',
            alias='ada@example.com',
            rail='enrolled',
            receiver=RECEIVER,
        )
        filed = self.client.post('/ingestZelleInFile', json={'userid': 'teller', 'file': record})
        self.assertEqual(filed.status_code, 201)

        self._session()
        listed = self.client.post('/listZelleIns', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        snap = listed.get_json()['ZelleIn']
        self.assertEqual(snap['inbounds'][0]['alias_masked'], 'a***@example.com')
        self.assertEqual(snap['ytd_received'], '35.00')

        lookup = self._session('teller', 'tier1') or self.client.post(
            '/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'},
        )
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['ZelleIn']['tokens'][0]['nickname'], 'Home')

        transfer = self.client.post('/fundTransfer', json={'userid': 'teller'})
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        wire = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(wire.get_json()['message'], 'unchanged')

    def test_claim_and_customer_return(self):
        self._add_token()
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestZelleIn', json={
            'userid': 'teller',
            'payment_id': 'ZEL202406140003',
            'amount': '20.00',
            'sender_name': 'Ada Lovelace',
            'alias_type': 'email',
            'alias': 'ada@example.com',
            'rail': 'request',
            'receiver_routing': RECEIVER,
        })
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.assertEqual(ingested.get_json()['inbound']['status'], 'pending_claim')
        self._session()
        claimed = self.client.post('/claimZelleIn', json={
            'userid': 'alice', 'inbound_id': inbound_id,
        })
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()['inbound']['status'], 'posted')
        returned = self.client.post('/requestZelleInReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id,
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['inbound']['status'], 'returned')


if __name__ == '__main__':
    unittest.main()
