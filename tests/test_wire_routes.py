import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WirePolicy,
    WireService,
    attach_wire_routes,
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
            'Wires': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Wires': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    attach_wire_routes(app, service)
    return app


class WireRouteTests(unittest.TestCase):
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

        self.service = WireService(
            WirePolicy(
                outbound_fee=Decimal('25.00'),
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _bene_payload(self, **overrides):
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
        response = self.client.post('/addWireBeneficiary', json=self._bene_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addWireBeneficiary', json=self._bene_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_complete_or_recall(self):
        self.login()
        added = self.client.post('/addWireBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('account_number', body['beneficiary'])
        bene_id = body['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendWire', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        wire_id = sent.get_json()['wire']['wire_id']
        self.assertNotIn('account_number', sent.get_json()['wire'])
        completed = self.client.post('/completeWire', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'wire_forbidden')
        recalled = self.client.post('/recallWire', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_complete_recall_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addWireBeneficiary', json=self._bene_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendWire', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '25.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        wire = sent.get_json()['wire']
        self.assertEqual(wire['status'], 'sent')
        self.assertEqual(wire['amount'], '25.00')

        listed = self.client.post('/listWires', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Wires']['ytd_sent'], '25.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Wires']['beneficiaries'][0]['nickname'], 'Ally')

        completed = self.client.post('/completeWire', json={
            'userid': 'teller', 'wire_id': wire['wire_id'],
        })
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['wire']['status'], 'completed')

        other = self.client.post('/sendWire', json={
            'userid': 'teller', 'customer_id': 'alice', 'beneficiary_id': bene_id,
            'amount': '12.00', 'trace_id': 'in-2',
        })
        self.assertEqual(other.status_code, 201)
        recalled = self.client.post('/recallWire', json={
            'userid': 'teller', 'wire_id': other.get_json()['wire']['wire_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['wire']['status'], 'recalled')

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

    def test_bad_aba_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addWireBeneficiary', json=self._bene_payload(aba='021000022'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_aba')
        added = self.client.post('/addWireBeneficiary', json=self._bene_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendWire', json={
            'userid': 'alice',
            'beneficiary_id': added.get_json()['beneficiary']['beneficiary_id'],
            'amount': '40.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['wire']['status'], 'held')
        overridden = self.client.post('/overrideOfac', json={
            'userid': 'alice', 'wire_id': held.get_json()['wire']['wire_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self.login()
        added = self.client.post('/addWireBeneficiary', json=self._bene_payload(nickname='Wells'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        queued = self.client.post('/sendWire', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '33.00',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['wire']['status'], 'queued')
        wire_id = queued.get_json()['wire']['wire_id']
        denied = self.client.post('/releaseWire', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectWire', json={'userid': 'teller', 'wire_id': wire_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['wire']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
