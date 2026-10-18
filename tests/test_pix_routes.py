import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.pix import (
    MemoryPixStore,
    PixPolicy,
    PixService,
    SpiClock,
    attach_pix_routes,
)

BRT = timezone(timedelta(hours=-3))
CPF = '52998224725'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BRT).timestamp()


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
            'Pix': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Pix': service.snapshot(values.get('customer_id')),
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

    attach_pix_routes(app, service)
    return app


class PixRouteTests(unittest.TestCase):
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

        self.service = PixService(
            PixPolicy(
                outbound_fee=Decimal('0.00'),
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryPixStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=SpiClock(tz_offset_hours=-3),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _key_payload(self, **overrides):
        body = {
            'userid': 'alice',
            'nickname': 'Maria',
            'legal_name': 'Maria Silva',
            'key_type': 'cpf',
            'key_value': CPF,
            'ispb': '60701190',
            'default_account': '1001',
        }
        body.update(overrides)
        return body

    def test_unauthenticated_is_401(self):
        response = self.client.post('/addPixKey', json=self._key_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._session()
        response = self.client.post('/addPixKey', json=self._key_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_send_preview_recall_and_existing_money_routes(self):
        self._session()
        added = self.client.post('/addPixKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        key_id = added.get_json()['key']['key_id']
        self.assertNotIn('key_value', added.get_json()['key'])

        preview = self.client.post('/previewPix', json={
            'userid': 'alice', 'key_id': key_id, 'amount': '25.00',
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()['preview']['currency'], 'BRL')

        sent = self.client.post('/sendPix', json={
            'userid': 'alice', 'key_id': key_id, 'amount': '25.00', 'trace_id': 'r1',
        })
        self.assertEqual(sent.status_code, 201)
        payment_id = sent.get_json()['payment']['payment_id']
        self.assertEqual(sent.get_json()['payment']['status'], 'completed')
        self.assertNotIn('end_to_end_id', sent.get_json()['payment'])

        listed = self.client.post('/listPix', json={'userid': 'alice'})
        self.assertEqual(listed.get_json()['Pix']['ytd_sent'], '25.00')

        recalled = self.client.post('/recallPix', json={'userid': 'alice', 'payment_id': payment_id})
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
        added = self.client.post('/addPixKey', json=self._key_payload(
            nickname='Ally', legal_name='Blocked Person LLC',
        ))
        key_id = added.get_json()['key']['key_id']
        held = self.client.post('/sendPix', json={
            'userid': 'alice', 'key_id': key_id, 'amount': '30.00', 'trace_id': 'h1',
        })
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        payment_id = held.get_json()['payment']['payment_id']

        self._session('bob', 'customer')
        stolen = self.client.post('/recallPix', json={'userid': 'bob', 'payment_id': payment_id})
        self.assertEqual(stolen.status_code, 403)

        self._session('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Pix']['keys'][0]['nickname'], 'Ally')

        other = self.client.post('/releasePix', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(other.status_code, 403)
        self.assertEqual(other.get_json()['error'], 'ofac_hold')

        overridden = self.client.post('/overridePixOfac', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['payment']['status'], 'completed')

        exported = self.client.post('/exportPix', json={
            'userid': 'teller', 'payment_id': payment_id,
        })
        self.assertEqual(exported.status_code, 200)
        self.assertIn('PIX1|1|', exported.get_json()['export']['pix1'])
        self.assertNotIn(CPF, exported.get_json()['export']['pix1'])

    def test_night_cap_and_invalid_cpf(self):
        self._session()
        added = self.client.post('/addPixKey', json=self._key_payload())
        key_id = added.get_json()['key']['key_id']
        self.now[0] = ts(2024, 6, 14, 21, 0)
        night = self.client.post('/sendPix', json={
            'userid': 'alice', 'key_id': key_id, 'amount': '1000.01',
        })
        self.assertEqual(night.status_code, 400)
        self.assertEqual(night.get_json()['error'], 'night_amount_exceeded')
        bad = self.client.post('/addPixKey', json=self._key_payload(
            nickname='Bad', key_value='12345678901',
        ))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()['error'], 'invalid_key')


if __name__ == '__main__':
    unittest.main()
