import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.crecv import (
    ChipsCalendar,
    CrecvPolicy,
    CrecvService,
    MemoryCrecvStore,
    attach_crecv_routes,
    compose_chip1,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '0001'


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
            'ChipsIn': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'ChipsIn': service.snapshot(values.get('customer_id')),
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

    attach_crecv_routes(app, service)
    return app


class CrecvRouteTests(unittest.TestCase):
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

        self.service = CrecvService(
            CrecvPolicy(
                dual_control_threshold=Decimal('10000.00'),
                receiver_pid=RECEIVER,
            ),
            MemoryCrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': 'None',
            } if userid == 'alice' else {},
            calendar=ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_is_401(self):
        response = self.client.post('/listChipsIns', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self.login()
        response = self.client.post('/listChipsIns', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_enroll_staff_ingest_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addChipsUid', json={
            'userid': 'alice', 'nickname': 'Ops', 'uid': '123456', 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        uid_payload = added.get_json()['uid']
        self.assertEqual(uid_payload['uid_masked'], '***456')
        self.assertNotIn('uid', uid_payload)
        listed = self.client.post('/listChipsUids', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['ChipsIn']['uids'][0]['nickname'], 'Ops')

        ingest = self.client.post('/ingestChipsIn', json={
            'userid': 'alice',
            'sequence': '202406140002000001',
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_pid': '0002',
            'recv_pid': RECEIVER,
            'uid': '123456',
        })
        self.assertEqual(ingest.status_code, 403)

        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestChipsIn', json={
            'userid': 'teller',
            'sequence': '202406140002000001',
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_pid': '0002',
            'recv_pid': RECEIVER,
            'uid': '123456',
        })
        self.assertEqual(ingested.status_code, 201)
        inbound = ingested.get_json()['inbound']
        self.assertEqual(inbound['status'], 'released')
        self.assertNotIn('sequence', inbound)
        self.assertEqual(inbound['sequence_masked'], '20240614***0001')

        again = self.client.post('/ingestChipsIn', json={
            'userid': 'teller',
            'sequence': '202406140002000001',
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_pid': '0002',
            'recv_pid': RECEIVER,
            'uid': '123456',
        })
        self.assertEqual(again.status_code, 200)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['ChipsIn']['inbounds'][0]['sender_name'], 'Ada Lovelace')

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
        self.client.post('/addChipsUid', json={
            'userid': 'alice', 'nickname': 'Ops', 'uid': '123456', 'destination_account': '1001',
        })
        self.login('teller', 'tier1')
        record = compose_chip1(
            sequence='202406140002000021',
            value_date='20240614',
            send_pid='0002',
            recv_pid=RECEIVER,
            uid='123456',
            amount=Decimal('250.00'),
            name='Blocked Person',
            mask=False,
        )
        held = self.client.post('/ingestChipsInFile', json={'userid': 'teller', 'file': record})
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbounds'][0]['inbound_id']
        self.assertEqual(held.get_json()['inbounds'][0]['status'], 'held')

        self.login('alice')
        denied = self.client.post('/overrideChipsInOfac', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        self.login('teller', 'tier1')
        overridden = self.client.post('/overrideChipsInOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'released')

        exported = self.client.post('/exportChipsIn', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(exported.status_code, 200)
        self.assertIn('***456', exported.get_json()['file'])
        self.assertNotIn('123456', exported.get_json()['file'])

        unmatched = self.client.post('/listUnmatchedChipsIns', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)

        self.login()
        forbidden = self.client.post('/listUnmatchedChipsIns', json={'userid': 'alice'})
        self.assertEqual(forbidden.status_code, 403)

    def test_customer_cannot_release_and_staff_missing_customer_id(self):
        self.login()
        added = self.client.post('/addChipsUid', json={
            'userid': 'alice', 'nickname': 'Ops', 'uid': '123456', 'destination_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestChipsIn', json={
            'userid': 'teller',
            'sequence': '202406140002000031',
            'amount': '250.00',
            'name': 'Ada Lovelace',
            'send_pid': '0002',
            'recv_pid': RECEIVER,
            'uid': '123456',
        })
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.login()
        denied = self.client.post('/releaseChipsIn', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier1')
        missing = self.client.post('/addChipsUid', json={
            'userid': 'teller', 'nickname': 'Ops', 'destination_account': '1001',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'missing_customer_id')


if __name__ == '__main__':
    unittest.main()
