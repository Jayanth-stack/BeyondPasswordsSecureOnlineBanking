import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.vau import (
    MemoryVauStore,
    UpdaterClock,
    VauPolicy,
    VauService,
    attach_vau_routes,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
VISA_NEW = '4012888888881881'


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
            'CardUpdater': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'CardUpdater': service.snapshot(values.get('customer_id'), actor=session.get('userid'), actor_type=session.get('usertype') or 'employee'),
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

    attach_vau_routes(app, service)
    return app


class VauRouteTests(unittest.TestCase):
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

        self.service = VauService(
            VauPolicy(advice_fee=Decimal('0.10')),
            MemoryVauStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=UpdaterClock(cutoff_hour=22, tz_offset_hours=-4),
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
        response = self.client.post('/addCardUpdaterKey', json=self._key_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addCardUpdaterKey', json=self._key_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_reverse_as_staff_or_ingest(self):
        self.login()
        added = self.client.post('/addCardUpdaterKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('pan', body['key'])
        key_id = body['key']['key_id']
        sent = self.client.post('/reportCardUpdate', json={
            'userid': 'alice', 'key_id': key_id, 'reason': 'E', 'new_expiry': '1228',
            'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        advice_id = sent.get_json()['advice']['advice_id']
        self.assertNotIn('pan', sent.get_json()['advice'])
        self.assertNotIn('vau_ref', sent.get_json()['advice'])
        staff_reverse = self.client.post('/reverseCardUpdate', json={'userid': 'alice', 'advice_id': advice_id})
        self.assertEqual(staff_reverse.status_code, 403)
        self.assertEqual(staff_reverse.get_json()['error'], 'vau_forbidden')
        ingest = self.client.post('/ingestCardUpdateInquiry', json={
            'userid': 'alice', 'pan': VISA, 'expiry': '1227',
        })
        self.assertEqual(ingest.status_code, 403)

    def test_staff_release_reverse_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addCardUpdaterKey', json=self._key_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        key_id = added.get_json()['key']['key_id']
        pending = self.client.post('/reportCardUpdate', json={
            'userid': 'alice', 'key_id': key_id, 'reason': 'A',
            'new_pan': VISA_NEW, 'new_expiry': '1228', 'trace_id': 'in-1',
        })
        self.assertEqual(pending.status_code, 201)
        advice = pending.get_json()['advice']
        self.assertEqual(advice['status'], 'pending_release')

        listed = self.client.post('/listCardUpdater', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['CardUpdater']['open_count'], 1)

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['CardUpdater']['keys'][0]['nickname'], 'Ally')

        same = self.client.post('/releaseCardUpdate', json={
            'userid': 'teller', 'advice_id': advice['advice_id'],
        })
        # originator was alice the customer, so teller is a different actor — allowed
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.get_json()['advice']['status'], 'posted')

        reversed_row = self.client.post('/reverseCardUpdate', json={
            'userid': 'teller', 'advice_id': advice['advice_id'],
        })
        self.assertEqual(reversed_row.status_code, 200)
        self.assertEqual(reversed_row.get_json()['advice']['status'], 'reversed')

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

    def test_bad_pan_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addCardUpdaterKey', json=self._key_payload(pan='4111111111111112'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_pan')
        added = self.client.post('/addCardUpdaterKey', json=self._key_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/reportCardUpdate', json={
            'userid': 'alice',
            'key_id': added.get_json()['key']['key_id'],
            'reason': 'E',
            'new_expiry': '1228',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['advice']['status'], 'held')
        overridden = self.client.post('/overrideCardUpdateOfac', json={
            'userid': 'alice', 'advice_id': held.get_json()['advice']['advice_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_ingest_file_and_customer_cannot_export(self):
        self.login('teller', 'tier2')
        merchant = self.client.post('/addCardUpdaterMerchant', json={
            'userid': 'teller', 'nickname': 'Acme', 'legal_name': 'Acme LLC',
            'bid': '1234567', 'network': 'visa',
        })
        self.assertEqual(merchant.status_code, 201)
        self.assertNotIn('bid', merchant.get_json()['merchant'])
        ingested = self.client.post('/ingestCardUpdateFile', json={
            'userid': 'teller',
            'file': 'VAU1|P|4111111111111111|1227|visa|visa_vau|1234567|Acme|inq-9|posted',
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['advices'][0]['status'], 'unmatched')
        self.login()
        denied = self.client.post('/exportCardUpdate', json={'userid': 'alice'})
        self.assertEqual(denied.status_code, 403)


if __name__ == '__main__':
    unittest.main()
