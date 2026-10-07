import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.icl import (
    IclPolicy,
    IclService,
    MemoryIclStore,
    attach_icl_routes,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


SAMPLE_FILE = """01|FileHeader|20240614|011000015|021000021|CL01
10|CashLetter|CL01|20240614|021000021
20|Bundle|B1
25|Check|202406140000001||021000021|1001|4321|0000010000|011000015|Ada Lovelace
50|Image|Y|Y|
70|BundleControl|1|0000010000
90|CashLetterControl|1|0000010000
99|FileControl|1|1|0000010000
"""


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
            'Icls': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Icls': service.snapshot(values.get('customer_id')),
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

    @app.route('/depositCheck', methods=['POST'])
    def deposit_check():
        return jsonify({'message': 'done'}), 200

    attach_icl_routes(app, service)
    return app


class IclRouteTests(unittest.TestCase):
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

        self.service = IclService(
            IclPolicy(dual_control_threshold=Decimal('10000.00'), cutoff_hour=14),
            MemoryIclStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            lookup_fn=lambda account: 'alice' if str(account) in {'1001', '1002'} else None,
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _payload(self, **overrides):
        body = {
            'userid': 'teller',
            'ece': '202406140000001',
            'payor_aba': '021000021',
            'bofd_aba': '011000015',
            'drawer_account': '1001',
            'serial': '4321',
            'amount': '100.00',
            'payee_name': 'Ada Lovelace',
        }
        body.update(overrides)
        return body

    def test_unauthenticated_ingest_401(self):
        response = self.client.post('/ingestIcl', json=self._payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login('teller', 'tier1')
        response = self.client.post('/ingestIcl', json=self._payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_ingest(self):
        self.login()
        response = self.client.post('/ingestIcl', json=self._payload(userid='alice'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'icl_forbidden')

    def test_staff_ingest_201_then_200_and_masks(self):
        self.login('teller', 'tier1')
        first = self.client.post('/ingestIcl', json=self._payload())
        self.assertEqual(first.status_code, 201)
        body = first.get_json()
        self.assertEqual(body['inbound']['status'], 'posted')
        self.assertNotIn('drawer_account', body['inbound'])
        self.assertNotIn('image_fingerprint', body['inbound'])
        self.assertEqual(body['inbound']['drawer_last4'], '1001')
        second = self.client.post('/ingestIcl', json=self._payload())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()['inbound']['inbound_id'], body['inbound']['inbound_id'])

        listed = self.client.post('/listIcls', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Icls']['ytd_posted'], '100.00')

        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Icls']['inbounds'][0]['payee_name'], 'Ada Lovelace')

        self.login()
        dash = self.client.post('/loadCustomer')
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()['Icls']['posted_count'], 1)

    def test_staff_ingest_file_and_existing_money_routes_unchanged(self):
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestIclFile', json={'userid': 'teller', 'file': SAMPLE_FILE})
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['accepted_count'], 1)

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
        cheque = self.client.post('/depositCheck', json={'userid': 'teller', 'cheque_no': 1})
        self.assertEqual(cheque.status_code, 200)
        self.assertEqual(cheque.get_json()['message'], 'done')

    def test_xml_file_400(self):
        self.login('teller', 'tier1')
        response = self.client.post('/ingestIclFile', json={
            'userid': 'teller', 'file': '<!DOCTYPE icl><icl/>',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_file')

    def test_wrong_receiver_400(self):
        self.login('teller', 'tier1')
        response = self.client.post('/ingestIcl', json=self._payload(payor_aba='011000015'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'wrong_receiver')

    def test_ofac_override_release_reject_return(self):
        self.login('teller', 'tier1')
        held = self.client.post('/ingestIcl', json=self._payload(
            payee_name='Blocked Person', ece='202406140000010',
        ))
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['inbound']['status'], 'held')
        inbound_id = held.get_json()['inbound']['inbound_id']
        with self.client.session_transaction() as sess:
            sess.clear()
        unauth = self.client.post('/overrideIclOfac', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(unauth.status_code, 401)
        self.login()
        denied = self.client.post('/overrideIclOfac', json={'userid': 'alice', 'inbound_id': inbound_id})
        self.assertEqual(denied.status_code, 403)

        self.login('boss', 'tier2')
        overridden = self.client.post('/overrideIclOfac', json={'userid': 'boss', 'inbound_id': inbound_id})
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()['inbound']['status'], 'posted')

        pending = self.client.post('/ingestIcl', json=self._payload(
            userid='boss', amount='15000.00', ece='202406140000011',
        ))
        self.assertEqual(pending.get_json()['inbound']['status'], 'pending_release')
        pending_id = pending.get_json()['inbound']['inbound_id']
        same = self.client.post('/releaseIcl', json={'userid': 'boss', 'inbound_id': pending_id})
        self.assertEqual(same.status_code, 403)
        self.assertEqual(same.get_json()['error'], 'same_approver')
        self.login('teller', 'tier1')
        released = self.client.post('/releaseIcl', json={'userid': 'teller', 'inbound_id': pending_id})
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()['inbound']['status'], 'posted')

        queued_now = ts(2024, 6, 14, 15, 0)
        self.now[0] = queued_now
        queued = self.client.post('/ingestIcl', json=self._payload(
            userid='teller', ece='202406140000012',
        ))
        self.assertEqual(queued.get_json()['inbound']['status'], 'queued')
        rejected = self.client.post('/rejectIcl', json={
            'userid': 'teller', 'inbound_id': queued.get_json()['inbound']['inbound_id'],
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['inbound']['status'], 'rejected')

        self.now[0] = ts(2024, 6, 14, 11, 0)
        posted = self.client.post('/ingestIcl', json=self._payload(
            userid='teller', ece='202406140000013',
        ))
        inbound_id = posted.get_json()['inbound']['inbound_id']
        self.login()
        returned = self.client.post('/requestIclReturn', json={
            'userid': 'alice', 'inbound_id': inbound_id, 'reason': 'forged',
        })
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['inbound']['status'], 'returned')
        self.assertEqual(returned.get_json()['inbound']['return_reason'], 'forged')

    def test_missing_file_400(self):
        self.login('teller', 'tier1')
        response = self.client.post('/ingestIclFile', json={'userid': 'teller'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'missing_file')


if __name__ == '__main__':
    unittest.main()
