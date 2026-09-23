import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.rtp import (
    InstantClock,
    MemoryRtpStore,
    RtpPolicy,
    RtpService,
    attach_rtp_routes,
)

ET = timezone(timedelta(hours=-4))


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
            'Rtp': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Rtp': service.snapshot(values.get('customer_id')),
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

    attach_rtp_routes(app, service)
    return app


class RtpRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 23, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = RtpService(
            RtpPolicy(
                fednow_fee=Decimal('1.00'),
                rtp_fee=Decimal('0.45'),
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryRtpStore(),
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

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _party_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Chase',
            'legal_name': 'Ada Lovelace',
            'aba': '021000021',
            'account_number': '77881234',
            'street': '1 Federal St',
            'city': 'New York',
            'state': 'NY',
            'postal': '10004',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addRtpCounterparty', json=self._party_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addRtpCounterparty', json=self._party_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_recall_or_complete(self):
        self.login()
        added = self.client.post('/addRtpCounterparty', json=self._party_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('account_number', body['counterparty'])
        party_id = body['counterparty']['counterparty_id']
        sent = self.client.post('/sendRtp', json={
            'userid': 'alice', 'counterparty_id': party_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        payment_id = sent.get_json()['payment']['payment_id']
        self.assertEqual(sent.get_json()['payment']['status'], 'completed')
        self.assertNotIn('account_number', sent.get_json()['payment'])
        completed = self.client.post('/completeRtp', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'rtp_forbidden')
        recalled = self.client.post('/recallRtp', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_accept_rfp_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addRtpCounterparty', json=self._party_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        party_id = added.get_json()['counterparty']['counterparty_id']
        sent = self.client.post('/sendRtp', json={
            'userid': 'alice', 'counterparty_id': party_id, 'amount': '25.00',
            'rail': 'rtp', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        payment = sent.get_json()['payment']
        self.assertEqual(payment['status'], 'completed')
        self.assertEqual(payment['amount'], '25.00')
        self.assertEqual(payment['rail'], 'rtp')

        listed = self.client.post('/listRtps', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Rtp']['ytd_sent'], '25.00')

        requested = self.client.post('/requestRtp', json={
            'userid': 'alice', 'counterparty_id': party_id, 'amount': '18.00', 'trace_id': 'rfp-1',
        })
        self.assertEqual(requested.status_code, 201)
        request_id = requested.get_json()['request']['request_id']

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Rtp']['counterparties'][0]['nickname'], 'Ally')

        recalled = self.client.post('/recallRtp', json={
            'userid': 'teller', 'payment_id': payment['payment_id'],
        })
        self.assertEqual(recalled.status_code, 403)
        self.assertEqual(recalled.get_json()['error'], 'scheme_irrevocable')

        accepted = self.client.post('/acceptRfp', json={
            'userid': 'teller', 'request_id': request_id,
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()['request']['status'], 'accepted')
        self.assertEqual(self.credits[0][1], '18.00')

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
            'userid': 'teller', 'beneficiary_id': 'x', 'amount': '1',
        })
        self.assertEqual(wire.status_code, 200)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_bad_aba_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addRtpCounterparty', json=self._party_payload(aba='021000022'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_aba')
        added = self.client.post('/addRtpCounterparty', json=self._party_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendRtp', json={
            'userid': 'alice',
            'counterparty_id': added.get_json()['counterparty']['counterparty_id'],
            'amount': '40.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        overridden = self.client.post('/overrideRtpOfac', json={
            'userid': 'alice', 'payment_id': held.get_json()['payment']['payment_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_pending_customer_cannot_release(self):
        self.login('maker', 'tier1')
        added = self.client.post('/addRtpCounterparty', json=self._party_payload(
            userid='maker', customer_id='alice', nickname='Wells',
        ))
        self.assertEqual(added.status_code, 201)
        party_id = added.get_json()['counterparty']['counterparty_id']
        pending = self.client.post('/sendRtp', json={
            'userid': 'maker', 'customer_id': 'alice', 'counterparty_id': party_id,
            'amount': '10000.00',
        })
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()['payment']['status'], 'pending_release')
        payment_id = pending.get_json()['payment']['payment_id']
        denied = self.client.post('/releaseRtp', json={'userid': 'maker', 'payment_id': payment_id})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()['error'], 'same_approver')
        self.login('alice')
        customer_release = self.client.post('/releaseRtp', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(customer_release.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectRtp', json={'userid': 'teller', 'payment_id': payment_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['payment']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
