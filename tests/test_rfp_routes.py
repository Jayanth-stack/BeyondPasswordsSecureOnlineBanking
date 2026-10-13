import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.rfp import (
    InstantClock,
    MemoryRfpStore,
    RfpPolicy,
    RfpService,
    attach_rfp_routes,
    compose_pain013,
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
            'Rfp': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Rfp': service.snapshot(values.get('customer_id')),
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

    attach_rfp_routes(app, service)
    return app


class RfpRouteTests(unittest.TestCase):
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

        self.service = RfpService(
            RfpPolicy(
                dual_control_threshold=Decimal('10000.00'),
                receiver_routing=RECEIVER,
            ),
            MemoryRfpStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=InstantClock(tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _enroll(self):
        self._session()
        return self.client.post('/enrollRfp', json={
            'userid': 'alice',
            'nickname': 'Home',
            'kind': 'email',
            'alias': 'ada@example.com',
            'destination_account': '1001',
        })

    def test_unauthenticated_is_401(self):
        response = self.client.post('/listRfps', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._session()
        response = self.client.post('/listRfps', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._enroll()
        denied = self.client.post('/ingestRfp', json={
            'userid': 'alice',
            'end_to_end_id': 'RFP20240614000001',
            'amount': '25.00',
            'creditor_name': 'Ada Lovelace',
            'creditor_aba': RECEIVER,
            'creditor_account': '77881234',
            'debtor_account': '1001',
            'rail': 'fednow',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error'], 'rfp_forbidden')

    def test_staff_ingest_and_customer_pay(self):
        added = self._enroll()
        self.assertEqual(added.status_code, 201)
        self.assertNotIn('alias_value', added.get_json()['enrollment'])
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestRfp', json={
            'userid': 'teller',
            'end_to_end_id': 'RFP20240614000001',
            'amount': '25.00',
            'creditor_name': 'Ada Lovelace',
            'creditor_aba': RECEIVER,
            'creditor_account': '77881234',
            'debtor_account': '1001',
            'rail': 'fednow',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['request']['status'], 'requested')
        self.assertNotIn('creditor_account', body['request'])
        self.assertEqual(self.debits, [])
        again = self.client.post('/ingestRfp', json={
            'userid': 'teller',
            'end_to_end_id': 'RFP20240614000001',
            'amount': '25.00',
            'creditor_name': 'Ada Lovelace',
            'creditor_aba': RECEIVER,
            'creditor_account': '77881234',
            'debtor_account': '1001',
            'rail': 'fednow',
            'receiver_routing': RECEIVER,
        })
        self.assertEqual(again.status_code, 200)

        record = compose_pain013(
            end_to_end_id='RFP20240614000002',
            amount=Decimal('10.00'),
            rail='fednow',
            creditor_name='Ada Lovelace',
            creditor_aba=RECEIVER,
            creditor_account='77881234',
            debtor_account='1001',
            receiver=RECEIVER,
        )
        filed = self.client.post('/ingestRfpFile', json={'userid': 'teller', 'file': record})
        self.assertEqual(filed.status_code, 201)

        self._session()
        listed = self.client.post('/listRfps', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        snap = listed.get_json()['Rfp']
        self.assertEqual(snap['enrollments'][0]['alias_masked'], 'a***@example.com')
        request_id = snap['requests'][0]['request_id']
        paid = self.client.post('/payRfp', json={'userid': 'alice', 'request_id': request_id})
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.get_json()['request']['status'], 'paid')
        self.assertEqual(self.debits[0][1], '25.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'alice', 'customer_id': 'alice'})
        self._session('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Rfp']['enrollments'][0]['nickname'], 'Home')

        transfer = self.client.post('/fundTransfer', json={'userid': 'teller'})
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        wire = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(wire.get_json()['message'], 'unchanged')

    def test_decline(self):
        self._enroll()
        self._session('teller', 'tier1')
        ingested = self.client.post('/ingestRfp', json={
            'userid': 'teller',
            'end_to_end_id': 'RFP20240614000003',
            'amount': '20.00',
            'creditor_name': 'Ada Lovelace',
            'creditor_aba': RECEIVER,
            'creditor_account': '77881234',
            'debtor_account': '1001',
            'rail': 'fednow',
            'receiver_routing': RECEIVER,
        })
        request_id = ingested.get_json()['request']['request_id']
        self.assertEqual(ingested.get_json()['request']['status'], 'requested')
        self._session()
        declined = self.client.post('/declineRfp', json={
            'userid': 'alice', 'request_id': request_id, 'reason': 'refuse',
        })
        self.assertEqual(declined.status_code, 200)
        self.assertEqual(declined.get_json()['request']['status'], 'declined')
        self.assertEqual(self.debits, [])


if __name__ == '__main__':
    unittest.main()
