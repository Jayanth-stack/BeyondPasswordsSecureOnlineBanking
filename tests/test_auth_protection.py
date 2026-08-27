import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.app_loader import bank, csrf_header
from utility.rate_limit import RateLimiter


class LoginCsrfAndRateLimitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp = tmp
        self.limiter = RateLimiter.from_path(os.path.join(tmp.name, 'rl.sqlite'))
        bank.app.extensions['rate_limiter'] = self.limiter
        bank.app.config['LOGIN_USER_LIMIT'] = 5
        bank.app.config['LOGIN_USER_WINDOW'] = 900
        bank.app.config['LOGIN_IP_LIMIT'] = 50
        bank.app.config['LOGIN_IP_WINDOW'] = 900
        self.client = bank.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self, userid='alice', password='wrong', headers=None, usertype='customer'):
        hdrs = headers if headers is not None else csrf_header(self.client)
        return self.client.post(
            '/login',
            json={'userid': userid, 'password': password, 'usertype': usertype},
            headers=hdrs,
        )

    def test_login_without_csrf_is_403(self):
        resp = self.client.post(
            '/login',
            json={'userid': 'alice', 'password': 'x', 'usertype': 'customer'},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['code'], 'csrf_failed')

    def test_login_get_page_still_works(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)

    def test_failed_login_then_lockout(self):
        with patch.object(bank, 'Customers') as cls:
            user = cls.return_value
            user.retrieve_hashed_password.return_value = None
            last = None
            for _ in range(5):
                last = self._login()
                self.assertEqual(last.status_code, 401)
            locked = self._login()
            self.assertEqual(locked.status_code, 429)
            self.assertEqual(locked.get_json()['code'], 'rate_limited')
            self.assertIn('Retry-After', locked.headers)

    def test_successful_login_resets_user_failures(self):
        with patch.object(bank, 'Customers') as cls:
            user = cls.return_value
            user.retrieve_hashed_password.return_value = None
            for _ in range(4):
                self.assertEqual(self._login().status_code, 401)
            user.retrieve_hashed_password.return_value = 'hashed'
            user.retrieve_phone_number.return_value = '+15555550100'
            with patch.object(bank, 'check_encrypted_password', return_value=True), \
                    patch.object(bank, 'client') as twilio:
                twilio.verify.v2.services.return_value.verifications.create.return_value = MagicMock()
                resp = self._login(password='correct')
            self.assertIn(resp.status_code, (302, 200))
            user.retrieve_hashed_password.return_value = None
            self.assertEqual(self._login().status_code, 401)

    def test_otp_verify_requires_csrf_and_counts_attempts(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
            sess['_csrf_token'] = 'fixed-token'
        bank.app.config['OTP_VERIFY_LIMIT'] = 3
        with patch.object(bank, 'Customers') as cls:
            cls.return_value.retrieve_phone_number.return_value = '+15555550100'
            with patch.object(bank, 'client') as twilio:
                check = MagicMock()
                check.status = 'denied'
                twilio.verify.v2.services.return_value.verification_checks.create.return_value = check
                headers = {'X-CSRF-Token': 'fixed-token'}
                for _ in range(3):
                    resp = self.client.post(
                        '/verify-otp',
                        json={'otp_code': '000000'},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 401)
                locked = self.client.post(
                    '/verify-otp',
                    json={'otp_code': '000000'},
                    headers=headers,
                )
                self.assertEqual(locked.status_code, 429)

    def test_send_otp_rate_limited_per_user(self):
        bank.app.config['OTP_SEND_LIMIT'] = 2
        bank.app.config['OTP_SEND_WINDOW'] = 600
        with patch.object(bank, 'Customers') as cls:
            cls.return_value.retrieve_phone_number.return_value = '+15555550100'
            with patch.object(bank, 'twilio_client') as twilio:
                twilio.verify.v2.services.return_value.verifications.create.return_value = MagicMock(sid='sid')
                headers = csrf_header(self.client)
                for _ in range(2):
                    resp = self.client.post(
                        '/sendOTP',
                        json={'userid': 'alice', 'requester': 'Customer'},
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200)
                locked = self.client.post(
                    '/sendOTP',
                    json={'userid': 'alice', 'requester': 'Customer'},
                    headers=headers,
                )
                self.assertEqual(locked.status_code, 429)


class FundTransferCsrfTests(unittest.TestCase):
    def setUp(self):
        self.client = bank.app.test_client()

    def test_fund_transfer_without_csrf_is_403(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
        resp = self.client.post(
            '/fundTransfer',
            json={'userid': 'alice', 'fromAccount': 1, 'toAccount': 2, 'amount': 10},
        )
        self.assertEqual(resp.status_code, 403)

    def test_fund_transfer_with_csrf_passes_guard(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
        headers = csrf_header(self.client)
        with patch.object(bank, 'Employee') as emp_cls:
            emp_cls.return_value.add_transaction.return_value = 'Request to be approved by tier1 employee'
            resp = self.client.post(
                '/fundTransfer',
                json={'userid': 'alice', 'fromAccount': 1, 'toAccount': 2, 'amount': 10},
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Request to be approved', resp.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
