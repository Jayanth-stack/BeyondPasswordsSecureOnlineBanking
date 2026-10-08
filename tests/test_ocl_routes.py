import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.ocl import (
    MemoryOclStore,
    OclPolicy,
    OclService,
    attach_ocl_routes,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))
PAYOR = '026009593'


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
            'Ocls': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Ocls': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/depositCheck', methods=['POST'])
    def deposit_check():
        return jsonify({'message': 'Success'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 200

    attach_ocl_routes(app, service)
    return app


class OclRouteTests(unittest.TestCase):
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

        self.service = OclService(
            OclPolicy(
                outbound_fee=Decimal('1.00'),
                dual_control_threshold=Decimal('10000.00'),
                bofd_aba='021000021',
                cutoff_hour=14,
            ),
            MemoryOclStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _profile_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Payroll',
            'payee_name': 'Ada Lovelace',
            'payor_aba': PAYOR,
            'drawer_account': '77881234',
            'serial': '1001',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addOclProfile', json=self._profile_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addOclProfile', json=self._profile_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_complete_or_recall(self):
        self.login()
        added = self.client.post('/addOclProfile', json=self._profile_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('drawer_account', body['profile'])
        profile_id = body['profile']['profile_id']
        sent = self.client.post('/sendOcl', json={
            'userid': 'alice', 'profile_id': profile_id, 'amount': '40.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        outbound_id = sent.get_json()['outbound']['outbound_id']
        self.assertNotIn('drawer_account', sent.get_json()['outbound'])
        self.assertNotIn('ece', sent.get_json()['outbound'])
        self.assertNotIn('image_fingerprint', sent.get_json()['outbound'])
        completed = self.client.post('/completeOcl', json={'userid': 'alice', 'outbound_id': outbound_id})
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()['error'], 'ocl_forbidden')
        recalled = self.client.post('/recallOcl', json={'userid': 'alice', 'outbound_id': outbound_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_complete_return_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addOclProfile', json=self._profile_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        profile_id = added.get_json()['profile']['profile_id']
        sent = self.client.post('/sendOcl', json={
            'userid': 'alice', 'profile_id': profile_id, 'amount': '25.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        item = sent.get_json()['outbound']
        self.assertEqual(item['status'], 'submitted')
        self.assertEqual(item['amount'], '25.00')

        listed = self.client.post('/listOcls', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Ocls']['ytd_posted'], '25.00')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Ocls']['profiles'][0]['nickname'], 'Ally')

        completed = self.client.post('/completeOcl', json={
            'userid': 'teller', 'outbound_id': item['outbound_id'],
        })
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['outbound']['status'], 'completed')

        other_profile = self.client.post('/addOclProfile', json=self._profile_payload(
            userid='teller', customer_id='alice', nickname='Rent', serial='2002',
        ))
        self.assertEqual(other_profile.status_code, 201)
        other = self.client.post('/sendOcl', json={
            'userid': 'teller', 'customer_id': 'alice',
            'profile_id': other_profile.get_json()['profile']['profile_id'],
            'amount': '12.00', 'trace_id': 'in-2',
        })
        self.assertEqual(other.status_code, 201)
        recalled = self.client.post('/recallOcl', json={
            'userid': 'teller', 'outbound_id': other.get_json()['outbound']['outbound_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['outbound']['status'], 'recalled')

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
        cheque = self.client.post('/depositCheck', json={
            'userid': 'teller', 'cheque_no': 7,
        })
        self.assertEqual(cheque.status_code, 200)
        self.assertEqual(cheque.get_json()['message'], 'Success')
        wire = self.client.post('/sendWire', json={'userid': 'teller', 'amount': '1'})
        self.assertEqual(wire.status_code, 200)

    def test_on_us_aba_403_and_ofac_hold(self):
        self.login()
        onus = self.client.post('/addOclProfile', json=self._profile_payload(payor_aba='021000021'))
        self.assertEqual(onus.status_code, 403)
        self.assertEqual(onus.get_json()['error'], 'on_us_not_allowed')
        missing = self.client.post('/addOclProfile', json=self._profile_payload(payor_aba='021000022'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_aba')
        added = self.client.post('/addOclProfile', json=self._profile_payload(
            nickname='Blocked', payee_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendOcl', json={
            'userid': 'alice',
            'profile_id': added.get_json()['profile']['profile_id'],
            'amount': '40.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['outbound']['status'], 'held')
        overridden = self.client.post('/overrideOclOfac', json={
            'userid': 'alice', 'outbound_id': held.get_json()['outbound']['outbound_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release_or_export(self):
        self.now[0] = ts(2024, 6, 14, 15, 0)
        self.login()
        added = self.client.post('/addOclProfile', json=self._profile_payload(nickname='Wells'))
        self.assertEqual(added.status_code, 201)
        profile_id = added.get_json()['profile']['profile_id']
        queued = self.client.post('/sendOcl', json={
            'userid': 'alice', 'profile_id': profile_id, 'amount': '33.00',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['outbound']['status'], 'queued')
        outbound_id = queued.get_json()['outbound']['outbound_id']
        denied = self.client.post('/releaseOcl', json={'userid': 'alice', 'outbound_id': outbound_id})
        self.assertEqual(denied.status_code, 403)
        exported = self.client.post('/exportOcl', json={'userid': 'alice'})
        self.assertEqual(exported.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectOcl', json={'userid': 'teller', 'outbound_id': outbound_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['outbound']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
