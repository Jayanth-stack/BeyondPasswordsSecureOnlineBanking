"""Feature-path tests for server-side sessions on a tiny Flask app.

Does not import app.py (MySQL connects at import). Mounts the same
init_sessions / session_snapshot helpers the real app uses.
"""

import unittest

try:
    from flask import Flask, jsonify, request, session
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

from utility.sessions import (
    MemorySessionStore,
    SessionPolicy,
    SessionService,
    init_sessions,
    session_snapshot,
    set_service,
)


def build_app(service):
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True
    app.config['SESSION_COOKIE_SECURE'] = False
    init_sessions(app, service=service)

    @app.route('/login', methods=['POST'])
    def login():
        values = request.get_json() or {}
        session.clear()
        session['userid'] = values['userid']
        session['usertype'] = values.get('usertype', 'customer')
        return jsonify({'message': 'ok'})

    @app.route('/whoami')
    def whoami():
        if 'userid' not in session:
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'userid': session['userid'], 'sid': session.sid})

    @app.route('/loadCustomer', methods=['POST', 'GET'])
    def load_customer():
        if 'userid' not in session or session.get('usertype') != 'customer':
            return jsonify({'message': 'Unauthorized access or session expired'}), 401
        return jsonify({'Sessions': session_snapshot(session)}), 200

    @app.route('/logout', methods=['POST'])
    def logout():
        values = request.get_json(silent=True) or {}
        if 'userid' not in values:
            return jsonify({'message': 'Some data missing'}), 400
        session.clear()
        return jsonify({'message': 'logged out'})

    return app


@unittest.skipUnless(HAS_FLASK, 'Flask is not installed')
class SessionRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySessionStore()
        self.svc = SessionService(
            store=self.store,
            policy=SessionPolicy(idle_seconds=1800, absolute_seconds=43200, max_concurrent=2),
        )
        set_service(self.svc)
        self.app = build_app(self.svc)
        self.client = self.app.test_client()

    def tearDown(self):
        set_service(None)

    def _login(self, client=None, userid='alice', ua='Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0'):
        client = client or self.client
        return client.post(
            '/login',
            json={'userid': userid, 'usertype': 'customer'},
            headers={'User-Agent': ua},
        )

    def test_restart_safe_cookie_is_only_a_sid(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get('Set-Cookie', '')
        self.assertTrue(set_cookie)
        self.assertNotIn('alice', set_cookie)
        self.assertNotIn('userid', set_cookie)
        who = self.client.get('/whoami')
        self.assertEqual(who.status_code, 200)
        self.assertEqual(who.get_json()['userid'], 'alice')

    def test_logout_destroys_server_session(self):
        self._login()
        sid = self.client.get('/whoami').get_json()['sid']
        self.assertIsNotNone(self.svc.load(sid))
        resp = self.client.post('/logout', json={'userid': 'alice'})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.svc.load(sid))
        self.assertEqual(self.client.get('/whoami').status_code, 401)

    def test_idle_timeout_unauthenticates(self):
        self._login()
        sid = self.client.get('/whoami').get_json()['sid']
        record = self.store.get(sid)
        record.idle_expires_at = record.last_seen - 1
        self.store.save(record)
        self.assertEqual(self.client.get('/whoami').status_code, 401)

    def test_revoked_session_cannot_be_reused(self):
        self._login()
        sid = self.client.get('/whoami').get_json()['sid']
        self.svc.revoke(sid, 'alice')
        self.assertEqual(self.client.get('/whoami').status_code, 401)

    def test_list_and_revoke_other_device(self):
        self._login(ua='Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0')
        other = self.app.test_client()
        self._login(other, ua='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1')
        listed = self.client.post('/listSessions', json={'userid': 'alice'})
        self.assertEqual(listed.status_code, 200)
        body = listed.get_json()
        self.assertEqual(len(body['sessions']), 2)
        other_sid = [s['sid'] for s in body['sessions'] if not s['current']][0]
        revoked = self.client.post('/revokeSession', json={'sid': other_sid})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(other.get('/whoami').status_code, 401)
        self.assertEqual(self.client.get('/whoami').status_code, 200)

    def test_revoke_others_keeps_current(self):
        self._login()
        other = self.app.test_client()
        self._login(other)
        resp = self.client.post('/revokeOtherSessions', json={'userid': 'alice'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['count'], 1)
        self.assertEqual(self.client.get('/whoami').status_code, 200)
        self.assertEqual(other.get('/whoami').status_code, 401)

    def test_concurrent_cap_evicts_oldest(self):
        first = self.app.test_client()
        second = self.app.test_client()
        third = self.app.test_client()
        self._login(first)
        self._login(second)
        self._login(third)
        self.assertEqual(first.get('/whoami').status_code, 401)
        self.assertEqual(second.get('/whoami').status_code, 200)
        self.assertEqual(third.get('/whoami').status_code, 200)

    def test_load_customer_includes_snapshot(self):
        self._login()
        resp = self.client.post('/loadCustomer', json={})
        self.assertEqual(resp.status_code, 200)
        snap = resp.get_json()['Sessions']
        self.assertIn('sessions', snap)
        self.assertEqual(len(snap['sessions']), 1)
        self.assertTrue(snap['sessions'][0]['current'])
        self.assertTrue(snap['new_device'])

    def test_mismatch_userid_is_403(self):
        self._login()
        resp = self.client.post('/listSessions', json={'userid': 'eve'})
        self.assertEqual(resp.status_code, 403)

    def test_session_transaction_roundtrip(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'bob'
            sess['usertype'] = 'customer'
        who = self.client.get('/whoami')
        self.assertEqual(who.status_code, 200)
        self.assertEqual(who.get_json()['userid'], 'bob')


if __name__ == '__main__':
    unittest.main()
