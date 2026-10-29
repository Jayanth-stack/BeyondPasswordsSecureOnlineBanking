import unittest
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, session

from utility.octin import (
    CardNetworkClock,
    MemoryOctinStore,
    OctinPolicy,
    OctinService,
    attach_octin_routes,
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
            'CardRecv': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'CardRecv': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire queued'}), 200

    attach_octin_routes(app, service)
    return app


class OctinRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 11, 0)]
        self.credits = []
        self.debits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = OctinService(
            OctinPolicy(),
            MemoryOctinStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=CardNetworkClock(tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _key_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Travel Visa',
            'legal_name': 'Ada Lovelace',
            'pan': VISA,
            'expiry': '1227',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addCardRecvKey', json=self._key_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addCardRecvKey', json=self._key_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest_or_release(self):
        self.login()
        added = self.client.post('/addCardRecvKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('pan', body['key'])
        ingested = self.client.post('/ingestCardRecv', json={
            'userid': 'alice', 'pan': VISA, 'amount': '20.00', 'sender_name': 'Acme',
        })
        self.assertEqual(ingested.status_code, 403)
        self.assertEqual(ingested.get_json()['error'], 'octin_forbidden')

    def test_staff_ingest_release_return_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addCardRecvKey', json=self._key_payload(nickname='Debit'))
        self.assertEqual(added.status_code, 201)

        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestCardRecv', json={
            'userid': 'teller', 'pan': VISA, 'amount': '25.00', 'sender_name': 'Acme', 'rrn': '240615000021',
        })
        self.assertEqual(ingested.status_code, 201)
        inbound = ingested.get_json()['inbound']
        self.assertEqual(inbound['status'], 'posted')
        self.assertNotIn('pan', inbound)
        self.assertNotEqual(inbound.get('rrn_masked'), '240615000021')

        listed = self.client.post('/listCardRecv', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['CardRecv']['ytd_posted'], '25.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['CardRecv']['keys'][0]['nickname'], 'Debit')

        returned = self.client.post('/returnCardRecv', json={
            'userid': 'teller', 'inbound_id': inbound['inbound_id'],
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['inbound']['status'], 'returned')

        pending = self.client.post('/ingestCardRecv', json={
            'userid': 'teller', 'pan': VISA, 'amount': '2500.00', 'sender_name': 'Acme',
        })
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()['inbound']['status'], 'pending_release')
        same = self.client.post('/releaseCardRecv', json={
            'userid': 'teller', 'inbound_id': pending.get_json()['inbound']['inbound_id'],
        })
        self.assertEqual(same.status_code, 403)
        self.assertEqual(same.get_json()['error'], 'same_approver')

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

    def test_bad_pan_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addCardRecvKey', json=self._key_payload(pan='4111111111111112'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_pan')
        added = self.client.post('/addCardRecvKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        held = self.client.post('/ingestCardRecv', json={
            'userid': 'teller', 'pan': VISA, 'amount': '20.00', 'sender_name': 'Blocked Person',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['inbound']['status'], 'held')
        overridden = self.client.post('/overrideCardRecvOfac', json={
            'userid': 'alice', 'inbound_id': held.get_json()['inbound']['inbound_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_claim_unmatched_and_customer_return(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestCardRecv', json={
            'userid': 'teller', 'pan': VISA, 'amount': '16.00', 'sender_name': 'Acme', 'expiry': '1227',
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['inbound']['status'], 'unmatched')
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.login()
        claimed = self.client.post('/claimCardRecv', json={
            'userid': 'alice', 'pan': VISA, 'destination_account': '1001', 'nickname': 'My Visa',
        })
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()['inbound']['status'], 'posted')
        requested = self.client.post('/requestCardRecvReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id,
        })
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.get_json()['inbound']['status'], 'returned')

    def test_staff_reject_and_file_ingest(self):
        self.login()
        self.client.post('/addCardRecvKey', json=self._key_payload())
        self.login('teller', 'tier2')
        line = 'OCT1|0260|%s|1227|19.00|6538|260000|240615000033|000033|Acme|oct|posted' % VISA
        ingested = self.client.post('/ingestCardRecvFile', json={'userid': 'teller', 'file': line})
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['created'], 1)
        pending = self.client.post('/ingestCardRecv', json={
            'userid': 'teller', 'pan': VISA, 'amount': '2500.00', 'sender_name': 'Acme',
        })
        inbound_id = pending.get_json()['inbound']['inbound_id']
        rejected = self.client.post('/rejectCardRecv', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['inbound']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
