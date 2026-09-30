import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.insepa import (
    EurUsdBook,
    InSepaPolicy,
    InSepaService,
    MemoryInSepaStore,
    Target2Calendar,
    attach_insepa_routes,
    compose_iban,
    compose_pacs008,
)

CEST = timezone(timedelta(hours=2))
OUR_BIC = 'KNHADEFFXXX'
SENDER_BIC = 'COBADEFFXXX'
SCHEME_A = 'INST20240615KH0001'
BENEFICIARY_IBAN = compose_iban('DE', '370400440000001001')
ORIGINATOR_IBAN = 'DE89370400440532013000'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


def pacs_message(**overrides):
    fields = {
        'scheme': 'sct_inst',
        'scheme_id': SCHEME_A,
        'end_to_end_id': 'E2EINST0001',
        'msg_id': 'MSGINST0001',
        'amount': '40.00',
        'sender_bic': SENDER_BIC,
        'receiver_bic': OUR_BIC,
        'iban': BENEFICIARY_IBAN,
        'originator_iban': ORIGINATOR_IBAN,
        'originator_name': 'ACME GMBH',
        'beneficiary_name': 'ADA LOVELACE',
    }
    fields.update(overrides)
    return compose_pacs008(fields)


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
            'InSepas': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'InSepas': service.snapshot(values.get('customer_id')),
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

    attach_insepa_routes(app, service)
    return app


class InSepaRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 15, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        self.service = InSepaService(
            InSepaPolicy(
                receiver_bic=OUR_BIC,
                dual_control_threshold=Decimal('10000.00'),
                fx_rate=Decimal('1.080000'),
            ),
            MemoryInSepaStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            fx_book=EurUsdBook(Decimal('1.080000')),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_unauthenticated_ingest_401(self):
        response = self.client.post('/ingestInSepa', json={'file': pacs_message()})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/ingestInSepa', json={'userid': 'bob', 'file': pacs_message()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self.login()
        response = self.client.post('/ingestInSepa', json={'userid': 'alice', 'file': pacs_message()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'insepa_forbidden')

    def test_staff_ingest_file_batch_and_existing_money_routes_unchanged(self):
        self.login('teller', 'tier1')
        first = pacs_message()
        second = pacs_message(
            scheme_id='INST20240615KH0002', amount='10.00',
            end_to_end_id='E2EINST0002', msg_id='MSGINST0002',
        )
        ingested = self.client.post('/ingestInSepaFile', json={
            'userid': 'teller', 'file': first + '\n' + second,
        })
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['accepted_count'], 2)
        self.assertNotIn('beneficiary_account', body['accepted'][0])
        self.assertEqual(body['accepted'][0]['status'], 'posted')
        self.assertEqual(body['accepted'][0]['iban_masked'], 'DE****1001')

        listed = self.client.post('/listInSepas', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['InSepas']['posted_count'], 2)

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['InSepas']['inbounds'][0]['originator_name'], 'ACME GMBH')

        duplicate = self.client.post('/ingestInSepa', json={'userid': 'teller', 'file': first})
        self.assertEqual(duplicate.status_code, 200)

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

    def test_json_iban_maps_to_internal_account(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestInSepa', json={
            'userid': 'teller',
            'scheme': 'sct_inst',
            'scheme_id': 'INSTJSONMAP0001',
            'end_to_end_id': 'E2EJSONMAP1',
            'amount': '20.00',
            'sender_bic': SENDER_BIC,
            'receiver_bic': OUR_BIC,
            'iban': BENEFICIARY_IBAN,
            'originator_name': 'ACME GMBH',
        })
        self.assertEqual(ingested.status_code, 201)
        inbound = ingested.get_json()['inbound']
        self.assertEqual(inbound['userid'], 'alice')
        self.assertEqual(inbound['iban_masked'], 'DE****1001')
        self.assertEqual(inbound['beneficiary_last4'], '1001')
        self.assertNotIn('iban', inbound)

    def test_customer_return_and_quote(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestInSepa', json={'userid': 'teller', 'file': pacs_message()})
        self.assertEqual(ingested.status_code, 201)
        inbound_id = ingested.get_json()['inbound']['inbound_id']
        self.login('alice', 'customer')
        quoted = self.client.post('/quoteInSepaFx', json={'userid': 'alice', 'amount': '40.00'})
        self.assertEqual(quoted.status_code, 200)
        self.assertEqual(quoted.get_json()['fx']['amount_usd'], '43.20')
        returned = self.client.post('/requestInSepaReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id, 'reason': 'cust',
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['inbound']['status'], 'returned')


if __name__ == '__main__':
    unittest.main()
