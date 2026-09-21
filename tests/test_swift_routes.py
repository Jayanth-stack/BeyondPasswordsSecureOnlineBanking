import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.swift import (
    FxBook,
    MemorySwiftStore,
    SwiftPolicy,
    SwiftService,
    Target2Calendar,
    attach_swift_routes,
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
            'Swift': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Swift': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'domestic unchanged'}), 200

    attach_swift_routes(app, service)
    return app


class SwiftRouteTests(unittest.TestCase):
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

        self.service = SwiftService(
            SwiftPolicy(dual_control_threshold=Decimal('10000.00')),
            MemorySwiftStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=-4),
            fx=FxBook({'EUR': Decimal('1.08'), 'USD': Decimal('1')}),
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
            'nickname': 'Berlin',
            'legal_name': 'Ada Lovelace',
            'bic': 'DEUTDEFF',
            'iban': 'DE89370400440532013000',
            'street': 'Taunusanlage 12',
            'city': 'Frankfurt',
            'country': 'DE',
            'postal': '60325',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addSwiftBeneficiary', json=self._bene_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addSwiftBeneficiary', json=self._bene_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_complete_or_recall(self):
        self.login()
        added = self.client.post('/addSwiftBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('iban', body['beneficiary'])
        self.assertEqual(body['beneficiary']['iban_masked'], 'DE****3000')
        bene_id = body['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendSwift', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '40.00',
            'currency': 'EUR', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        wire_id = sent.get_json()['wire']['wire_id']
        self.assertNotIn('iban', sent.get_json()['wire'])
        completed = self.client.post('/completeSwift', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'swift_forbidden')
        recalled = self.client.post('/recallSwift', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_complete_recall_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addSwiftBeneficiary', json=self._bene_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendSwift', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '25.00',
            'currency': 'EUR', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        wire = sent.get_json()['wire']
        self.assertEqual(wire['status'], 'sent')
        self.assertEqual(wire['debit_usd'], '27.00')

        listed = self.client.post('/listSwifts', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Swift']['ytd_sent'], '27.00')

        quoted = self.client.post('/quoteSwiftFx', json={'userid': 'alice', 'amount': '100', 'currency': 'EUR'})
        self.assertEqual(quoted.status_code, 200)
        self.assertEqual(quoted.get_json()['quote']['debit_usd'], '108.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Swift']['beneficiaries'][0]['nickname'], 'Ally')

        completed = self.client.post('/completeSwift', json={
            'userid': 'teller', 'wire_id': wire['wire_id'],
        })
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['wire']['status'], 'completed')

        other = self.client.post('/sendSwift', json={
            'userid': 'teller', 'customer_id': 'alice', 'beneficiary_id': bene_id,
            'amount': '12.00', 'currency': 'EUR', 'trace_id': 'in-2',
        })
        self.assertEqual(other.status_code, 201)
        recalled = self.client.post('/recallSwift', json={
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
        domestic = self.client.post('/sendWire', json={'userid': 'teller'})
        self.assertEqual(domestic.status_code, 200)
        self.assertEqual(domestic.get_json()['message'], 'domestic unchanged')

    def test_bad_iban_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addSwiftBeneficiary', json=self._bene_payload(iban='DE89370400440532013001'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_iban')
        added = self.client.post('/addSwiftBeneficiary', json=self._bene_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendSwift', json={
            'userid': 'alice',
            'beneficiary_id': added.get_json()['beneficiary']['beneficiary_id'],
            'amount': '40.00',
            'currency': 'EUR',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['wire']['status'], 'held')
        overridden = self.client.post('/overrideSwiftOfac', json={
            'userid': 'alice', 'wire_id': held.get_json()['wire']['wire_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        self.login()
        added = self.client.post('/addSwiftBeneficiary', json=self._bene_payload(nickname='Wells'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        queued = self.client.post('/sendSwift', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '33.00', 'currency': 'EUR',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['wire']['status'], 'queued')
        wire_id = queued.get_json()['wire']['wire_id']
        denied = self.client.post('/releaseSwift', json={'userid': 'alice', 'wire_id': wire_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectSwift', json={'userid': 'teller', 'wire_id': wire_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['wire']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
