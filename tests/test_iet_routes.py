import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.iet import (
    CadUsdBook,
    InteracClock,
    IetPolicy,
    IetService,
    MemoryIetStore,
    attach_iet_routes,
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
            'Iet': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Iet': service.snapshot(values.get('customer_id')),
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

    attach_iet_routes(app, service)
    return app


class IetRouteTests(unittest.TestCase):
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

        self.service = IetService(
            IetPolicy(
                autodeposit_fee=Decimal('1.50'),
                question_fee=Decimal('1.00'),
                dual_control_threshold=Decimal('10000.00'),
                cadusd=Decimal('0.740000'),
            ),
            MemoryIetStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=InteracClock(tz_offset_hours=-4),
            fx=CadUsdBook(Decimal('0.740000')),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _contact_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Pat',
            'legal_name': 'Pat Singh',
            'kind': 'email',
            'alias': 'pat@example.com',
            'rail': 'autodeposit',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addIetContact', json=self._contact_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addIetContact', json=self._contact_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_complete_or_recall(self):
        self.login()
        added = self.client.post('/addIetContact', json=self._contact_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('alias_value', body['contact'])
        self.assertNotIn('answer_digest', body['contact'])
        contact_id = body['contact']['contact_id']
        sent = self.client.post('/sendIet', json={
            'userid': 'alice', 'contact_id': contact_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        transfer_id = sent.get_json()['transfer']['transfer_id']
        self.assertNotIn('alias_value', sent.get_json()['transfer'])
        completed = self.client.post('/completeIet', json={'userid': 'alice', 'transfer_id': transfer_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'iet_forbidden')
        recalled = self.client.post('/recallIet', json={'userid': 'alice', 'transfer_id': transfer_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_complete_recall_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addIetContact', json=self._contact_payload(
            nickname='Sam', alias='sam@example.com', rail='question',
            question='Name of first school?', answer='oakridge',
        ))
        self.assertEqual(added.status_code, 201)
        contact_id = added.get_json()['contact']['contact_id']
        sent = self.client.post('/sendIet', json={
            'userid': 'alice', 'contact_id': contact_id, 'amount': '25.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        transfer = sent.get_json()['transfer']
        self.assertEqual(transfer['status'], 'pending_claim')
        self.assertEqual(transfer['amount_cad'], '25.00')
        self.assertEqual(transfer['debit_usd'], '18.50')

        listed = self.client.post('/listIets', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Iet']['ytd_sent'], '18.50')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Iet']['contacts'][0]['nickname'], 'Sam')
        self.assertNotIn('alias_value', lookup.get_json()['Iet']['contacts'][0])

        completed = self.client.post('/completeIet', json={
            'userid': 'teller', 'transfer_id': transfer['transfer_id'],
        })
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['transfer']['status'], 'completed')

        other = self.client.post('/sendIet', json={
            'userid': 'teller', 'customer_id': 'alice', 'contact_id': contact_id,
            'amount': '12.00', 'trace_id': 'in-2',
        })
        self.assertEqual(other.status_code, 201)
        recalled = self.client.post('/recallIet', json={
            'userid': 'teller', 'transfer_id': other.get_json()['transfer']['transfer_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['transfer']['status'], 'recalled')

        transfer_resp = self.client.post('/fundTransfer', json={
            'userid': 'teller', 'fromAccount': '1001', 'toAccount': '1002', 'amount': '1',
        })
        self.assertEqual(transfer_resp.status_code, 200)
        self.assertEqual(transfer_resp.get_json()['message'], 'Request to be approved by tier1 employee')
        withdraw = self.client.post('/withdrawAmount', json={
            'userid': 'teller', 'account': '1001', 'amount': '1',
        })
        self.assertEqual(withdraw.status_code, 200)
        self.assertEqual(withdraw.get_json()['message'], 'Amount Debited')
        wire = self.client.post('/sendWire', json={'userid': 'teller', 'amount': '1'})
        self.assertEqual(wire.status_code, 200)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_bad_alias_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addIetContact', json=self._contact_payload(alias='not-email'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_alias')
        added = self.client.post('/addIetContact', json=self._contact_payload(
            nickname='Blocked', legal_name='Blocked Person', alias='blocked@example.com',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendIet', json={
            'userid': 'alice',
            'contact_id': added.get_json()['contact']['contact_id'],
            'amount': '40.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['transfer']['status'], 'held')
        overridden = self.client.post('/overrideIetOfac', json={
            'userid': 'alice', 'transfer_id': held.get_json()['transfer']['transfer_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_quote_preview_and_customer_cannot_release(self):
        self.login()
        quoted = self.client.post('/quoteIetFx', json={'userid': 'alice', 'amount': '100.00'})
        self.assertEqual(quoted.status_code, 200)
        self.assertEqual(quoted.get_json()['quote']['amount_usd'], '74.00')
        added = self.client.post('/addIetContact', json=self._contact_payload(
            nickname='Wells', alias='wells@example.com', rail='question',
            question='Name of first school?', answer='oakridge',
        ))
        self.assertEqual(added.status_code, 201)
        contact_id = added.get_json()['contact']['contact_id']
        preview = self.client.post('/previewIet', json={
            'userid': 'alice', 'contact_id': contact_id, 'amount': '33.00',
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()['preview']['fee'], '1.00')
        sent = self.client.post('/sendIet', json={
            'userid': 'alice', 'contact_id': contact_id, 'amount': '14000.00',
        })
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()['transfer']['status'], 'pending_release')
        denied = self.client.post('/releaseIet', json={
            'userid': 'alice', 'transfer_id': sent.get_json()['transfer']['transfer_id'],
        })
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectIet', json={
            'userid': 'teller', 'transfer_id': sent.get_json()['transfer']['transfer_id'],
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['transfer']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
