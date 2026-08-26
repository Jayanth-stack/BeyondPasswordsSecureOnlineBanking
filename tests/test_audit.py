import json
import unittest

from utility.audit import audit, redact


class AuditTests(unittest.TestCase):
    def test_redacts_password_and_otp_keys(self):
        cleaned = redact({
            'userid': 'alice',
            'password': 'super-secret',
            'otp_code': '123456',
            'nested': {'ssn': '111-22-3333', 'email': 'a@b.c'},
        })
        self.assertEqual(cleaned['userid'], 'alice')
        self.assertEqual(cleaned['password'], '[REDACTED]')
        self.assertEqual(cleaned['otp_code'], '[REDACTED]')
        self.assertEqual(cleaned['nested']['ssn'], '[REDACTED]')
        self.assertEqual(cleaned['nested']['email'], 'a@b.c')

    def test_audit_event_is_json_serializable(self):
        event = audit('login.password', 'failure', actor='alice', usertype='customer',
                      password='should-not-appear')
        encoded = json.dumps(event)
        self.assertIn('login.password', encoded)
        self.assertNotIn('should-not-appear', encoded)
        self.assertEqual(event['details']['password'], '[REDACTED]')


if __name__ == '__main__':
    unittest.main()
