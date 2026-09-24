"""Session-shape auth, account IDOR, and money-integrity contracts still untested."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class LoginShapedSessionGateTests(unittest.TestCase):
    """Login stores session['userid']; several money gates look for other keys."""

    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _login_shaped(self, userid="cust1", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_deny_request_login_session_matches_userid(self):
        self._login_shaped()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.post(
                "/denyRequest",
                json={"userid": "cust1", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(9)

    def test_approve_request_login_session_uses_userid_not_customer_id_key(self):
        self._login_shaped()
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 8)

    def test_logout_clears_session_userid(self):
        self._login_shaped()
        response = self.client.post("/logout", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertNotIn("userid", sess)
            self.assertNotIn("usertype", sess)


class AccountIdorRouteTests(unittest.TestCase):
    """Money routes bind identity to JSON userid, not to the account numbers."""

    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid, usertype, emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_open_account_customer_can_open_for_another_customer_id(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust9",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("cust9", "savings")

    def test_fund_transfer_does_not_verify_from_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 999,
                    "toAccount": 20,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(999, 20, 50.0)

    def test_withdraw_does_not_verify_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.post(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 888, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(888, 25)

    def test_deposit_does_not_verify_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.post(
                "/depositAmount",
                json={"userid": "cust1", "account": 777, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(777, 40)

    def test_request_funds_does_not_verify_from_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.post(
                "/requestFunds",
                json={
                    "userid": "cust1",
                    "fromAccount": 111,
                    "toAccount": 222,
                    "amount": 30,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(111, 222, 30)

    def test_transaction_history_does_not_verify_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_transaction_history.return_value = [("$1",)]
            response = self.client.post(
                "/getTransactionHistory",
                json={"userid": "cust1", "account_no": 555},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_transaction_history.assert_called_once_with(555)

    def test_cashier_cheque_does_not_verify_from_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.post(
                "/getCashierCheque",
                json={
                    "userid": "cust1",
                    "to_account": 20,
                    "from_account": 444,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "cust1", 20, 444, 15
        )

    def test_cashier_cheque_allows_employee_session(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.post(
                "/getCashierCheque",
                json={
                    "userid": "emp1",
                    "to_account": 20,
                    "from_account": 10,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once()

    def test_fund_transfer_non_numeric_amount_raises(self):
        self._session("cust1", "customer")
        with self.assertRaises(ValueError):
            self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": "not-a-number",
                },
            )

    def test_withdraw_non_numeric_amount_raises(self):
        self._session("cust1", "customer")
        with self.assertRaises(ValueError):
            self.client.post(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 10, "amount": "abc"},
            )

    def test_open_account_helper_failure_is_500(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.side_effect = RuntimeError("db down")
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], "Failed to open new account")

    def test_get_appointment_list_none_string_is_200(self):
        # Helper returns the string 'None', which is truthy, so the 404 branch is dead.
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = "None"
            response = self.client.post(
                "/getAppointmentList",
                json={"customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "None")

    def test_approve_request_missing_amount_is_not_none_string(self):
        # Helper returns -1 for missing txn; route only treats the string 'None' as missing.
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = -1
            emp.get_fromAccount_of_transaction.return_value = -1
            emp.get_toAccount_of_transaction.return_value = -1
            emp.get_transaction_status.return_value = -1
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Invalid transaction details")
        self.assertNotEqual(response.get_json()["message"], "Wrong Transaction number")
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_approve_request_emp_missing_amount_is_not_none(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = -1
            emp.get_employee_tier.return_value = 2
            emp.get_fromAccount_of_transaction.return_value = -1
            emp.get_toAccount_of_transaction.return_value = -1
            emp.get_transaction_status.return_value = -1
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Invalid transaction_no")
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_reset_password_rejected_otp_does_not_change_password(self):
        with patch("app.Customers") as customers_cls, patch("app.twilio_client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "pending"
            )
            response = self.client.post(
                "/resetPassword",
                json={
                    "userid": "cust1",
                    "newPassword": "n3w",
                    "otp": "000000",
                    "requester": "Customer",
                },
            )
        self.assertEqual(response.status_code, 401)
        customers_cls.return_value.reset_password.assert_not_called()

    def test_login_success_does_not_update_login_history(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = object()
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        customers_cls.return_value.update_login_history.assert_not_called()

    def test_send_otp_employee_forwards_unprefixed_phone(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "4155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "VA1"
            )
            response = self.client.post("/sendOTP", json={"userid": "emp1"})
        self.assertEqual(response.status_code, 200)
        twilio.verify.v2.services.return_value.verifications.create.assert_called_once_with(
            to="4155552671", channel="sms"
        )

    def test_modify_employee_requires_body(self):
        self._session("admin1", "admin", emp_tier=3)
        self.assertEqual(self.client.post("/modifyEmployee", json={}).status_code, 400)

    def test_deactivate_account_requires_body(self):
        self._session("emp1", "tier2", emp_tier=2)
        self.assertEqual(self.client.post("/deactivateAccount", json={}).status_code, 400)

    def test_fund_transfer_requires_body(self):
        self._session("cust1", "customer")
        self.assertEqual(self.client.post("/fundTransfer", json={}).status_code, 400)


class MoneyIntegrityHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.customer_cursor = customer_module.cursor
        cls.customer_db = customer_module.db
        cls.employee_cursor = employee_module.cursor
        cls.employee_db = employee_module.db

    def setUp(self):
        self.customer_cursor.reset_mock()
        self.customer_db.reset_mock()
        self.employee_cursor.reset_mock()
        self.employee_db.reset_mock()
        self.customer_cursor.fetchall.side_effect = None
        self.customer_cursor.fetchall.return_value = []
        self.employee_cursor.fetchall.side_effect = None
        self.employee_cursor.fetchall.return_value = []
        self.customer_db.commit.side_effect = None
        self.employee_db.commit.side_effect = None

    def tearDown(self):
        self.customer_db.commit.side_effect = None
        self.employee_db.commit.side_effect = None

    def test_make_cashier_check_does_not_debit_sender(self):
        customer = self.Customers()
        with patch.object(customer, "verify_account", return_value=1):
            self.assertEqual(customer.make_cashier_check("cust1", 20, 10, 50), "Success")
        executed = " ".join(str(call.args[0]) for call in self.customer_cursor.execute.call_args_list)
        self.assertIn("INSERT INTO Cheque", executed)
        self.assertNotIn("balance=", executed)

    def test_update_account_info_stores_plaintext_ssn(self):
        from utility.encrypt import encrypt_ssn

        self.assertEqual(
            self.Customers().update_account_info(
                "cust1", "L", "", "F", "1", "a@b.com", "123456789", "dob", "addr"
            ),
            "updated",
        )
        sql = self.customer_cursor.execute.call_args.args[0]
        self.assertIn("123456789", sql)
        self.assertNotIn(encrypt_ssn("123456789"), sql)

    def test_deactivate_account_never_executes_active_update(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            self.assertEqual(emp.deactivate_account("emp1", 10), "Account Closed")
        executed = [str(call.args[0]) for call in self.employee_cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM Accounts" in sql for sql in executed))
        self.assertFalse(any("SET active = 0" in sql for sql in executed))

    def test_get_all_account_unknown_type_is_added_as_key(self):
        self.customer_cursor.fetchall.return_value = [(99, "brokerage", 12.5)]
        result = self.Customers().get_all_account("cust1")
        self.assertEqual(result["brokerage"], {"Account": 99, "Balance": 12.5})
        self.assertEqual(result["checkin"], "None")

    def test_update_info_request_list_empty_returns_zero(self):
        self.employee_cursor.fetchall.return_value = []
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=1):
            self.assertEqual(emp.update_info_request_list("emp1"), 0)

    def test_getdate_formats_differ_between_modules(self):
        import customer as customer_module
        import employee as employee_module
        from datetime import datetime

        frozen = datetime(2026, 9, 15, 10, 23, 13)
        with patch("customer.datetime") as cust_dt, patch("employee.datetime") as emp_dt:
            cust_dt.now.return_value = frozen
            emp_dt.now.return_value = frozen
            self.assertEqual(customer_module.getdate(), "15/09/2026 10:23:13")
            self.assertEqual(employee_module.getdate(), "2026-09-15")


if __name__ == "__main__":
    unittest.main()
