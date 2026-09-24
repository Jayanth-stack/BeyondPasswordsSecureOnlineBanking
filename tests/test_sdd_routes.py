import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.sdd import (
    EurUsdBook,
    MemorySddStore,
    SddPolicy,
    SddService,
    Target2Calendar,
    attach_sdd_routes,
)

CEST = timezone(timedelta(hours=2))
DE_IBAN = 'DE89370400440532013000'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


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
            'Sdd': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        return jsonify({
            'Accounts': {'savings': {'Account': 1002, 'Balance': 10}, 'checkin': {'Account': 1001, 'Balance': 50}, 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Sdd': service.snapshot(values.get('customer_id')),
        }), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        return jsonify({'message': 'Amount Debited'}), 200

    @app.route('/sendWire', methods=['POST'])
    def send_wire():
        return jsonify({'message': 'Wire originated'}), 201

    attach_sdd_routes(app, service)
    return app


class SddRouteTests(unittest.TestCase):
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

        self.service = SddService(
            SddPolicy(
                core_fee=Decimal('8.00'),
                dual_control_threshold=Decimal('10000.00'),
                core_first_lead_days=0,
                core_recurring_lead_days=0,
                b2b_lead_days=0,
            ),
            MemorySddStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': 'None',
            },
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            fx_book=EurUsdBook(rate=Decimal('1.08')),
        )
        self.app = build_app(self.service)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _debtor_payload(self, **overrides):
        payload = {
            'userid': 'alice',
            'nickname': 'Tenant',
            'legal_name': 'Ada Lovelace',
            'iban': DE_IBAN,
            'city': 'Berlin',
            'country': 'DE',
            'bic': 'COBADEFFXXX',
            'default_account': '1001',
        }
        payload.update(overrides)
        return payload

    def _ready(self):
        self.login()
        added = self.client.post('/addSddDebtor', json=self._debtor_payload())
        self.assertEqual(added.status_code, 201)
        debtor_id = added.get_json()['debtor']['debtor_id']
        mandate = self.client.post('/createSddMandate', json={
            'userid': 'alice',
            'debtor_id': debtor_id,
            'umr': 'RENT-1',
            'scheme': 'core',
            'activate': True,
        })
        self.assertEqual(mandate.status_code, 201)
        return mandate.get_json()['mandate']['mandate_id']

    def test_unauthenticated_add_401(self):
        response = self.client.post('/addSddDebtor', json=self._debtor_payload())
        self.assertEqual(response.status_code, 401)

    def test_userid_mismatch_403(self):
        self.login()
        response = self.client.post('/addSddDebtor', json=self._debtor_payload(userid='mallory'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_collect_settles_and_existing_money_routes_untouched(self):
        mandate_id = self._ready()
        preview = self.client.post('/previewSdd', json={
            'userid': 'alice', 'mandate_id': mandate_id, 'amount': '25.00',
        })
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()['preview']['amount_eur'], '25.00')
        collected = self.client.post('/collectSdd', json={
            'userid': 'alice', 'mandate_id': mandate_id, 'amount': '25.00', 'trace_id': 'r1',
        })
        self.assertEqual(collected.status_code, 201)
        self.assertNotIn('iban', collected.get_json()['collection'])
        self.assertEqual(collected.get_json()['collection']['status'], 'sent')
        listed = self.client.post('/listSdds', json={'userid': 'alice'})
        self.assertEqual(listed.get_json()['Sdd']['collections'][0]['status'], 'settled')
        self.assertEqual(listed.get_json()['Sdd']['ytd_collected'], '27.00')
        lookup = self.client.post('/getCustomer', json={'userid': 'alice', 'customer_id': 'alice'})
        self.assertEqual(lookup.get_json()['Sdd']['debtors'][0]['nickname'], 'Tenant')
        transfer = self.client.post('/fundTransfer', json={'userid': 'alice', 'amount': '5'})
        self.assertEqual(transfer.status_code, 200)
        self.assertEqual(transfer.get_json()['message'], 'Request to be approved by tier1 employee')
        withdraw = self.client.post('/withdrawAmount', json={'userid': 'alice', 'amount': '5'})
        self.assertEqual(withdraw.status_code, 200)
        wire = self.client.post('/sendWire', json={'userid': 'alice', 'amount': '5'})
        self.assertEqual(wire.status_code, 201)

    def test_staff_release_and_quote(self):
        mandate_id = self._ready()
        self.login('maker', 'tier1')
        pending = self.client.post('/collectSdd', json={
            'userid': 'maker',
            'customer_id': 'alice',
            'mandate_id': mandate_id,
            'amount': '10000.00',
            'trace_id': 'hv',
        })
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()['collection']['status'], 'pending_release')
        collection_id = pending.get_json()['collection']['collection_id']
        same = self.client.post('/releaseSdd', json={
            'userid': 'maker', 'collection_id': collection_id,
        })
        self.assertEqual(same.status_code, 403)
        self.login('checker', 'tier2')
        released = self.client.post('/releaseSdd', json={
            'userid': 'checker', 'collection_id': collection_id,
        })
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()['collection']['status'], 'sent')
        self.login()
        quote = self.client.post('/quoteSddFx', json={'userid': 'alice', 'amount': '10'})
        self.assertEqual(quote.status_code, 200)
        self.assertEqual(quote.get_json()['quote']['credit_usd'], '10.80')


if __name__ == '__main__':
    unittest.main()
