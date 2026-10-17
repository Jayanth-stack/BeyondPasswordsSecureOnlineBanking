import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.gpi import (
    GpiPolicy,
    GpiService,
    MemoryGpiStore,
    attach_gpi_routes,
    compose_gpi,
    Target2Calendar,
)

ET = timezone(timedelta(hours=-4))
SAMPLE_UETR = 'A1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
SAMPLE_TRN = 'GPI20240614000001'
SAMPLE_BIC = 'DEUTDEFFXXX'


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
            'Gpi': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Gpi': service.snapshot(values.get('customer_id')),
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

    attach_gpi_routes(app, service)
    return app


class GpiRouteTests(unittest.TestCase):
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

        self.service = GpiService(
            GpiPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryGpiStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _enroll_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Payroll',
            'direction': 'outbound',
            'rail': 'gpi',
            'amount': '25.00',
            'account': '1001',
            'counterparty_name': 'Ada Lovelace',
            'counterparty_bic': SAMPLE_BIC,
            'uetr': SAMPLE_UETR,
            'trn': SAMPLE_TRN,
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_enroll_401(self):
        response = self.client.post('/enrollGpi', json=self._enroll_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/enrollGpi', json=self._enroll_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self.login()
        added = self.client.post('/enrollGpi', json=self._enroll_payload())
        self.assertEqual(added.status_code, 201)
        self.assertNotIn('uetr', added.get_json()['enrollment'])
        ingested = self.client.post('/ingestGpi', json={
            'userid': 'alice',
            'message_id': 'TRCK20240614000001',
            'uetr': SAMPLE_UETR,
            'trn': SAMPLE_TRN,
            'status': 'ACSC',
            'amount': '25.00',
            'rail': 'gpi',
            'name': 'Ada Lovelace',
            'bic': SAMPLE_BIC,
        })
        self.assertEqual(ingested.status_code, 403)
        self.assertEqual(ingested.get_json()['error'], 'gpi_forbidden')

    def test_staff_ingest_file_query_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/enrollGpi', json=self._enroll_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)

        self.login('teller', 'tier1')
        record = compose_gpi(
            message_id='TRCK20240614000001', uetr=SAMPLE_UETR, trn=SAMPLE_TRN,
            status='ACSC', amount=Decimal('25.00'), rail='gpi', party_name='Ada Lovelace',
            party_bic=SAMPLE_BIC, cycle_date='20240614',
        )
        ingested = self.client.post('/ingestGpiFile', json={
            'userid': 'teller', 'file': record,
        })
        self.assertEqual(ingested.status_code, 201)
        case = ingested.get_json()['case']
        self.assertEqual(case['status'], 'settled')
        self.assertNotIn('uetr', case)

        listed = self.client.post('/listGpis', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Gpi']['ytd_tracked'], '25.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Gpi']['enrollments'][0]['nickname'], 'Ally')

        self.login()
        queried = self.client.post('/requestGpiQuery', json={
            'userid': 'alice', 'case_id': case['case_id'],
        })
        self.assertEqual(queried.status_code, 200)
        self.assertEqual(queried.get_json()['case']['status'], 'query_requested')

        self.login('teller', 'tier1')
        accepted = self.client.post('/acceptGpiQuery', json={
            'userid': 'teller', 'case_id': case['case_id'],
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()['case']['status'], 'resolved_accepted')
        self.assertEqual(self.credits[0][1], '25.00')

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
        wire = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(wire.status_code, 200)
        self.assertEqual(wire.get_json()['message'], 'Wire originated')

    def test_xml_file_rejected_and_duplicate_nickname(self):
        self.login('teller', 'tier1')
        xml = self.client.post('/ingestGpiFile', json={
            'userid': 'teller', 'file': '<?xml version="1.0"?><pacs:Pacs002/>',
        })
        self.assertEqual(xml.status_code, 400)
        self.assertEqual(xml.get_json()['error'], 'invalid_file')
        self.login()
        first = self.client.post('/enrollGpi', json=self._enroll_payload())
        self.assertEqual(first.status_code, 201)
        dup = self.client.post('/enrollGpi', json=self._enroll_payload(
            uetr='B1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6',
            trn='GPI20240614000002',
        ))
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(dup.get_json()['error'], 'enrollment_duplicate')


if __name__ == '__main__':
    unittest.main()
