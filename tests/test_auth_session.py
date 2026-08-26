import unittest

from utility.auth_session import (
    complete_mfa_login,
    dashboard_endpoint,
    is_fully_authenticated,
    is_pending_mfa,
    mark_password_reset_verified,
    password_reset_verified,
    start_mfa_login,
)


class AuthSessionTests(unittest.TestCase):
    def test_password_success_does_not_authenticate(self):
        sess = {}
        start_mfa_login(sess, 'alice', 'customer')
        self.assertTrue(is_pending_mfa(sess))
        self.assertFalse(is_fully_authenticated(sess))
        self.assertNotIn('userid', sess)

    def test_complete_mfa_promotes_identity(self):
        sess = {}
        start_mfa_login(sess, 'alice', 'tier1', emp_tier=1)
        self.assertTrue(complete_mfa_login(sess))
        self.assertTrue(is_fully_authenticated(sess))
        self.assertFalse(is_pending_mfa(sess))
        self.assertEqual(sess['userid'], 'alice')
        self.assertEqual(sess['usertype'], 'tier1')
        self.assertEqual(sess['emp_tier'], 1)
        self.assertTrue(sess['mfa_verified'])

    def test_complete_mfa_without_pending_fails(self):
        self.assertFalse(complete_mfa_login({}))

    def test_start_mfa_clears_previous_session(self):
        sess = {'userid': 'old', 'mfa_verified': True}
        start_mfa_login(sess, 'bob', 'customer')
        self.assertNotEqual(sess.get('userid'), 'old')
        self.assertFalse(is_fully_authenticated(sess))

    def test_dashboard_routing(self):
        self.assertEqual(dashboard_endpoint('customer'), 'get_customer_dash_ui')
        self.assertEqual(dashboard_endpoint('admin', 3), 'get_admin_dashboard_ui')
        self.assertEqual(dashboard_endpoint('tier2', 2), 'get_tier2_dashboard_ui')
        self.assertEqual(dashboard_endpoint('tier1', 1), 'get_tier1_dashboard_ui')

    def test_password_reset_flag_matches_userid(self):
        sess = {}
        mark_password_reset_verified(sess, 'alice', 'Customer')
        self.assertTrue(password_reset_verified(sess, 'alice'))
        self.assertFalse(password_reset_verified(sess, 'eve'))


if __name__ == '__main__':
    unittest.main()
