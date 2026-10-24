import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.cardhold import (
    CardHoldPolicy,
    CardHoldService,
    HoldClock,
    MemoryCardHoldStore,
    attach_cardhold_routes,
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
            'CardHolds': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'CardHolds': service.snapshot(values.get('customer_id')),
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

    attach_cardhold_routes(app, service)
    return app


class CardHoldRouteTests(unittest.TestCase):
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

        self.service = CardHoldService(
            CardHoldPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryCardHoldStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=HoldClock(tz_offset_hours=-4),
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
            'nickname': 'Visa Daily',
            'pan': VISA,
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addCardHoldKey', json=self._key_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addCardHoldKey', json=self._key_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest_or_capture(self):
        self.login()
        added = self.client.post('/addCardHoldKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('fingerprint', body['key'])
        self.assertNotIn('pan', body['key'])
        ingested = self.client.post('/ingestCardHold', json={
            'userid': 'alice', 'amount': '12.00', 'merchant': 'CAFE', 'pan': VISA,
        })
        self.assertEqual(ingested.status_code, 403)
        self.assertEqual(ingested.get_json()['error'], 'cardhold_forbidden')

    def test_staff_ingest_capture_export_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addCardHoldKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestCardHold', json={
            'userid': 'teller', 'customer_id': 'alice',
            'amount': '15.00', 'merchant': 'CAFE', 'pan': VISA, 'stan': '000111',
        })
        self.assertEqual(ingested.status_code, 201)
        hold = ingested.get_json()['hold']
        self.assertEqual(hold['status'], 'authorized')
        self.assertNotIn('stan', hold)
        self.assertNotIn(VISA, str(hold))
        captured = self.client.post('/captureCardHold', json={
            'userid': 'teller', 'hold_id': hold['hold_id'],
        })
        self.assertEqual(captured.status_code, 200)
        self.assertEqual(captured.get_json()['hold']['status'], 'captured')
        exported = self.client.post('/exportCardHold', json={
            'userid': 'teller', 'hold_id': hold['hold_id'],
        })
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.get_json()['file'].startswith('AUTH1|'))
        self.assertNotIn(VISA, exported.get_json()['file'])
        transfer = self.client.post('/fundTransfer', json={'userid': 'teller'})
        self.assertEqual(transfer.status_code, 200)
        withdraw = self.client.post('/withdrawAmount', json={'userid': 'teller'})
        self.assertEqual(withdraw.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(wire.status_code, 201)

    def test_duplicate_stan_is_200_and_file_xml_is_400(self):
        self.login()
        self.client.post('/addCardHoldKey', json=self._key_payload())
        self.login('teller', 'tier1')
        first = self.client.post('/ingestCardHold', json={
            'userid': 'teller', 'amount': '8.00', 'merchant': 'DELI', 'pan': VISA, 'stan': '000222',
        })
        self.assertEqual(first.status_code, 201)
        again = self.client.post('/ingestCardHold', json={
            'userid': 'teller', 'amount': '8.00', 'merchant': 'DELI', 'pan': VISA, 'stan': '000222',
        })
        self.assertEqual(again.status_code, 200)
        xml = self.client.post('/ingestCardHoldFile', json={
            'userid': 'teller', 'file': '<?xml version="1.0"?><doc/>',
        })
        self.assertEqual(xml.status_code, 400)
        self.assertEqual(xml.get_json()['error'], 'invalid_file')

    def test_staff_missing_customer_id_on_add_is_400(self):
        self.login('teller', 'tier1')
        response = self.client.post('/addCardHoldKey', json={
            'userid': 'teller', 'nickname': 'Visa', 'pan': VISA, 'default_account': '1001',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'missing_customer_id')

    def test_customer_void_and_claim_pan(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestCardHold', json={
            'userid': 'teller', 'amount': '9.00', 'merchant': 'MART', 'pan': VISA, 'stan': '000333',
        })
        self.assertEqual(ingested.get_json()['hold']['status'], 'unmatched')
        hold_id = ingested.get_json()['hold']['hold_id']
        self.login()
        claimed = self.client.post('/claimCardHold', json={
            'userid': 'alice', 'hold_id': hold_id, 'pan': VISA,
            'account': '1001', 'nickname': 'Claimed Visa',
        })
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()['hold']['status'], 'authorized')
        voided = self.client.post('/requestCardHoldVoid', json={
            'userid': 'alice', 'hold_id': hold_id,
        })
        self.assertEqual(voided.status_code, 200)
        self.assertEqual(voided.get_json()['hold']['status'], 'voided')


if __name__ == '__main__':
    unittest.main()
