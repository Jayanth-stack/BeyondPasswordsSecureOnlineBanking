import unittest
from unittest.mock import MagicMock, patch

# customer/employee modules connect to MySQL at import time; stub before app import.
_MOCK_DB = MagicMock()
_MOCK_CURSOR = MagicMock()
_MOCK_DB.cursor.return_value = _MOCK_CURSOR

with patch("mysql.connector.connect", return_value=_MOCK_DB):
    from app import app


class SessionAuthRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True

    def _login_customer_session(self, userid="alice"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "customer"

    def _login_tier2_session(self, userid="tier2emp"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "tier2"
            sess["emp_tier"] = 2

    def test_deny_request_rejects_session_key_spoof(self):
        self._login_customer_session()

        response = self.client.post(
            "/denyRequest",
            json={"userid": "userid", "transaction_no": 99},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

    @patch("app.Customers")
    def test_deny_request_allows_matching_customer(self, customers_cls):
        customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
        self._login_customer_session("alice")

        response = self.client.post(
            "/denyRequest",
            json={"userid": "alice", "transaction_no": 99},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Request Cancelled")

    def test_logout_clears_session(self):
        self._login_customer_session("alice")

        response = self.client.post("/logout", json={"userid": "alice"})
        self.assertEqual(response.status_code, 302)

        with self.client.session_transaction() as sess:
            self.assertNotIn("userid", sess)
            self.assertNotIn("usertype", sess)

    def test_logout_rejects_userid_mismatch(self):
        self._login_customer_session("alice")

        response = self.client.post("/logout", json={"userid": "bob"})
        self.assertEqual(response.status_code, 401)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("userid"), "alice")

    @patch("app.Employee")
    @patch("app.Customers")
    def test_approve_request_uses_session_userid(self, customers_cls, employee_cls):
        employee_cls.return_value.get_amount_of_transaction.return_value = 50
        employee_cls.return_value.get_fromAccount_of_transaction.return_value = 1
        employee_cls.return_value.get_toAccount_of_transaction.return_value = 2
        employee_cls.return_value.get_transaction_status.return_value = 1
        customers_cls.return_value.fund_transfers.return_value = "done"
        self._login_customer_session("alice")

        response = self.client.post(
            "/approveRequest",
            json={"customer_id": "alice", "transaction_no": 10},
        )

        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once()

    @patch("app.Customers")
    @patch("app.Employee")
    def test_reset_password_uses_force_reset_after_otp(self, employee_cls, customers_cls):
        user = customers_cls.return_value
        user.retrieve_phone_number.return_value = "+15551234567"
        user.reset_fpassword.return_value = "Password Updated"

        mock_verify = MagicMock()
        mock_verify.verification_checks.create.return_value.status = "approved"

        with patch("app.twilio_client") as twilio_client:
            twilio_client.verify.v2.services.return_value = mock_verify

            response = self.client.post(
                "/resetPassword",
                json={
                    "userid": "alice",
                    "newPassword": "new-secret",
                    "otp": "123456",
                    "requester": "Customer",
                },
            )

        self.assertEqual(response.status_code, 200)
        user.reset_fpassword.assert_called_once_with("alice", "new-secret")
        user.reset_password.assert_not_called()

    @patch("app.Employee")
    def test_deactivate_account_passes_actor_userid(self, employee_cls):
        employee_cls.return_value.deactivate_account.return_value = "Account Closed"
        self._login_tier2_session("tier2emp")

        response = self.client.post(
            "/deactivateAccount",
            json={"userid": "tier2emp", "account_no": 42},
        )

        self.assertEqual(response.status_code, 200)
        employee_cls.return_value.deactivate_account.assert_called_once_with("tier2emp", 42)


if __name__ == "__main__":
    unittest.main()
