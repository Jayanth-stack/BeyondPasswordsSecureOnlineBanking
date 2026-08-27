import unittest

from flask import Flask, jsonify, request

from utility.csrf import CsrfProtection, init_csrf


class FakeRequest:
    def __init__(self, headers=None, json_body=None, form=None, args=None):
        self.headers = headers or {}
        self._json = json_body
        self.form = form or {}
        self.args = args or {}

    def get_json(self, silent=True):
        return self._json


class CsrfCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.csrf = CsrfProtection()
        self.session = {}

    def test_issue_is_stable_until_rotate(self):
        first = self.csrf.issue(self.session)
        second = self.csrf.issue(self.session)
        self.assertEqual(first, second)
        rotated = self.csrf.rotate(self.session)
        self.assertNotEqual(first, rotated)
        self.assertEqual(self.csrf.issue(self.session), rotated)

    def test_header_validates(self):
        token = self.csrf.issue(self.session)
        req = FakeRequest(headers={'X-CSRF-Token': token})
        self.assertTrue(self.csrf.validate(self.session, req))

    def test_json_body_fallback(self):
        token = self.csrf.issue(self.session)
        req = FakeRequest(json_body={'csrf_token': token})
        self.assertTrue(self.csrf.validate(self.session, req))

    def test_rejects_missing_and_mismatch(self):
        self.csrf.issue(self.session)
        self.assertFalse(self.csrf.validate(self.session, FakeRequest()))
        self.assertFalse(self.csrf.validate(self.session, FakeRequest(
            headers={'X-CSRF-Token': 'nope' + 'x' * 40}
        )))
        self.assertFalse(self.csrf.validate({}, FakeRequest(
            headers={'X-CSRF-Token': 'anything'}
        )))


class CsrfFlaskTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = 'csrf-test'
        app.config['TESTING'] = True
        app.config['CSRF_ENABLED'] = True
        init_csrf(app)

        @app.route('/echo', methods=['POST'])
        def echo():
            return jsonify({'ok': True, 'userid': (request.get_json() or {}).get('userid')})

        @app.route('/open', methods=['GET'])
        def open_view():
            return jsonify({'ok': True})

        self.client = app.test_client()

    def test_get_does_not_need_token(self):
        resp = self.client.get('/open')
        self.assertEqual(resp.status_code, 200)

    def test_post_without_token_is_403(self):
        resp = self.client.post('/echo', json={'userid': 'alice'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['code'], 'csrf_failed')

    def test_post_with_issued_token_succeeds(self):
        token = self.client.get('/csrf-token').get_json()['csrf_token']
        resp = self.client.post(
            '/echo',
            json={'userid': 'alice'},
            headers={'X-CSRF-Token': token},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['userid'], 'alice')

    def test_token_is_session_bound(self):
        token = self.client.get('/csrf-token').get_json()['csrf_token']
        other = Flask(__name__)
        other.secret_key = 'csrf-test'
        other.config['TESTING'] = True
        other.config['CSRF_ENABLED'] = True
        init_csrf(other)

        @other.route('/echo', methods=['POST'])
        def echo():
            return jsonify({'ok': True})

        other_client = other.test_client()
        resp = other_client.post('/echo', json={}, headers={'X-CSRF-Token': token})
        self.assertEqual(resp.status_code, 403)


if __name__ == '__main__':
    unittest.main()
