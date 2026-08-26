import os
import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401  # mock mysql before app import

os.environ['MFA_PROVIDER'] = 'local'
os.environ['MFA_LOCAL_SECRET'] = 'test-mfa-local-secret'

import app as bank_app
from utility.mfa import LOGIN_PURPOSE, PASSWORD_RESET_PURPOSE, compute_local_otp, reset_mfa_service


def _customer_mock(phone='4805550199', password_hash='hashed'):
    user = MagicMock()
    user.retrieve_hashed_password.return_value = password_hash
    user.retrieve_phone_number.return_value = phone
    user.update_login_history.return_value = None
    user.reset_fpassword.return_value = 'Password Updated'
    user.reset_password.return_value = 'Password Updated'
    return user


class MfaLoginFlowTests(unittest.TestCase):
    def setUp(self):
        reset_mfa_service()
        bank_app.app.config['TESTING'] = True
        bank_app.app.config['SECRET_KEY'] = 'test-secret-key-for-sessions'
        bank_app.app.config['SESSION_COOKIE_SECURE'] = False
        self.client = bank_app.app.test_client()

    def tearDown(self):
        reset_mfa_service()

    def test_dashboard_requires_post_mfa_session(self):
        response = self.client.get('/customer_dash', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/', response.headers['Location'])

    def test_otp_page_requires_pending_mfa(self):
        response = self.client.get('/otp_page', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/'))

    @patch('app.check_encrypted_password', return_value=True)
    @patch('app.Customers')
    def test_login_does_not_set_userid_until_otp(self, customers_cls, _check_pw):
        customers_cls.return_value = _customer_mock()
        response = self.client.post('/login', json={
            'userid': 'alice',
            'password': 'ignored',
            'usertype': 'customer',
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/otp_page', response.headers['Location'])

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('pending_userid'), 'alice')
            self.assertNotIn('userid', sess)
            self.assertFalse(sess.get('mfa_verified'))

        blocked = self.client.get('/customer_dash', follow_redirects=False)
        self.assertEqual(blocked.status_code, 302)
        self.assertIn('/otp_page', blocked.headers['Location'])

        api = self.client.post('/loadCustomer', json={'customer_id': 'alice'})
        self.assertEqual(api.status_code, 401)

    @patch('app.check_encrypted_password', return_value=True)
    @patch('app.Customers')
    def test_wrong_otp_keeps_session_pending(self, customers_cls, _check_pw):
        customers_cls.return_value = _customer_mock()
        self.client.post('/login', json={
            'userid': 'alice', 'password': 'ignored', 'usertype': 'customer',
        })
        response = self.client.post('/verify-otp', json={'otp_code': '000000'})
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('pending_userid'), 'alice')
            self.assertNotIn('userid', sess)

    @patch('app.check_encrypted_password', return_value=True)
    @patch('app.Customers')
    def test_valid_otp_grants_dashboard(self, customers_cls, _check_pw):
        user = _customer_mock()
        customers_cls.return_value = user
        self.client.post('/login', json={
            'userid': 'alice', 'password': 'ignored', 'usertype': 'customer',
        })
        code = compute_local_otp('+14805550199', LOGIN_PURPOSE, secret='test-mfa-local-secret')
        response = self.client.post('/verify-otp', json={'otp_code': code}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/customer_dash', response.headers['Location'])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('userid'), 'alice')
            self.assertTrue(sess.get('mfa_verified'))
        user.update_login_history.assert_called()
        dash = self.client.get('/customer_dash', follow_redirects=False)
        self.assertEqual(dash.status_code, 200)

    @patch('app.Customers')
    def test_forgot_password_otp_then_reset(self, customers_cls):
        user = _customer_mock()
        customers_cls.return_value = user
        send = self.client.post('/sendOTP', json={'userid': 'alice', 'requester': 'Customer'})
        self.assertEqual(send.status_code, 200)
        self.assertEqual(send.get_json()['message'], 'OTP Sent')

        code = compute_local_otp('+14805550199', PASSWORD_RESET_PURPOSE, secret='test-mfa-local-secret')
        verify = self.client.post('/OTPAccess', json={
            'userid': 'alice', 'otp': code, 'requester': 'Customer',
        })
        self.assertEqual(verify.get_json()['message'], 'verified')

        reset = self.client.post('/resetPassword', json={
            'userid': 'alice',
            'newPassword': 'NewPass123',
            'requester': 'Customer',
        })
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.get_json()['message'], 'Password Updated')
        user.reset_fpassword.assert_called_with('alice', 'NewPass123')

    @patch('app.Customers')
    def test_reset_without_mfa_is_denied(self, customers_cls):
        customers_cls.return_value = _customer_mock()
        response = self.client.post('/resetPassword', json={
            'userid': 'alice',
            'newPassword': 'NewPass123',
            'requester': 'Customer',
        })
        self.assertEqual(response.status_code, 401)

    def test_logout_clears_auth_session(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
            sess['mfa_verified'] = True
            sess['auth_stage'] = 'authenticated'
        response = self.client.post('/logout', json={'userid': 'alice'}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn('userid', sess)
            self.assertFalse(sess.get('mfa_verified'))

    def test_parse_login_history_pipe_and_legacy(self):
        from customer import parse_login_history
        self.assertEqual(
            parse_login_history('26/08/2026 01:00:00 [login]|25/08/2026 09:00:00 [login]|'),
            ['26/08/2026 01:00:00 [login]', '25/08/2026 09:00:00 [login]'],
        )
        legacy = parse_login_history('26/08/2026 01:00:0025/08/2026 09:00:00')
        self.assertEqual(legacy[0], '26/08/2026 01:00:00')


if __name__ == '__main__':
    unittest.main()
