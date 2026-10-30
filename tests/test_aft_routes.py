import unittest
from datetime import datetime, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.aft import (
    CardNetworkClock,
    MemoryAftStore,
    AftPolicy,
    AftService,
    attach_aft_routes,
)

UTC = timezone.utc
VISA = '4111111111111111'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


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
            'CardPull': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'CardPull': service.snapshot(values.get('customer_id')),
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

    attach_aft_routes(app, service)
    return app


class AftRouteTests(unittest.TestCase):
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

        self.service = AftService(
            AftPolicy(
                outbound_fee=Decimal('2.00'),
                dual_control_threshold=Decimal('2500.00'),
            ),
            MemoryAftStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=CardNetworkClock(tz_offset_hours=0),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _source_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Mom',
            'legal_name': 'Ada Lovelace',
            'pan': VISA,
            'expiry': '1229',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addCardPullSource', json=self._source_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addCardPullSource', json=self._source_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_release_or_export(self):
        self.login()
        added = self.client.post('/addCardPullSource', json=self._source_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('pan', body['source'])
        self.assertNotIn('fingerprint', body['source'])
        source_id = body['source']['source_id']
        sent = self.client.post('/sendCardPull', json={
            'userid': 'alice', 'source_id': source_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        payment = sent.get_json()['payment']
        self.assertNotIn('pan', payment)
        self.assertNotIn('rrn', payment)
        self.assertEqual(payment['status'], 'completed')
        self.assertEqual(payment['mti'], '0200')
        self.assertEqual(payment['processing_code'], '100000')
        released = self.client.post('/releaseCardPull', json={'userid': 'alice', 'payment_id': payment['payment_id']})
        self.assertEqual(released.status_code, 403)
        self.assertEqual(released.get_json()['error'], 'aft_forbidden')
        exported = self.client.post('/exportCardPull', json={'userid': 'alice', 'payment_id': payment['payment_id']})
        self.assertEqual(exported.status_code, 403)

    def test_staff_recall_export_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addCardPullSource', json=self._source_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        source_id = added.get_json()['source']['source_id']
        sent = self.client.post('/sendCardPull', json={
            'userid': 'alice', 'source_id': source_id, 'amount': '25.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        payment = sent.get_json()['payment']
        self.assertEqual(payment['status'], 'completed')
        self.assertEqual(payment['amount'], '25.00')
        self.assertEqual(payment['network'], 'visa_direct')

        listed = self.client.post('/listCardPull', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['CardPull']['ytd_pulled'], '25.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['CardPull']['sources'][0]['nickname'], 'Ally')

        exported = self.client.post('/exportCardPull', json={
            'userid': 'teller', 'payment_id': payment['payment_id'],
        })
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.get_json()['file'].startswith('AFT1|'))
        self.assertNotIn(VISA, exported.get_json()['file'])
        self.assertIn('|100000|', exported.get_json()['file'])

        recalled = self.client.post('/recallCardPull', json={
            'userid': 'teller', 'payment_id': payment['payment_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['payment']['status'], 'recalled')
        self.assertEqual(recalled.get_json()['payment']['mti'], '0420')

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
        wire = self.client.post('/sendWire', json={
            'userid': 'teller', 'amount': '1',
        })
        self.assertEqual(wire.status_code, 200)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_bad_pan_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addCardPullSource', json=self._source_payload(pan='4111111111111112'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_pan')
        added = self.client.post('/addCardPullSource', json=self._source_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendCardPull', json={
            'userid': 'alice',
            'source_id': added.get_json()['source']['source_id'],
            'amount': '40.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        overridden = self.client.post('/overrideCardPullOfac', json={
            'userid': 'alice', 'payment_id': held.get_json()['payment']['payment_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_pending_customer_cannot_release(self):
        self.login('maker', 'tier1')
        added = self.client.post('/addCardPullSource', json=self._source_payload(
            userid='maker', customer_id='alice', nickname='Wells',
        ))
        self.assertEqual(added.status_code, 201)
        source_id = added.get_json()['source']['source_id']
        pending = self.client.post('/sendCardPull', json={
            'userid': 'maker', 'customer_id': 'alice', 'source_id': source_id,
            'amount': '2500.00',
        })
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()['payment']['status'], 'pending_release')
        payment_id = pending.get_json()['payment']['payment_id']
        denied = self.client.post('/releaseCardPull', json={'userid': 'maker', 'payment_id': payment_id})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error'], 'same_approver')
        self.login('checker', 'tier2')
        rejected = self.client.post('/rejectCardPull', json={'userid': 'checker', 'payment_id': payment_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['payment']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
