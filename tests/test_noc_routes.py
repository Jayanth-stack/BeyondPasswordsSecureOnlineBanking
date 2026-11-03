import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.noc import (
    AchClock,
    MemoryNocStore,
    NocPolicy,
    NocService,
    attach_noc_routes,
)

ET = timezone(timedelta(hours=-4))
ABA = '021000021'
DEST = '555566667777'


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
            'AchNoc': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'AchNoc': service.snapshot(values.get('customer_id'), actor=session.get('userid'), actor_type=session.get('usertype') or 'employee'),
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

    attach_noc_routes(app, service)
    return app


class NocRouteTests(unittest.TestCase):
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

        self.service = NocService(
            NocPolicy(fee_amount=Decimal('0.20')),
            MemoryNocStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=AchClock(tz_offset_hours=-4),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _key_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Payroll dest',
            'legal_name': 'Ada Lovelace',
            'dest_aba': ABA,
            'dest_account': DEST,
            'funding_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addAchNocKey', json=self._key_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addAchNocKey', json=self._key_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_reverse_as_staff_or_ingest(self):
        self.login()
        added = self.client.post('/addAchNocKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('fingerprint', body['key'])
        self.assertNotIn('dest_account', body['key'])
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestAchNoc', json={
            'userid': 'teller', 'trace': '021000010000001', 'account': DEST,
            'aba': ABA, 'change_code': 'C01', 'corrected': '888899990000',
            'rdfi_name': 'Chase Bank',
        })
        self.assertEqual(ingested.status_code, 201)
        notice_id = ingested.get_json()['notice']['notice_id']
        self.assertNotIn('trace_id', ingested.get_json()['notice'])
        self.login()
        applied = self.client.post('/applyAchNoc', json={
            'userid': 'alice', 'notice_id': notice_id,
        })
        self.assertEqual(applied.status_code, 201)
        event_id = applied.get_json()['notice']['notice_id']
        staff_reverse = self.client.post('/reverseAchNoc', json={'userid': 'alice', 'notice_id': event_id})
        self.assertEqual(staff_reverse.status_code, 403)
        self.assertEqual(staff_reverse.get_json()['error'], 'noc_forbidden')
        ingest = self.client.post('/ingestAchNoc', json={
            'userid': 'alice', 'trace': '021000010000002', 'account': DEST, 'aba': ABA,
            'corrected': '111122223333',
        })
        self.assertEqual(ingest.status_code, 403)

    def test_staff_release_reverse_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addAchNocKey', json=self._key_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        pending = self.client.post('/ingestAchNoc', json={
            'userid': 'teller', 'trace': '021000010000099', 'account': DEST,
            'aba': ABA, 'change_code': 'C02', 'corrected': '011401533',
            'rdfi_name': 'Chase Bank',
        })
        self.assertEqual(pending.status_code, 201)
        notice = pending.get_json()['notice']
        self.assertEqual(notice['status'], 'open')

        self.login()
        applied = self.client.post('/applyAchNoc', json={
            'userid': 'alice', 'notice_id': notice['notice_id'],
        })
        self.assertEqual(applied.status_code, 201)
        self.assertEqual(applied.get_json()['notice']['status'], 'pending_release')

        listed = self.client.post('/listAchNoc', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['AchNoc']['open_count'], 1)

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['AchNoc']['keys'][0]['nickname'], 'Ally')

        released = self.client.post('/releaseAchNoc', json={
            'userid': 'teller', 'notice_id': notice['notice_id'],
        })
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()['notice']['status'], 'posted')
        self.assertEqual(released.get_json()['notice']['message_type'], 'CORA')

        reversed_row = self.client.post('/reverseAchNoc', json={
            'userid': 'teller', 'notice_id': notice['notice_id'],
        })
        self.assertEqual(reversed_row.status_code, 200)
        self.assertEqual(reversed_row.get_json()['notice']['status'], 'reversed')

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

    def test_bad_aba_400_and_ofac_hold(self):
        self.login()
        added = self.client.post('/addAchNocKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        missing = self.client.post('/ingestAchNoc', json={
            'userid': 'teller', 'trace': '021000010000021', 'account': DEST,
            'aba': '021000022', 'change_code': 'C01', 'corrected': '888899990000',
        })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_aba')
        held = self.client.post('/ingestAchNoc', json={
            'userid': 'teller', 'trace': '021000010000031', 'account': DEST,
            'aba': ABA, 'change_code': 'C01', 'corrected': '888899990000',
            'rdfi_name': 'Blocked Person',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['notice']['status'], 'held')
        self.login()
        overridden = self.client.post('/overrideAchNocOfac', json={
            'userid': 'alice', 'notice_id': held.get_json()['notice']['notice_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_ingest_file_and_customer_cannot_export(self):
        self.login('teller', 'tier2')
        corr = self.client.post('/addAchNocCorrespondent', json={
            'userid': 'teller', 'nickname': 'Chase', 'legal_name': 'Chase Bank',
            'aba': ABA,
        })
        self.assertEqual(corr.status_code, 201)
        self.assertNotIn('aba', corr.get_json()['correspondent'])
        ingested = self.client.post('/ingestAchNocFile', json={
            'userid': 'teller',
            'file': 'NOC1|COR|021000010000041|C01|021000021|999988887777|121234345656|Chase Bank|PPD|22|25.00|unmatched',
        })
        self.assertEqual(ingested.status_code, 201)
        self.assertEqual(ingested.get_json()['notices'][0]['status'], 'unmatched')
        self.login()
        denied = self.client.post('/exportAchNoc', json={'userid': 'alice'})
        self.assertEqual(denied.status_code, 403)

    def test_customer_can_refuse_without_debit(self):
        self.login()
        added = self.client.post('/addAchNocKey', json=self._key_payload())
        self.assertEqual(added.status_code, 201)
        self.login('teller', 'tier1')
        ingested = self.client.post('/ingestAchNoc', json={
            'userid': 'teller', 'trace': '021000010000051', 'account': DEST,
            'aba': ABA, 'change_code': 'C01', 'corrected': '555500001111',
            'rdfi_name': 'Chase Bank',
        })
        notice_id = ingested.get_json()['notice']['notice_id']
        self.login()
        refused = self.client.post('/refuseAchNoc', json={
            'userid': 'alice', 'notice_id': notice_id, 'refuse': 'C65',
        })
        self.assertEqual(refused.status_code, 201)
        body = refused.get_json()['notice']
        self.assertEqual(body['status'], 'refused')
        self.assertEqual(body['message_type'], 'CORR')
        self.assertEqual(self.debits, [])


if __name__ == '__main__':
    unittest.main()
