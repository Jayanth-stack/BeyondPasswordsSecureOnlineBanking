"""Exercise the same approval-policy gate the approve/queue routes use.

Does not import app.py (MySQL connects at module import). Mounts the
identical decision flow onto a tiny Flask app so the feature path is real:
small amounts execute, high-value records a first approval, a second
distinct supervisor executes, the maker cannot double-approve.
"""

import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from utility.approval_policy import (
    ACTION_ESCALATE,
    ACTION_EXECUTE,
    ACTION_RECORD_FIRST,
    Actor,
    ApprovalPolicy,
    flask_error,
    first_approval_remark,
    parse_first_approver,
    pending_visible_to,
    set_policy,
)


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    pending = {
        11: {'amount': 50, 'from': 1001, 'to': 1002, 'status': 1, 'remark': '', 'executed': False},
        12: {'amount': 2500, 'from': 1001, 'to': 1002, 'status': 1, 'remark': '', 'executed': False},
    }

    def _actor():
        return Actor.from_mapping({
            'userid': session.get('userid'),
            'usertype': session.get('usertype'),
            'emp_tier': session.get('emp_tier'),
        })

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json()
        requirement = ApprovalPolicy().classify('transfer', values['amount'])
        return jsonify({'message': requirement.queue_message(),
                        'required_tier': requirement.required_tier,
                        'required_approvals': requirement.required_approvals}), 200

    @app.route('/approveRequest', methods=['POST'])
    def approve_request():
        if 'userid' not in session:
            return jsonify({'message': 'Not logged In'}), 401
        values = request.get_json()
        if session.get('usertype') != 'customer' or session.get('userid') != values['customer_id']:
            return jsonify({'message': 'Not authorized to approve this request',
                            'error': 'not_customer'}), 403
        txn = pending.get(int(values['transaction_no']))
        if not txn or txn['status'] == 0:
            return jsonify({'message': 'Wrong Transaction number'}), 400
        decision = ApprovalPolicy().review(
            _actor(), 'fund_request', txn['amount'], expected_role='customer'
        )
        denied = flask_error(decision)
        if denied:
            return denied
        if decision.action == ACTION_ESCALATE:
            return jsonify({'message': 'Request Sent to Tier2 employee',
                            'action': ACTION_ESCALATE}), 200
        txn['status'] = 0
        txn['executed'] = True
        return jsonify({'message': 'done', 'action': ACTION_EXECUTE}), 200

    @app.route('/approveRequestEmp', methods=['POST'])
    def approve_request_employee():
        if 'userid' not in session or session['userid'] != request.get_json()['userid']:
            return jsonify({'message': 'Not logged In'}), 401
        values = request.get_json()
        txn = pending.get(int(values['transaction_no']))
        if not txn or txn['status'] == 0:
            return jsonify({'message': 'Invalid transaction_no'}), 400
        decision = ApprovalPolicy().review(
            _actor(), 'transfer', txn['amount'],
            parse_first_approver(txn['remark']), expected_role='employee'
        )
        denied = flask_error(decision)
        if denied:
            return denied
        if decision.action == ACTION_RECORD_FIRST:
            txn['remark'] = first_approval_remark(values['userid'])
            return jsonify({'message': 'Awaiting second approval',
                            'action': ACTION_RECORD_FIRST}), 200
        txn['status'] = 0
        txn['executed'] = True
        return jsonify({'message': 'done', 'action': ACTION_EXECUTE}), 200

    @app.route('/pendingForActor', methods=['GET'])
    def pending_for_actor():
        rows = []
        for txn_no, txn in pending.items():
            if txn['status'] != 1:
                continue
            rows.append((
                txn_no, txn['from'], txn['to'], '-1',
                2 if txn['amount'] > 1000 else 1,
                txn['amount'], 0, 1, txn['remark'],
            ))
        visible = pending_visible_to(_actor(), rows, ApprovalPolicy())
        return jsonify({'ids': [row[0] for row in visible]}), 200

    app.pending = pending
    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class ApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        set_policy(ApprovalPolicy())
        self.app = build_app()
        self.client = self.app.test_client()

    def tearDown(self):
        set_policy(None)

    def _login(self, userid, usertype, emp_tier=None):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype
            if emp_tier is not None:
                sess['emp_tier'] = emp_tier

    def test_queue_message_for_small_and_large_transfers(self):
        self._login('alice', 'customer')
        small = self.client.post('/fundTransfer', json={'amount': 1000})
        large = self.client.post('/fundTransfer', json={'amount': 1000.01})
        self.assertEqual(small.get_json()['message'], 'Request to be approved by tier1 employee')
        self.assertEqual(small.get_json()['required_approvals'], 1)
        self.assertEqual(large.get_json()['message'], 'Request to be approved by tier2 employee')
        self.assertEqual(large.get_json()['required_approvals'], 2)

    def test_customer_executes_small_fund_request(self):
        self._login('alice', 'customer')
        res = self.client.post('/approveRequest', json={'customer_id': 'alice', 'transaction_no': 11})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['action'], ACTION_EXECUTE)
        self.assertTrue(self.app.pending[11]['executed'])

    def test_customer_escalates_high_value_without_moving_money(self):
        self._login('alice', 'customer')
        res = self.client.post('/approveRequest', json={'customer_id': 'alice', 'transaction_no': 12})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['action'], ACTION_ESCALATE)
        self.assertFalse(self.app.pending[12]['executed'])

    def test_customer_cannot_approve_as_someone_else(self):
        self._login('alice', 'customer')
        res = self.client.post('/approveRequest', json={'customer_id': 'bob', 'transaction_no': 11})
        self.assertEqual(res.status_code, 403)

    def test_tier1_executes_small_amount(self):
        self._login('emp1', 'tier1', emp_tier=1)
        res = self.client.post('/approveRequestEmp', json={'userid': 'emp1', 'transaction_no': 11})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['action'], ACTION_EXECUTE)
        self.assertTrue(self.app.pending[11]['executed'])

    def test_high_value_requires_two_distinct_employees(self):
        self._login('emp1', 'tier1', emp_tier=1)
        first = self.client.post('/approveRequestEmp', json={'userid': 'emp1', 'transaction_no': 12})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['action'], ACTION_RECORD_FIRST)
        self.assertFalse(self.app.pending[12]['executed'])
        self.assertTrue(self.app.pending[12]['remark'].startswith('FIRST:'))

        replay = self.client.post('/approveRequestEmp', json={'userid': 'emp1', 'transaction_no': 12})
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.get_json()['error'], 'same_approver')
        self.assertFalse(self.app.pending[12]['executed'])

        self._login('sup2', 'tier2', emp_tier=2)
        second = self.client.post('/approveRequestEmp', json={'userid': 'sup2', 'transaction_no': 12})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()['action'], ACTION_EXECUTE)
        self.assertTrue(self.app.pending[12]['executed'])

    def test_customer_cannot_hit_employee_approve(self):
        self._login('alice', 'customer')
        res = self.client.post('/approveRequestEmp', json={'userid': 'alice', 'transaction_no': 11})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()['error'], 'not_employee')
        self.assertFalse(self.app.pending[11]['executed'])

    def test_tier1_queue_includes_maker_work_on_high_value(self):
        self._login('emp1', 'tier1', emp_tier=1)
        res = self.client.get('/pendingForActor')
        self.assertEqual(res.get_json()['ids'], [11, 12])


if __name__ == '__main__':
    unittest.main()
