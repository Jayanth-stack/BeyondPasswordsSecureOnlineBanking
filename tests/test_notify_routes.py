import os
import unittest

from flask import Flask, jsonify, request, session

from utility.notify import (
    Contact,
    MemoryNotifyStore,
    NotifyPolicy,
    NotifyService,
    RecordingChannel,
    attach_notify_routes,
)


CONTACTS = {
    'ada': Contact(userid='ada', usertype='customer', email='ada@bank.test', phone='+15555550100'),
}


def build_app(service, events):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

    @app.route('/login', methods=['POST'])
    def login():
        values = request.get_json() or {}
        ctx_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
        ua = request.headers.get('User-Agent', '')
        if values.get('password') != 'ok':
            service.note_login_failure(values.get('userid', ''), usertype=values.get('usertype', 'customer'),
                                       ip=ctx_ip, user_agent=ua)
            return jsonify({'message': 'Invalid credentials'}), 401
        session['userid'] = values['userid']
        session['usertype'] = values.get('usertype', 'customer')
        return jsonify({'message': 'otp'}), 200

    @app.route('/verify-otp', methods=['POST'])
    def verify_otp():
        if 'userid' not in session:
            return jsonify({'error': 'Session expired or invalid'}), 401
        ctx_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
        ua = request.headers.get('User-Agent', '')
        service.note_login_success(session['userid'], session.get('usertype', 'customer'),
                                   ip=ctx_ip, user_agent=ua)
        return jsonify({'message': 'ok'}), 200

    @app.route('/fundTransfer', methods=['POST'])
    def transfer():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        if values.get('userid') != session['userid']:
            return jsonify({'message': 'User ID mismatch'}), 401
        service.emit_if_high_value(
            session['userid'], session.get('usertype', 'customer'), 'transfer',
            values.get('amount'), values.get('fromAccount'), values.get('toAccount'),
            ip=request.remote_addr or '', user_agent=request.headers.get('User-Agent', ''),
        )
        events.append(('transfer', values.get('amount')))
        return jsonify({'message': 'Request to be approved by tier1 employee'}), 200

    @app.route('/withdrawAmount', methods=['POST'])
    def withdraw():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        service.emit_if_high_value(
            session['userid'], session.get('usertype', 'customer'), 'withdraw',
            values.get('amount'), values.get('account'), None,
        )
        events.append(('withdraw', values.get('amount')))
        return jsonify({'message': 'done'}), 200

    @app.route('/getCashierCheque', methods=['POST'])
    def cheque():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        values = request.get_json() or {}
        service.emit_if_high_value(
            session['userid'], session.get('usertype', 'customer'), 'cheque',
            values.get('amount'), values.get('from_account'), values.get('to_account'),
        )
        events.append(('cheque', values.get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/depositAmount', methods=['POST'])
    def deposit():
        events.append(('deposit', (request.get_json() or {}).get('amount')))
        return jsonify({'message': 'Success'}), 200

    @app.route('/requestFunds', methods=['POST'])
    def request_funds():
        events.append(('request', None))
        return jsonify({'message': 'Request Sent'}), 200

    @app.route('/resetPassword', methods=['POST'])
    def reset_password():
        values = request.get_json() or {}
        service.emit_quietly('password_reset', userid=values.get('userid'), usertype='customer')
        return jsonify({'message': 'Password updated'}), 200

    @app.route('/updateInfo', methods=['POST'])
    def update_info():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized'}), 401
        service.emit_quietly('profile_change', userid=session['userid'], usertype=session.get('usertype', 'customer'))
        return jsonify({'message': 'Request Sent'}), 200

    @app.route('/loadCustomer', methods=['POST'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({
            'Accounts': {'savings': {'Account': 1001, 'Balance': 50}, 'checkin': 'None', 'credit': 'None'},
            'Info': {'first_name': 'Ada'},
            'FundsRequests': 'None',
            'Notifications': service.snapshot(session['userid']),
        }), 200

    attach_notify_routes(app, service)
    return app


class NotifyRoutesTest(unittest.TestCase):
    def setUp(self):
        self.email = RecordingChannel('email')
        self.store = MemoryNotifyStore()
        self.service = NotifyService(
            store=self.store,
            policy=NotifyPolicy(cooldown_seconds=0, login_failure_threshold=3, high_value_threshold=500),
            channels=[self.email],
            contact_resolver=lambda u, t: CONTACTS.get(u, Contact(userid=u, email=f'{u}@x.test')),
        )
        self.events = []
        self.app = build_app(self.service, self.events)
        self.client = self.app.test_client()

    def login(self, userid='ada', password='ok', usertype='customer', **headers):
        return self.client.post('/login', json={'userid': userid, 'password': password, 'usertype': usertype},
                                headers=headers)

    def authed(self, userid='ada'):
        self.login(userid)
        self.client.post('/verify-otp', json={'otp_code': '000000'},
                         headers={'User-Agent': 'Chrome/120 Windows', 'X-Forwarded-For': '203.0.113.10'})

    def test_failed_logins_then_notify(self):
        for _ in range(2):
            resp = self.login(password='no')
            self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.store.unread_count('ada'), 0)
        resp = self.login(password='no')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.store.unread_count('ada'), 1)
        self.assertEqual(self.store.list_notifications('ada')[0].event, 'login_failure')

    def test_verify_otp_new_device_then_known(self):
        self.login()
        first = self.client.post('/verify-otp', json={'otp_code': '1'},
                                 headers={'User-Agent': 'Firefox Linux', 'X-Forwarded-For': '198.51.100.2'})
        self.assertEqual(first.status_code, 200)
        inbox = self.service.snapshot('ada')
        self.assertEqual(inbox['unread'], 1)
        self.assertEqual(inbox['items'][0]['event'], 'new_device')
        self.assertTrue(self.email.sent)

        self.client.post('/verify-otp', json={'otp_code': '1'},
                         headers={'User-Agent': 'Firefox Linux', 'X-Forwarded-For': '198.51.100.9'})
        self.assertEqual(self.service.store.unread_count('ada'), 1)

    def test_high_value_transfer_notifies_low_does_not(self):
        self.authed()
        low = self.client.post('/fundTransfer', json={
            'userid': 'ada', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '20.00',
        })
        self.assertEqual(low.status_code, 200)
        self.assertEqual(low.get_json()['message'], 'Request to be approved by tier1 employee')
        high = self.client.post('/fundTransfer', json={
            'userid': 'ada', 'fromAccount': '1001', 'toAccount': '2002', 'amount': '500.00',
        })
        self.assertEqual(high.status_code, 200)
        events = [item.event for item in self.store.list_notifications('ada')]
        self.assertIn('high_value', events)
        self.assertEqual(self.events, [('transfer', '20.00'), ('transfer', '500.00')])

    def test_deposit_and_request_ungated(self):
        self.authed()
        before = self.store.unread_count('ada')
        self.client.post('/depositAmount', json={'userid': 'ada', 'account': '1001', 'amount': '9000'})
        self.client.post('/requestFunds', json={'userid': 'ada'})
        self.assertEqual(self.store.unread_count('ada'), before)
        self.assertIn(('deposit', '9000'), self.events)

    def test_cheque_and_withdraw_high_value(self):
        self.authed()
        self.client.post('/getCashierCheque', json={
            'userid': 'ada', 'from_account': '1001', 'to_account': '2002', 'amount': '500.00',
        })
        self.client.post('/withdrawAmount', json={'userid': 'ada', 'account': '1001', 'amount': '500.00'})
        events = {item.event for item in self.store.list_notifications('ada')}
        self.assertIn('high_value', events)

    def test_password_reset_and_profile(self):
        self.authed()
        self.client.post('/resetPassword', json={'userid': 'ada', 'newPassword': 'x', 'otp': '1'})
        self.client.post('/updateInfo', json={'userid': 'ada'})
        events = {item.event for item in self.store.list_notifications('ada')}
        self.assertIn('password_reset', events)
        self.assertIn('profile_change', events)

    def test_load_customer_snapshot(self):
        self.authed()
        resp = self.client.post('/loadCustomer', json={'userid': 'ada'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('Notifications', data)
        self.assertGreaterEqual(data['Notifications']['unread'], 1)
        self.assertIn('prefs', data['Notifications'])

    def test_list_mark_prefs_and_mismatch(self):
        self.authed()
        listed = self.client.post('/listNotifications', json={'userid': 'ada'})
        self.assertEqual(listed.status_code, 200)
        note_id = listed.get_json()['items'][0]['notification_id']
        marked = self.client.post('/markNotificationRead', json={'userid': 'ada', 'notification_id': note_id})
        self.assertEqual(marked.status_code, 200)
        self.assertEqual(marked.get_json()['unread'], 0)
        missing = self.client.post('/markNotificationRead', json={'userid': 'ada', 'notification_id': 'nope'})
        self.assertEqual(missing.status_code, 404)

        prefs = self.client.post('/updateNotifyPrefs', json={
            'userid': 'ada', 'email_enabled': False, 'sms_enabled': True, 'events': ['new_device'],
        })
        self.assertEqual(prefs.status_code, 200)
        self.assertFalse(prefs.get_json()['email_enabled'])
        self.assertEqual(prefs.get_json()['events'], ['new_device'])

        with self.app.test_client() as other:
            other.post('/login', json={'userid': 'ada', 'password': 'ok', 'usertype': 'customer'})
            denied = other.post('/listNotifications', json={'userid': 'eve'})
            self.assertEqual(denied.status_code, 403)

    def test_unauthenticated_crud(self):
        resp = self.client.post('/listNotifications', json={'userid': 'ada'})
        self.assertEqual(resp.status_code, 401)

    def test_app_py_wiring(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        with open(os.path.join(root, 'app.py'), encoding='utf-8') as handle:
            app_src = handle.read()
        self.assertIn('from utility.notify import', app_src)
        self.assertIn('note_login_failure', app_src)
        self.assertIn('note_login_success', app_src)
        self.assertIn('emit_if_high_value', app_src)
        self.assertIn('attach_notify_routes', app_src)
        self.assertIn("'Notifications':", app_src)
        with open(os.path.join(root, 'static', 'main_js', 'customer.js'), encoding='utf-8') as handle:
            js_src = handle.read()
        self.assertIn('security_alerts', js_src)
        self.assertIn('listNotifications', js_src)
        self.assertIn('updateNotifyPrefs', js_src)
        with open(os.path.join(root, 'templates', 'customer.html'), encoding='utf-8') as handle:
            html = handle.read()
        self.assertIn('security_alerts_pane', html)
        self.assertIn('security_alerts_tbl', html)


if __name__ == '__main__':
    unittest.main()
