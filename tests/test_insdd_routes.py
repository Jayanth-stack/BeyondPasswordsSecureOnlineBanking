import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.insdd import (
    EurUsdBook,
    InSddPolicy,
    InSddService,
    MemoryInSddStore,
    Target2Calendar,
    attach_insdd_routes,
    compose_creditor_identifier,
    compose_iban,
    compose_pain008,
)

CEST = timezone(timedelta(hours=2))
OUR_BIC = 'KNHADEFFXXX'
CI = compose_creditor_identifier('DE', '09999999999')
DEBTOR_IBAN = compose_iban('DE', '370400440000001001')
UMR = 'UMR-ALICE-1'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


def pain_file(scheme_id='E2ETEST0000001', amount='1250.00', **extra):
    fields = {
        'scheme_id': scheme_id,
        'amount': amount,
        'scheme': extra.get('scheme', 'core'),
        'sequence': extra.get('sequence', 'FRST'),
        'receiver_bic': OUR_BIC,
        'debtor_iban': extra.get('debtor_iban', DEBTOR_IBAN),
        'creditor_id': CI,
        'umr': extra.get('umr', UMR),
        'collection_date': extra.get('collection_date', '2024-06-14'),
        'creditor_name': extra.get('creditor_name', 'ACME CORP'),
        'debtor_name': 'ADA LOVELACE',
    }
    return compose_pain008(fields)


def build_app(service):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}},
            'Info': {'first_name': 'Ada'},
            'InSdds': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'checkin': {'Account': 1001, 'Balance': 50}},
            'Info': {'first_name': 'Ada'},
            'InSdds': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 201

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    attach_insdd_routes(app, service)
    return app


class InSddRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.credits = []
        self.debits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = InSddService(
            InSddPolicy(receiver_bic=OUR_BIC, dual_control_threshold=Decimal('10000.00')),
            MemoryInSddStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
            },
            lookup_fn=lambda account: 'alice' if str(account) in {'1001', '1002'} else None,
            calendar=Target2Calendar(cutoff_hour=16, cutoff_minute=0, tz_offset_hours=2),
            fx=EurUsdBook(Decimal('1.080000')),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()
        self.service.add_mandate(
            actor='alice', actor_type='customer', userid='alice',
            values={'umr': UMR, 'creditor_id': CI, 'scheme': 'core', 'account': '1001', 'creditor_name': 'ACME CORP'},
        )

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_anonymous_is_401(self):
        response = self.client.post('/listInSdds', json={})
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_is_403(self):
        self._login()
        response = self.client.post('/listInSdds', json={'userid': 'bob'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self._login()
        response = self.client.post('/ingestInSdd', json={'file': pain_file()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'insdd_forbidden')
        self.assertEqual(self.debits, [])

    def test_staff_ingest_and_customer_snapshot(self):
        self._login('teller', 'tier1')
        ingested = self.client.post('/ingestInSdd', json={'file': pain_file(), 'userid': 'teller'})
        self.assertEqual(ingested.status_code, 201)
        body = ingested.get_json()
        self.assertEqual(body['inbound']['status'], 'posted')
        self.assertEqual(body['inbound']['iban_masked'], 'DE****1001')
        self.assertEqual(body['inbound']['debit_usd'], '1350.00')
        self.assertNotIn('debtor_iban', body['inbound'])

        again = self.client.post('/ingestInSdd', json={'file': pain_file(), 'userid': 'teller'})
        self.assertEqual(again.status_code, 200)

        self._login('alice', 'customer')
        listed = self.client.post('/listInSdds', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        snap = listed.get_json()['InSdds']
        self.assertEqual(snap['ytd_posted'], '1350.00')
        dash = self.client.post('/loadCustomer')
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['InSdds']['posted_count'], 1)

    def test_staff_file_ingest_and_unmatched_assign(self):
        self._login('teller', 'tier1')
        unknown = compose_iban('DE', '370400440000004044')
        batch = self.client.post('/ingestInSddFile', json={
            'userid': 'teller',
            'file': pain_file(scheme_id='E2EUNK000000001', umr='UMR-MISS', debtor_iban=unknown) + pain_file(),
        })
        self.assertEqual(batch.status_code, 201)
        payload = batch.get_json()
        self.assertEqual(payload['accepted_count'], 2)
        unmatched = self.client.post('/listUnmatchedInSdds', json={'userid': 'teller'})
        self.assertEqual(unmatched.status_code, 200)
        rows = unmatched.get_json()['InSdds']['unmatched']
        self.assertEqual(len(rows), 1)
        assigned = self.client.post('/assignInSdd', json={
            'userid': 'teller',
            'inbound_id': rows[0]['inbound_id'],
            'customer_id': 'alice',
            'account': '1001',
        })
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.get_json()['inbound']['status'], 'posted')

    def test_customer_refund_and_staff_ofac_release(self):
        self._login('teller', 'tier1')
        held = self.client.post('/ingestInSdd', json={
            'userid': 'teller',
            'scheme_id': 'E2EHELD0000001',
            'amount': '200.00',
            'scheme': 'core',
            'sequence': 'FRST',
            'receiver_bic': OUR_BIC,
            'debtor_iban': DEBTOR_IBAN,
            'creditor_id': CI,
            'umr': UMR,
            'collection_date': '2024-06-14',
            'creditor_name': 'OFAC TESTNAME',
            'debtor_name': 'Ada Lovelace',
        })
        self.assertEqual(held.status_code, 201)
        inbound_id = held.get_json()['inbound']['inbound_id']
        self.assertEqual(held.get_json()['inbound']['status'], 'held')
        self.assertEqual(self.debits, [])

        denied = self.client.post('/releaseInSdd', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        overridden = self.client.post('/overrideInSddOfac', json={'userid': 'teller', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        self._login('alice', 'customer')
        refunded = self.client.post('/requestInSddRefund', json={
            'userid': 'alice', 'inbound_id': inbound_id, 'reason': 'md06',
        })
        self.assertEqual(refunded.status_code, 200)
        self.assertEqual(refunded.get_json()['inbound']['status'], 'returned')
        self.assertEqual(len(self.credits), 1)

    def test_existing_money_routes_unchanged(self):
        self._login()
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice'})
        self.assertEqual(transfer.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice'})
        self.assertEqual(wire.status_code, 201)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')
