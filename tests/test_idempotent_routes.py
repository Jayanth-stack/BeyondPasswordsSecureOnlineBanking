"""Exercise the Flask adapter the money-moving routes use.

Does not import app.py (MySQL connects at module import). Mounts the same
flask_idempotent decorator and parse_amount helper onto a tiny app so the
feature path is proven: first transfer runs, retry replays, double-click is
blocked, mismatched payload is 409, invalid amounts are 400.
"""

import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from utility.idempotency import (
    IdempotencyService,
    MemoryIdempotencyStore,
    REPLAY_HEADER,
    flask_idempotent,
    set_idempotency,
)
from utility.money import AmountError, parse_amount


def _parsed_amount(values, field='amount'):
    try:
        return float(parse_amount(values.get(field))), None
    except AmountError as exc:
        return None, (jsonify({'message': str(exc), 'error': exc.code}), 400)


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True
    calls = {'fundTransfer': 0, 'withdrawAmount': 0}

    @app.route('/fundTransfer', methods=['POST', 'GET'])
    @flask_idempotent('fund-transfer')
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json() or {}
        required = ['userid', 'fromAccount', 'toAccount', 'amount']
        if not all(field in values for field in required):
            return jsonify({'message': 'Some data missing'}), 400
        amount, err = _parsed_amount(values)
        if err:
            return err
        calls['fundTransfer'] += 1
        return jsonify({'message': 'queued', 'amount': amount, 'n': calls['fundTransfer']}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    @flask_idempotent('withdraw')
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        values = request.get_json() or {}
        amount, err = _parsed_amount(values)
        if err:
            return err
        calls['withdrawAmount'] += 1
        return jsonify({'message': 'Amount Debited', 'n': calls['withdrawAmount']}), 200

    app.calls = calls
    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class IdempotentMoneyRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryIdempotencyStore()
        set_idempotency(IdempotencyService(self.store))
        self.app = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        set_idempotency(None)

    def _login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _transfer(self, key=None, amount='25.00', from_account=1001, to_account=2002):
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        return self.client.post('/fundTransfer', json={
            'userid': 'alice',
            'fromAccount': from_account,
            'toAccount': to_account,
            'amount': amount,
        }, headers=headers)

    def test_transfer_without_key_still_works(self):
        self._login()
        res = self._transfer()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['message'], 'queued')
        self.assertNotIn(REPLAY_HEADER, res.headers)

    def test_retry_replays_and_does_not_reexecute(self):
        self._login()
        first = self._transfer(key='click-0001')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['n'], 1)
        second = self._transfer(key='click-0001')
        self.assertEqual(second.status_code, 200)
        body = second.get_json()
        self.assertEqual(body['n'], 1)
        self.assertEqual(body['message'], 'queued')
        self.assertEqual(second.headers.get(REPLAY_HEADER), 'true')
        self.assertEqual(self.app.calls['fundTransfer'], 1)

    def test_same_key_different_payload_conflicts(self):
        self._login()
        self._transfer(key='click-0002', amount='25.00')
        res = self._transfer(key='click-0002', amount='50.00')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()['error'], 'idempotency_key_reused')
        self.assertEqual(self.app.calls['fundTransfer'], 1)

    def test_in_progress_blocks_double_click(self):
        self._login()
        svc = IdempotencyService(self.store)
        from utility.idempotency import fingerprint_payload
        svc.begin('click-0003', 'alice:fund-transfer', fingerprint_payload({
            'userid': 'alice', 'fromAccount': 1001, 'toAccount': 2002, 'amount': '25.00',
        }))
        res = self._transfer(key='click-0003')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()['error'], 'idempotency_in_progress')
        self.assertEqual(self.app.calls['fundTransfer'], 0)

    def test_invalid_amount_is_400_and_replayed(self):
        self._login()
        first = self._transfer(key='click-0004', amount='nope')
        self.assertEqual(first.status_code, 400)
        self.assertEqual(first.get_json()['error'], 'invalid_amount')
        second = self._transfer(key='click-0004', amount='nope')
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.headers.get(REPLAY_HEADER), 'true')
        self.assertEqual(self.app.calls['fundTransfer'], 0)

    def test_zero_and_negative_rejected(self):
        self._login()
        self.assertEqual(self._transfer(amount='0').status_code, 400)
        self.assertEqual(self._transfer(amount='-5').status_code, 400)

    def test_body_key_is_accepted(self):
        self._login()
        res = self.client.post('/fundTransfer', json={
            'userid': 'alice',
            'fromAccount': 1001,
            'toAccount': 2002,
            'amount': '5.00',
            'idempotency_key': 'bodykey1',
        })
        self.assertEqual(res.status_code, 200)
        again = self.client.post('/fundTransfer', json={
            'userid': 'alice',
            'fromAccount': 1001,
            'toAccount': 2002,
            'amount': '5.00',
            'idempotency_key': 'bodykey1',
        })
        self.assertEqual(again.headers.get(REPLAY_HEADER), 'true')
        self.assertEqual(self.app.calls['fundTransfer'], 1)

    def test_scopes_do_not_leak_across_users(self):
        self._login('alice')
        self._transfer(key='shared-key')
        with self.client.session_transaction() as sess:
            sess['userid'] = 'bob'
            sess['usertype'] = 'customer'
        res = self.client.post('/fundTransfer', json={
            'userid': 'bob',
            'fromAccount': 1001,
            'toAccount': 2002,
            'amount': '25.00',
        }, headers={'Idempotency-Key': 'shared-key'})
        self.assertEqual(res.status_code, 200)
        self.assertNotEqual(res.headers.get(REPLAY_HEADER), 'true')
        self.assertEqual(self.app.calls['fundTransfer'], 2)

    def test_withdraw_retry(self):
        self._login()
        headers = {'Idempotency-Key': 'withdraw-1'}
        first = self.client.post('/withdrawAmount', json={'amount': '9.99'}, headers=headers)
        second = self.client.post('/withdrawAmount', json={'amount': '9.99'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json()['n'], 1)
        self.assertEqual(second.get_json()['n'], 1)
        self.assertEqual(self.app.calls['withdrawAmount'], 1)

    def test_bad_key_format(self):
        self._login()
        res = self._transfer(key='bad key')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()['error'], 'invalid_idempotency_key')

    def test_get_bypasses_idempotency(self):
        self._login()
        res = self.client.get('/fundTransfer')
        # GET still hits the view; Flask 3 returns 415 without a JSON body.
        self.assertNotEqual(res.headers.get(REPLAY_HEADER), 'true')
        self.assertIn(res.status_code, (400, 415))


if __name__ == '__main__':
    unittest.main()
