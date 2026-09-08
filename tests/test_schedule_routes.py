import os
import unittest

from flask import Flask, jsonify, request, session

from utility.schedule import (
    MemoryScheduleStore,
    SchedulePolicy,
    ScheduleService,
    attach_schedule_routes,
    own_accounts_from_customer_payload,
    parse_money,
)


def build_app(service, executed):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    def _own(_userid):
        values = request.get_json(silent=True) or {}
        return values.get('own_accounts') or ['1001', '1002']

    def _exec(payload):
        executed.append(('transfer', payload['from_account'], payload['to_account'], payload['amount']))
        return 'Request to be approved by tier1 employee'

    service.executor = _exec

    @app.route('/fundTransfer', methods=['POST'])
    def fund_transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        if values.get('userid') != session['userid']:
            return jsonify({'message': 'User ID mismatch'}), 401
        executed.append(('immediate', values.get('fromAccount'), values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        service.run_due(userid=session['userid'])
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Schedules': service.snapshot(session['userid']),
        }), 200

    @app.route('/getCustomer', methods=['POST'])
    def get_customer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        owner = values.get('customer_id')
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'Schedules': service.snapshot(owner),
        }), 200

    attach_schedule_routes(app, service, own_accounts_loader=_own, executor=_exec)
    return app


class ScheduleRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.executed = []
        self.service = ScheduleService(
            SchedulePolicy(min_lead_seconds=60, max_amount=parse_money('5000.00')),
            MemoryScheduleStore(),
            clock=lambda: self.now[0],
        )
        self.app = build_app(self.service, self.executed)
        self.client = self.app.test_client()

    def login(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype
            if usertype != 'customer':
                sess['emp_tier'] = 1

    def test_401_without_session(self):
        response = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10',
        })
        self.assertEqual(response.status_code, 401)

    def test_403_userid_mismatch(self):
        self.login()
        response = self.client.post('/scheduleTransfer', json={
            'userid': 'bob', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10',
            'start_at': self.now[0] + 120,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'userid_mismatch')

    def test_create_list_cancel(self):
        self.login()
        created = self.client.post('/scheduleTransfer', json={
            'userid': 'alice',
            'fromAccount': '1001',
            'toAccount': '2002',
            'amount': '10.00',
            'interval': 'monthly',
            'start_at': self.now[0] + 120,
        })
        self.assertEqual(created.status_code, 201)
        body = created.get_json()
        self.assertEqual(body['message'], 'Transfer scheduled')
        schedule_id = body['schedule']['schedule_id']
        listed = self.client.post('/listSchedules', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['Schedules']['open_count'], 1)
        cancelled = self.client.post('/cancelSchedule', json={
            'userid': 'alice', 'schedule_id': schedule_id,
        })
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()['schedule']['status'], 'cancelled')

    def test_unowned_account_forbidden(self):
        self.login()
        response = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '9999', 'toAccount': '2002', 'amount': '10',
            'start_at': self.now[0] + 120,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'schedule_forbidden')

    def test_duplicate_409(self):
        self.login()
        payload = {
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '10.00',
            'start_at': self.now[0] + 120,
        }
        self.assertEqual(self.client.post('/scheduleTransfer', json=payload).status_code, 201)
        again = self.client.post('/scheduleTransfer', json=payload)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()['error'], 'schedule_duplicate')

    def test_invalid_amount_400(self):
        self.login()
        response = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '-1',
            'start_at': self.now[0] + 120,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_amount')

    def test_missing_schedule_id(self):
        self.login()
        response = self.client.post('/cancelSchedule', json={'userid': 'alice'})
        self.assertEqual(response.status_code, 400)

    def test_cancel_missing_404(self):
        self.login()
        response = self.client.post('/cancelSchedule', json={'userid': 'alice', 'schedule_id': 'nope'})
        self.assertEqual(response.status_code, 404)

    def test_pause_resume(self):
        self.login()
        created = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '3.00',
            'interval': 'weekly', 'start_at': self.now[0] + 120,
        })
        schedule_id = created.get_json()['schedule']['schedule_id']
        paused = self.client.post('/pauseSchedule', json={'userid': 'alice', 'schedule_id': schedule_id})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.get_json()['schedule']['status'], 'paused')
        resumed = self.client.post('/resumeSchedule', json={'userid': 'alice', 'schedule_id': schedule_id})
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()['schedule']['status'], 'active')

    def test_run_due_and_load_customer(self):
        self.login()
        created = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '7.00',
            'start_at': self.now[0] + 60,
        })
        self.assertEqual(created.status_code, 201)
        self.now[0] = 1_080.0
        loaded = self.client.post('/loadCustomer', json={'userid': 'alice'})
        self.assertEqual(loaded.status_code, 200)
        snap = loaded.get_json()['Schedules']
        self.assertEqual(snap['schedules'][0]['status'], 'completed')
        self.assertEqual(self.executed[-1][0], 'transfer')
        self.assertEqual(self.executed[-1][3], '7.00')

    def test_immediate_transfer_still_runs(self):
        self.login()
        response = self.client.post('/fundTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.executed, [('immediate', '1001', '1')])

    def test_employee_can_schedule_and_get_customer(self):
        self.login('emp1', 'tier1')
        created = self.client.post('/scheduleTransfer', json={
            'userid': 'emp1',
            'customer_id': 'alice',
            'fromAccount': '1001',
            'toAccount': '2002',
            'amount': '4.00',
            'start_at': self.now[0] + 120,
        })
        self.assertEqual(created.status_code, 201)
        looked = self.client.post('/getCustomer', json={'userid': 'emp1', 'customer_id': 'alice'})
        self.assertEqual(looked.get_json()['Schedules']['open_count'], 1)
        cancelled = self.client.post('/cancelSchedule', json={
            'userid': 'emp1',
            'schedule_id': created.get_json()['schedule']['schedule_id'],
        })
        self.assertEqual(cancelled.status_code, 200)

    def test_customer_cannot_cancel_others(self):
        self.login()
        created = self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '2.00',
            'start_at': self.now[0] + 120,
        })
        schedule_id = created.get_json()['schedule']['schedule_id']
        self.login('bob', 'customer')
        denied = self.client.post('/cancelSchedule', json={'userid': 'bob', 'schedule_id': schedule_id})
        self.assertEqual(denied.status_code, 403)

    def test_run_due_route(self):
        self.login()
        self.client.post('/scheduleTransfer', json={
            'userid': 'alice', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '1.50',
            'start_at': self.now[0] + 60,
        })
        self.now[0] = 1_080.0
        ran = self.client.post('/runDueSchedules', json={'userid': 'alice'})
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.get_json()['ran'], 1)

    def test_own_accounts_helper(self):
        payload = {'savings': {'Account': 1001, 'Balance': 1}, 'checkin': 'None', 'credit': 'None'}
        self.assertEqual(own_accounts_from_customer_payload(payload), ['1001'])

    def test_app_wiring_scan(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        with open(os.path.join(root, 'app.py')) as handle:
            app_src = handle.read()
        self.assertIn('attach_schedule_routes', app_src)
        self.assertIn("schedule_service.run_due", app_src)
        self.assertIn("'Schedules': schedule_service.snapshot", app_src)
        with open(os.path.join(root, 'static/main_js/customer.js')) as handle:
            js = handle.read()
        self.assertIn('scheduleTransfer', js)
        self.assertIn('cancelSchedule', js)
        with open(os.path.join(root, 'templates/customer.html')) as handle:
            html = handle.read()
        self.assertIn('scheduled_transfers_pane', html)
        self.assertIn('scheduled_transfers_menu', html)


if __name__ == '__main__':
    unittest.main()
