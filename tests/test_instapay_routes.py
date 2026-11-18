import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.instapay import (
    MemoryInstapayStore,
    InstapayPolicy,
    InstapayService,
    PhClock,
    attach_instapay_routes,
)

PHT = timezone(timedelta(hours=8))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PHT).timestamp()


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
            'InstaPay': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'InstaPay': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    attach_instapay_routes(app, service)
    return app


class InstapayRouteTests(unittest.TestCase):
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

        self.service = InstapayService(
            InstapayPolicy(
                outbound_fee=Decimal('0.00'),
                philpass_fee=Decimal('8.00'),
                dual_control_threshold=Decimal('50000.00'),
                phpusd=Decimal('0.017500'),
            ),
            MemoryInstapayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=PhClock(),
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
            'nickname': 'Juan Mobile',
            'legal_name': 'Juan Cruz',
            'filipino_name': 'Juan dela Cruz',
            'bank_code': '011',
            'account_number': '1234567890',
            'default_account': '1001',
            'proxy_type': 'phone',
            'proxy': '09171234567',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_complete_or_recall(self):
        self.login()
        added = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('account_number', body['beneficiary'])
        self.assertNotIn('filipino_name', body['beneficiary'])
        self.assertNotIn('fingerprint', body['beneficiary'])
        bene_id = body['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendInstaPay', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '10000.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        payment_id = sent.get_json()['payment']['payment_id']
        leaked = sent.get_json()['payment']
        self.assertNotIn('account_number', leaked)
        self.assertNotEqual(leaked['e2e_id'], 'INST20240614000001')
        self.assertTrue(leaked['e2e_id'].startswith('INST'))
        completed = self.client.post('/completeInstaPay', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'instapay_forbidden')
        recalled = self.client.post('/recallInstaPay', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_complete_recall_quote_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload(nickname='BPI Juan'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        quoted = self.client.post('/quoteInstaPayFx', json={'userid': 'alice', 'amount': '10000.00'})
        self.assertEqual(quoted.status_code, 200)
        self.assertEqual(quoted.get_json()['quote']['usd_amount'], '175.00')
        sent = self.client.post('/sendInstaPay', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '10000.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        payment = sent.get_json()['payment']
        self.assertEqual(payment['status'], 'completed')
        self.assertEqual(payment['amount'], '10000.00')
        self.assertEqual(payment['usd_amount'], '175.00')

        listed = self.client.post('/listInstaPay', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['InstaPay']['ytd_sent'], '10000.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['InstaPay']['beneficiaries'][0]['nickname'], 'BPI Juan')

        other = self.client.post('/sendInstaPay', json={
            'userid': 'teller', 'customer_id': 'alice', 'beneficiary_id': bene_id,
            'amount': '33000.00', 'trace_id': 'in-2', 'rail': 'pesonet',
        })
        self.assertEqual(other.status_code, 201)
        self.assertEqual(other.get_json()['payment']['status'], 'sent')
        recalled = self.client.post('/recallInstaPay', json={
            'userid': 'teller', 'payment_id': other.get_json()['payment']['payment_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['payment']['status'], 'recalled')

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

    def test_filipino_lookup_and_get_still_mutates(self):
        self.login()
        inquired = self.client.post('/lookupInstaPay', json={
            'userid': 'alice', 'proxy_type': 'phone', 'proxy': '09171234567', 'filipino_name': 'Juan dela Cruz',
        })
        self.assertEqual(inquired.status_code, 200)
        self.assertEqual(inquired.get_json()['filipino']['outcome'], 'match')
        self.assertNotIn('Juan dela Cruz', str(inquired.get_json()))
        added = self.client.get('/addInstaPayBeneficiary', json=self._bene_payload(nickname='GET Juan'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        sent = self.client.get('/sendInstaPay', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '10000.00', 'trace_id': 'get-1',
        })
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()['payment']['usd_amount'], '175.00')
        self.assertEqual(self.debits[0], ('1001', '175.00', 'instapay to GET Juan'))

    def test_bad_bank_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload(bank_code='000'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_bank')
        added = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload(
            nickname='Blocked', legal_name='Blocked Person',
            proxy_type='', proxy='', bank_code='010', account_number='9876543210',
            filipino_name='Maria Santos',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendInstaPay', json={
            'userid': 'alice',
            'beneficiary_id': added.get_json()['beneficiary']['beneficiary_id'],
            'amount': '10000.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        overridden = self.client.post('/overrideInstaPayOfac', json={
            'userid': 'alice', 'payment_id': held.get_json()['payment']['payment_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release(self):
        self.now[0] = ts(2024, 6, 14, 16, 0)
        self.login()
        added = self.client.post('/addInstaPayBeneficiary', json=self._bene_payload(nickname='Night'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        queued = self.client.post('/sendInstaPay', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '33000.00', 'rail': 'pesonet',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['payment']['status'], 'queued')
        payment_id = queued.get_json()['payment']['payment_id']
        denied = self.client.post('/releaseInstaPay', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectInstaPay', json={'userid': 'teller', 'payment_id': payment_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['payment']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
