import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.lockbox import (
    LockboxPolicy,
    LockboxService,
    MemoryLockboxStore,
    attach_lockbox_routes,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))

SAMPLE = (
    "01,LOCKBOX1,021000021,240614,1000,000001,,,2/\n"
    "02,LOCKBOX1,1,240614,1000,USD,2/\n"
    "03,1234567,USD,072,0000004000,1,V/\n"
    "16,174,0000004000,V,202406140000099,INV-9,ACME CORP/\n"
    "88,SERIAL 99/\n"
    "49,0000004000,3/\n"
    "98,0000004000,1,5/\n"
    "99,0000004000,1,7/\n"
)


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
            'Lockboxes': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Lockboxes': service.snapshot(values.get('customer_id')),
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

    attach_lockbox_routes(app, service)
    return app


class LockboxRouteTests(unittest.TestCase):
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

        self.service = LockboxService(
            LockboxPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryLockboxStore(),
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

    def _enroll_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'AR Box',
            'lockbox_id': '1234567',
            'credit_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_enroll_401(self):
        response = self.client.post('/enrollLockbox', json=self._enroll_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/enrollLockbox', json=self._enroll_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest_or_release(self):
        self.login()
        added = self.client.post('/enrollLockbox', json=self._enroll_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('credit_account', body['enrollment'])
        ingested = self.client.post('/ingestLockbox', json={
            'userid': 'alice', 'lockbox_id': '1234567', 'amount': '40.00',
            'remitter_name': 'Acme',
        })
        self.assertEqual(ingested.status_code, 403)
        self.assertEqual(ingested.get_json()['error'], 'lockbox_forbidden')

    def test_staff_ingest_file_return_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/enrollLockbox', json=self._enroll_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)

        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestLockbox', json={
            'userid': 'teller', 'lockbox_id': '1234567', 'amount': '25.00',
            'remitter_name': 'Acme Corp', 'bank_ref': '202406140000021',
        })
        self.assertEqual(ingested.status_code, 201)
        item = ingested.get_json()['item']
        self.assertEqual(item['status'], 'posted')
        self.assertNotIn('credit_account', item)
        self.assertNotIn('bank_ref', item)

        listed = self.client.post('/listLockboxes', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Lockboxes']['ytd_posted'], '25.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Lockboxes']['enrollments'][0]['nickname'], 'Ally')

        filed = self.client.post('/ingestLockboxFile', json={'userid': 'teller', 'file': SAMPLE})
        self.assertIn(filed.status_code, (200, 201))

        self.login('alice', 'customer')
        requested = self.client.post('/requestLockboxReturn', json={
            'userid': 'alice', 'item_id': item['item_id'],
        })
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.get_json()['item']['status'], 'returned')

        transfer = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '1002', 'amount': '1',
        })
        self.assertEqual(transfer.status_code, 200)
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        withdraw = self.client.post('/withdrawAmount', json={
            'userid': 'alice', 'account': '1001', 'amount': '1',
        })
        self.assertEqual(withdraw.status_code, 200)
        self.assertEqual(withdraw.get_json()['message'], 'Amount Debited')
        wire = self.client.post('/sendWire', json={'userid': 'alice', 'amount': '1'})
        self.assertEqual(wire.status_code, 200)

    def test_bad_lockbox_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/enrollLockbox', json=self._enroll_payload(lockbox_id='0000000'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_lockbox')
        added = self.client.post('/enrollLockbox', json=self._enroll_payload())
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        held = self.client.post('/ingestLockbox', json={
            'userid': 'teller', 'lockbox_id': '1234567', 'amount': '40.00',
            'remitter_name': 'Blocked Person',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['item']['status'], 'held')
        overridden = self.client.post('/overrideLockboxOfac', json={
            'userid': 'alice', 'item_id': held.get_json()['item']['item_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release(self):
        self.now[0] = ts(2024, 6, 14, 15, 0)
        self.login()
        added = self.client.post('/enrollLockbox', json=self._enroll_payload(nickname='Wells'))
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier2')
        queued = self.client.post('/ingestLockbox', json={
            'userid': 'teller', 'lockbox_id': '1234567', 'amount': '33.00',
            'remitter_name': 'Acme',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['item']['status'], 'queued')
        item_id = queued.get_json()['item']['item_id']
        self.login('alice', 'customer')
        denied = self.client.post('/releaseLockbox', json={'userid': 'alice', 'item_id': item_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectLockbox', json={'userid': 'teller', 'item_id': item_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['item']['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
