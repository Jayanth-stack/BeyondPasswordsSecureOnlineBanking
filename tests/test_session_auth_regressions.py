import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Keep unittest imports from truncating the tracked production log.
os.environ["BANK_LOG_FILE"] = os.path.join(tempfile.mkdtemp(), "bank.log")

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
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(99, "alice")

    @patch("app.Employee")
    def test_deny_request_allows_tier2_employee(self, employee_cls):
        employee_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
        self._login_tier2_session("tier2emp")

        response = self.client.post(
            "/denyRequest",
            json={"userid": "tier2emp", "transaction_no": 99},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Request Cancelled")
        employee_cls.return_value.deny_funds_requested.assert_called_once_with("tier2emp", 99)

    @patch("app.Customers")
    def test_deny_request_rejects_foreign_transaction(self, customers_cls):
        customers_cls.return_value.deny_funds_requested.return_value = (
            "Unauthorized or invalid transaction"
        )
        self._login_customer_session("alice")

        response = self.client.post(
            "/denyRequest",
            json={"userid": "alice", "transaction_no": 99},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["message"], "Unauthorized or invalid transaction"
        )
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(99, "alice")

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
        customers_cls.return_value.owns_pending_transaction.return_value = True
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
        customers_cls.return_value.owns_pending_transaction.assert_called_once_with(
            "alice", 10
        )
        customers_cls.return_value.fund_transfers.assert_called_once()

    @patch("app.Employee")
    @patch("app.Customers")
    def test_approve_request_rejects_foreign_transaction(self, customers_cls, employee_cls):
        customers_cls.return_value.owns_pending_transaction.return_value = False
        self._login_customer_session("alice")

        response = self.client.post(
            "/approveRequest",
            json={"customer_id": "alice", "transaction_no": 10},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["message"], "Unauthorized or invalid transaction"
        )
        customers_cls.return_value.owns_pending_transaction.assert_called_once_with(
            "alice", 10
        )
        employee_cls.return_value.get_amount_of_transaction.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_not_called()

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


class TransactionOwnershipQueryTests(unittest.TestCase):
    def setUp(self):
        from customer import cursor as cust_cursor, db as cust_db

        self.cust_cursor = cust_cursor
        self.cust_db = cust_db
        self.cust_cursor.reset_mock()
        self.cust_db.reset_mock()

    def test_deny_funds_requested_filters_by_approver(self):
        from customer import Customers

        self.cust_cursor.rowcount = 1
        result = Customers().deny_funds_requested(99, "alice")

        sql, params = self.cust_cursor.execute.call_args[0]
        self.assertIn("approver1_id", sql)
        self.assertIn("status = 1", sql)
        self.assertEqual(params, (99, "alice"))
        self.assertEqual(result, "Request Cancelled")
        self.cust_db.commit.assert_called_once()

    def test_deny_funds_requested_rejects_unowned_row(self):
        from customer import Customers

        self.cust_cursor.rowcount = 0
        result = Customers().deny_funds_requested(99, "alice")

        self.assertEqual(result, "Unauthorized or invalid transaction")
        self.cust_db.rollback.assert_called()
        self.cust_db.commit.assert_not_called()

    def test_owns_pending_transaction_queries_owner(self):
        from customer import Customers

        self.cust_cursor.fetchone.return_value = (1,)
        owned = Customers().owns_pending_transaction("alice", 10)

        sql, params = self.cust_cursor.execute.call_args[0]
        self.assertIn("approver1_id", sql)
        self.assertEqual(params, (10, "alice"))
        self.assertTrue(owned)

    def test_owns_pending_transaction_false_when_missing(self):
        from customer import Customers

        self.cust_cursor.fetchone.return_value = None
        self.assertFalse(Customers().owns_pending_transaction("alice", 10))


if __name__ == "__main__":
    unittest.main()
