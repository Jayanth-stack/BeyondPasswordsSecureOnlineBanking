import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.chips import (
    ChipsCalendar,
    ChipsPolicy,
    ChipsService,
    MemoryChipsStore,
    attach_chips_routes,
)
from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WirePolicy,
    WireService,
    attach_wire_routes,
)

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def build_app(chips, wires=None):
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
            'Chips': chips.snapshot(session['userid']),
            'Wires': wires.snapshot(session['userid']) if wires is not None else {'enabled': False, 'beneficiaries': [], 'wires': []},
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Chips': chips.snapshot(values.get('customer_id')),
            'Wires': wires.snapshot(values.get('customer_id')) if wires is not None else {'enabled': False},
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    attach_chips_routes(app, chips)
    if wires is not None:
        attach_wire_routes(app, wires)
    return app


class ChipsRouteTests(unittest.TestCase):
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

        accounts = lambda userid: {
            'checkin': {'Account': 1001, 'Balance': 50},
            'savings': {'Account': 1002, 'Balance': 10},
            'credit': 'None',
        }
        self.service = ChipsService(
            ChipsPolicy(
                outbound_fee=Decimal('8.00'),
                dual_control_threshold=Decimal('10000.00'),
                min_amount=Decimal('100.00'),
            ),
            MemoryChipsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts,
            calendar=ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4),
        )
        self.wires = WireService(
            WirePolicy(outbound_fee=Decimal('25.00'), min_amount=Decimal('10.00')),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts,
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )
        self.app = build_app(self.service, self.wires)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _bene_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Citi',
            'legal_name': 'Ada Lovelace',
            'participant': '0002',
            'uid': '123456',
            'account_number': '77881234',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addChipsBeneficiary', json=self._bene_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addChipsBeneficiary', json=self._bene_payload(userid='bob'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_customer_cannot_settle_or_recall(self):
        self.login()
        added = self.client.post('/addChipsBeneficiary', json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        body = added.get_json()
        self.assertNotIn('uid', body['beneficiary'])
        self.assertNotIn('account_number', body['beneficiary'])
        bene_id = body['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendChips', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '150.00', 'trace_id': 'p1',
        })
        self.assertEqual(sent.status_code, 201)
        payment_id = sent.get_json()['payment']['payment_id']
        self.assertEqual(sent.get_json()['payment']['status'], 'released')
        self.assertNotIn('chips_seq', sent.get_json()['payment'])
        settled = self.client.post('/settleChips', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(settled.status_code, 403)
        self.assertEqual(settled.get_json()['error'], 'chips_forbidden')
        recalled = self.client.post('/recallChips', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(recalled.status_code, 403)

    def test_staff_settle_recall_and_existing_money_routes_unchanged(self):
        self.login()
        added = self.client.post('/addChipsBeneficiary', json=self._bene_payload(nickname='Ally'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendChips', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '150.00', 'trace_id': 'in-1',
        })
        self.assertEqual(sent.status_code, 201)
        payment = sent.get_json()['payment']
        self.assertEqual(payment['status'], 'released')
        self.assertEqual(payment['amount'], '150.00')

        listed = self.client.post('/listChips', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Chips']['ytd_sent'], '150.00')
        self.assertEqual(listed.get_json()['Chips']['settlement_method'], 'chips_net')

        self.login('teller', 'tier1')
        lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['Chips']['beneficiaries'][0]['nickname'], 'Ally')

        settled = self.client.post('/settleChips', json={
            'userid': 'teller', 'payment_id': payment['payment_id'],
        })
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.get_json()['payment']['status'], 'settled')

        other = self.client.post('/sendChips', json={
            'userid': 'teller', 'customer_id': 'alice', 'beneficiary_id': bene_id,
            'amount': '120.00', 'trace_id': 'in-2',
        })
        self.assertEqual(other.status_code, 201)
        recalled = self.client.post('/recallChips', json={
            'userid': 'teller', 'payment_id': other.get_json()['payment']['payment_id'],
        })
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()['payment']['status'], 'recalled')

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

    def test_fedwire_routes_still_independent(self):
        self.login()
        added = self.client.post('/addWireBeneficiary', json={
            'userid': 'alice', 'nickname': 'Chase', 'legal_name': 'Ada Lovelace',
            'aba': '021000021', 'account_number': '77881234',
            'street': '1 Federal St', 'city': 'New York', 'state': 'NY', 'postal': '10004',
            'default_account': '1001',
        })
        self.assertEqual(added.status_code, 201)
        sent = self.client.post('/sendWire', json={
            'userid': 'alice',
            'beneficiary_id': added.get_json()['beneficiary']['beneficiary_id'],
            'amount': '40.00',
            'trace_id': 'wire-keep',
        })
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()['wire']['status'], 'sent')
        chips = self.client.post('/listChips', json={'userid': 'alice'})
        self.assertEqual(chips.get_json()['Chips']['ytd_sent'], '0.00')

    def test_bad_participant_400_and_ofac_hold(self):
        self.login()
        missing = self.client.post('/addChipsBeneficiary', json=self._bene_payload(participant='0000'))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()['error'], 'invalid_participant')
        added = self.client.post('/addChipsBeneficiary', json=self._bene_payload(
            nickname='Blocked', legal_name='Blocked Person',
        ))
        self.assertEqual(added.status_code, 201)
        held = self.client.post('/sendChips', json={
            'userid': 'alice',
            'beneficiary_id': added.get_json()['beneficiary']['beneficiary_id'],
            'amount': '150.00',
        })
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()['payment']['status'], 'held')
        overridden = self.client.post('/overrideChipsOfac', json={
            'userid': 'alice', 'payment_id': held.get_json()['payment']['payment_id'],
        })
        self.assertEqual(overridden.status_code, 403)

    def test_staff_reject_queued_customer_cannot_release(self):
        self.now[0] = ts(2024, 6, 14, 8, 0)
        self.login()
        added = self.client.post('/addChipsBeneficiary', json=self._bene_payload(nickname='Wells'))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        queued = self.client.post('/sendChips', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '150.00',
        })
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()['payment']['status'], 'queued')
        payment_id = queued.get_json()['payment']['payment_id']
        denied = self.client.post('/releaseChips', json={'userid': 'alice', 'payment_id': payment_id})
        self.assertEqual(denied.status_code, 403)
        self.login('teller', 'tier2')
        rejected = self.client.post('/rejectChips', json={'userid': 'teller', 'payment_id': payment_id})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()['payment']['status'], 'rejected')

    def test_staff_export_masks_uid(self):
        self.login()
        added = self.client.post('/addChipsBeneficiary', json=self._bene_payload())
        bene_id = added.get_json()['beneficiary']['beneficiary_id']
        sent = self.client.post('/sendChips', json={
            'userid': 'alice', 'beneficiary_id': bene_id, 'amount': '150.00', 'trace_id': 'ex',
        })
        self.login('teller', 'tier1')
        exported = self.client.post('/exportChips', json={
            'userid': 'teller', 'payment_id': sent.get_json()['payment']['payment_id'],
        })
        self.assertEqual(exported.status_code, 200)
        self.assertIn('***456', exported.get_json()['export']['chip1'])
        self.assertNotIn('123456', exported.get_json()['export']['chip1'])


if __name__ == '__main__':
    unittest.main()
