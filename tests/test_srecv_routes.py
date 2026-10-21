import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.srecv import (
    MemorySrecvStore,
    SrecvPolicy,
    SrecvService,
    SpeiClock,
    attach_srecv_routes,
    compose_cep1,
    compose_clabe,
    compose_rfc,
    compose_tracking_key,
)

CT = timezone(timedelta(hours=-6))
RECEIVER = '646'
SENDER = '012'
CLABE = compose_clabe('646', '180', '1')
RFC = compose_rfc('GODE561231GR')


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CT).timestamp()


def tracking(seq=1):
    return compose_tracking_key(SENDER, '20240614', seq)


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
            'SpeiIn': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'SpeiIn': service.snapshot(values.get('customer_id')),
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

    attach_srecv_routes(app, service)
    return app


class SrecvRouteTests(unittest.TestCase):
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

        self.service = SrecvService(
            SrecvPolicy(
                dual_control_threshold=Decimal('200000.00'),
                receiver_bank=RECEIVER,
            ),
            MemorySrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': 'None',
            } if userid == 'alice' else {},
            calendar=SpeiClock(tz_offset_hours=-6),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_is_401(self):
        response = self.client.post('/listSpeiIns', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self.login()
        response = self.client.post('/listSpeiIns', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_enroll_staff_ingest_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addSpeiRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada CLABE', 'rail': 'spei',
            'clabe': CLABE, 'rfc': RFC, 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        key_payload = added.get_json()['key']
        self.assertEqual(key_payload['clabe_masked'], '**************0012')
        self.assertNotIn('clabe', key_payload)
        listed = self.client.post('/listSpeiRecvKeys', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['SpeiIn']['keys'][0]['nickname'], 'Ada CLABE')

        ingest = self.client.post('/ingestSpeiIn', json={
            'userid': 'alice',
            'tracking_key': tracking(1),
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
        })
        self.assertEqual(ingest.status_code, 403)

        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestSpeiIn', json={
            'userid': 'teller',
            'tracking_key': tracking(1),
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
        })
        self.assertEqual(ingested.status_code, 201)
        inbound = ingested.get_json()['inbound']
        self.assertEqual(inbound['status'], 'posted')
        self.assertNotIn('tracking_key', inbound)
        self.assertTrue(inbound['tracking_key_masked'].startswith('KNH************'))

        again = self.client.post('/ingestSpeiIn', json={
            'userid': 'teller',
            'tracking_key': tracking(1),
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
        })
        self.assertEqual(again.status_code, 200)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['SpeiIn']['inbounds'][0]['sender_name'], 'Ada Lovelace')

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
        self.client.post('/addSpeiRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada CLABE', 'rail': 'spei',
            'clabe': CLABE, 'rfc': RFC, 'destination_account': '1001',
        })
        self.login('teller', 'tier1')
        record = compose_cep1(
            tracking_key=tracking(21),
            send_bank=SENDER,
            recv_bank=RECEIVER,
            clabe=CLABE,
            rfc=RFC,
            phone='',
            amount=Decimal('250.00'),
            purpose='goods',
            status='posted',
            rail='spei',
            mask=False,
        )
        held = self.client.post('/ingestSpeiInFile', json={
            'userid': 'teller', 'file': record, 'name': 'Blocked Person',
        })
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbounds'][0]['inbound_id']
        self.assertEqual(held.get_json()['inbounds'][0]['status'], 'held')

        self.login('alice')
        denied = self.client.post('/overrideSpeiInOfac', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        self.login('teller', 'tier1')
        overridden = self.client.post('/overrideSpeiInOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        exported = self.client.post('/exportSpeiIn', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(exported.status_code, 200)
        self.assertIn('**************0012', exported.get_json()['file'])
        self.assertNotIn(CLABE, exported.get_json()['file'])

        unmatched = self.client.post('/listUnmatchedSpeiIns', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)

        self.login()
        forbidden = self.client.post('/listUnmatchedSpeiIns', json={'userid': 'alice'})
        self.assertEqual(forbidden.status_code, 403)

    def test_customer_cannot_release_and_staff_missing_customer_id(self):
        self.login()
        added = self.client.post('/addSpeiRecvKey', json={
            'userid': 'alice', 'nickname': 'Ada CLABE', 'rail': 'spei',
            'clabe': CLABE, 'rfc': RFC, 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestSpeiIn', json={
            'userid': 'teller',
            'tracking_key': tracking(31),
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
        })
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.login()
        denied = self.client.post('/releaseSpeiIn', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier1')
        missing = self.client.post('/addSpeiRecvKey', json={
            'userid': 'teller', 'nickname': 'Ops', 'rail': 'spei',
            'clabe': CLABE, 'rfc': RFC, 'destination_account': '1001',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'missing_customer_id')

    def test_customer_claim_route(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestSpeiIn', json={
            'userid': 'teller',
            'tracking_key': tracking(41),
            'amount': '80.00',
            'name': 'Ada Lovelace',
            'send_bank': SENDER,
            'recv_bank': RECEIVER,
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['inbound']['status'], 'unmatched')
        self.login()
        claimed = self.client.post('/claimSpeiIn', json={
            'userid': 'alice',
            'rail': 'spei',
            'clabe': CLABE,
            'rfc': RFC,
            'destination_account': '1001',
            'nickname': 'Ada CLABE',
        })
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()['inbound']['status'], 'posted')
        self.assertNotIn('clabe', claimed.get_json()['inbound'])


if __name__ == '__main__':
    unittest.main()
