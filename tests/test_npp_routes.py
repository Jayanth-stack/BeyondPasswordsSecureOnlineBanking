import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.npp import (
    AusClock,
    MemoryNppStore,
    NppPolicy,
    NppService,
    attach_npp_routes,
)

AEST = timezone(timedelta(hours=10))
PAYID = 'ada@example.com'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=AEST).timestamp()


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
            'Npp': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Npp': service.snapshot(values.get('customer_id')),
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

    attach_npp_routes(app, service)
    return app


class NppRouteTests(unittest.TestCase):
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

        self.service = NppService(
            NppPolicy(
                outbound_fee=Decimal('0.00'),
                dual_control_threshold=Decimal('10000.00'),
                min_amount=Decimal('1.00'),
            ),
            MemoryNppStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=AusClock(tz_offset_minutes=600),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _bene_payload(self, **overrides):
        body = {
            'userid': 'alice',
            'nickname': 'Maria',
            'legal_name': 'Ada Lovelace',
            'rail': 'osko',
            'payid': PAYID,
            'default_account': '1001',
        }
        body.update(overrides)
        return body

    def test_unauthenticated_is_401(self):
        response = self.client.post('/addNppBeneficiary', json=self._bene_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._session()
        response = self.client.post('/addNppBeneficiary', json=self._bene_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_send_preview_lookup_recall_and_existing_money_routes(self):
        self._session()
        added = self.client.post('/addNppBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        beneficiary_id = added.get_json()['beneficiary']['beneficiary_id']
        self.assertNotIn('payid', added.get_json()['beneficiary'])
        self.assertNotIn('bsb', added.get_json()['beneficiary'])

        lookup = self.client.post('/lookupPayId', json={
            'userid': 'alice', 'payid': PAYID, 'legal_name': 'Ada Lovelace',
        })
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['PayId']['outcome'], 'match')
        self.assertNotEqual(lookup.get_json()['PayId']['payid_masked'], PAYID)

        preview = self.client.post('/previewNpp', json={
            'userid': 'alice', 'beneficiary_id': beneficiary_id, 'amount': '25.00',
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()['preview']['currency'], 'AUD')
        self.assertEqual(preview.get_json()['preview']['payid']['outcome'], 'match')

        sent = self.client.post('/sendNpp', json={
            'userid': 'alice', 'beneficiary_id': beneficiary_id, 'amount': '25.00', 'trace_id': 'r1',
        })
        self.assertEqual(sent.status_code, 201)
        payment_id = sent.get_json()['payment']['payment_id']
        self.assertEqual(sent.get_json()['payment']['status'], 'completed')
        self.assertNotIn('e2e', sent.get_json()['payment'])

        listed = self.client.post('/listNpp', json={'userid': 'alice'})
        self.assertEqual(listed.get_json()['Npp']['ytd_sent'], '25.00')

        recalled = self.client.post('/recallNpp', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['payment']['status'], 'recalled')

        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        withdraw = self.client.post('/withdrawAmount', json={'userid': 'alice'})
        self.assertEqual(withdraw.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_staff_lookup_idor_ofac_and_export(self):
        self._session()
        added = self.client.post('/addNppBeneficiary', json=self._bene_payload(
            nickname='Ally', legal_name='Blocked Person LLC', payid='blocked@example.net',
        ))
        beneficiary_id = added.get_json()['beneficiary']['beneficiary_id']
        held = self.client.post('/sendNpp', json={
            'userid': 'alice', 'beneficiary_id': beneficiary_id, 'amount': '30.00', 'trace_id': 'h1',
        })
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        payment_id = held.get_json()['payment']['payment_id']

        self._session('bob', 'customer')
        stolen = self.client.post('/recallNpp', json={'userid': 'bob', 'payment_id': payment_id})
        self.assertEqual(stolen.status_code, 403)

        self._session('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Npp']['beneficiaries'][0]['nickname'], 'Ally')

        other = self.client.post('/releaseNpp', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(other.status_code, 403)
        self.assertEqual(other.get_json()['error'], 'ofac_hold')

        overridden = self.client.post('/overrideNppOfac', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['payment']['status'], 'completed')

        exported = self.client.post('/exportNpp', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(exported.status_code, 200)
        self.assertIn('NPP1|1|', exported.get_json()['export']['npp1'])
        self.assertNotIn('blocked@example.net', exported.get_json()['export']['npp1'])

    def test_osko_cap_and_invalid_bsb(self):
        self._session()
        added = self.client.post('/addNppBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        beneficiary_id = added.get_json()['beneficiary']['beneficiary_id']
        over = self.client.post('/sendNpp', json={
            'userid': 'alice', 'beneficiary_id': beneficiary_id, 'amount': '10000.01',
        })
        self.assertEqual(over.status_code, 400)
        self.assertEqual(over.get_json()['error'], 'osko_amount_exceeded')
        bad = self.client.post('/addNppBeneficiary', json=self._bene_payload(
            nickname='Bad', rail='de', payid='', bsb='000000', account_number='12345678',
        ))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()['error'], 'invalid_bsb')


if __name__ == '__main__':
    unittest.main()
