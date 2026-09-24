"""Remaining high-risk contracts: NaN money, dual-control skip, OTP store, config drift."""
import importlib
import inspect
import math
import unittest
from unittest.mock import patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class NanAmountAndStaffUsertypeTests(unittest.TestCase):
    """`amount < 0` is false for NaN; several money routes never require customer usertype."""

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

    def test_fund_transfer_nan_amount_bypasses_negative_check(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": "NaN",
                },
            )
        self.assertEqual(response.status_code, 200)
        amount = emp_cls.return_value.add_transaction.call_args.args[2]
        self.assertTrue(math.isnan(amount))

    def test_deposit_nan_amount_bypasses_negative_check(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.post(
                "/depositAmount",
                json={"userid": "cust1", "account": 10, "amount": "NaN"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, "NaN")

    def test_withdraw_nan_amount_bypasses_negative_check(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.post(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 10, "amount": "NaN"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, "NaN")

    def test_request_funds_nan_amount_bypasses_negative_check(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.post(
                "/requestFunds",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": "NaN",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, "NaN")

    def test_cashier_cheque_nan_amount_bypasses_negative_check(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.post(
                "/getCashierCheque",
                json={
                    "userid": "cust1",
                    "to_account": 20,
                    "from_account": 10,
                    "amount": "NaN",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "cust1", 20, 10, "NaN"
        )

    def test_approve_request_emp_large_amount_does_not_escalate(self):
        # Customer /approveRequest dual-controls >1000; employee path executes immediately.
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 5000
            emp.get_employee_tier.return_value = 1
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 5000}
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 5000, 8)
        emp.transfer_transaction_to_tier2.assert_not_called()

    def test_withdraw_does_not_require_customer_usertype(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.post(
                "/withdrawAmount",
                json={"userid": "emp1", "account": 10, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, 25)

    def test_request_funds_does_not_require_customer_usertype(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.post(
                "/requestFunds",
                json={
                    "userid": "emp1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, 15)


class OtpStoreAndUnauthChequeTests(unittest.TestCase):
    """OTP store is case-sensitive; cheque routes redirect when unauthenticated."""

    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_send_otp_lowercase_customer_requester_uses_employee_store(self):
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls, patch(
            "app.twilio_client"
        ) as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "SM9"
            )
            response = self.client.post(
                "/sendOTP",
                json={"userid": "cust1", "requester": "customer"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.retrieve_phone_number.assert_called_once_with("cust1")
        customers_cls.return_value.retrieve_phone_number.assert_not_called()

    def test_reset_password_lowercase_customer_requester_uses_employee_store(self):
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls, patch(
            "app.twilio_client"
        ) as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            emp_cls.return_value.reset_fpassword.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post(
                "/resetPassword",
                json={
                    "userid": "cust1",
                    "newPassword": "n3w",
                    "otp": "123456",
                    "requester": "customer",
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.reset_fpassword.assert_called_once_with("cust1", "n3w")
        customers_cls.return_value.reset_fpassword.assert_not_called()
        emp_cls.return_value.reset_password.assert_not_called()

    def test_get_cheque_list_unauthenticated_redirects(self):
        with patch("app.Customers") as customers_cls:
            response = self.client.post("/getChequeList", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        customers_cls.return_value.get_cheque_list.assert_not_called()

    def test_deposit_check_unauthenticated_redirects(self):
        with patch("app.Customers") as customers_cls:
            response = self.client.post(
                "/depositCheck", json={"userid": "cust1", "cheque_no": 5}
            )
        self.assertIn(response.status_code, (301, 302))
        customers_cls.return_value.deposit_check.assert_not_called()

    def test_login_debug_log_includes_request_body(self):
        src = inspect.getsource(self.app_module.login)
        self.assertIn("str(values)", src)
        self.assertIn("Login attempt", src)

    def test_reset_password_debug_log_includes_request_body(self):
        src = inspect.getsource(self.app_module.reset_password)
        self.assertIn("str(values)", src)

    def test_cors_reflects_arbitrary_origin(self):
        with self.client.get("/", headers={"Origin": "https://evil.example"}) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers.get("Access-Control-Allow-Origin"),
                "https://evil.example",
            )


class MoneyHelperIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.customer_module = customer_module
        cls.employee_module = employee_module
        cls.cust_cursor = customer_module.cursor
        cls.cust_db = customer_module.db
        cls.emp_cursor = employee_module.cursor
        cls.emp_db = employee_module.db

    def setUp(self):
        for mock in (self.cust_cursor, self.cust_db, self.emp_cursor, self.emp_db):
            mock.reset_mock()
        self.cust_cursor.fetchall.side_effect = None
        self.cust_cursor.fetchone.side_effect = None
        self.cust_cursor.fetchall.return_value = []
        self.cust_cursor.fetchone.return_value = None
        self.emp_cursor.fetchall.side_effect = None
        self.emp_cursor.fetchone.side_effect = None
        self.emp_cursor.fetchall.return_value = []
        self.emp_cursor.fetchone.return_value = None
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None
        self.cust_db.rollback.side_effect = None
        self.emp_db.rollback.side_effect = None

    def tearDown(self):
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None
        self.cust_db.rollback.side_effect = None
        self.emp_db.rollback.side_effect = None

    def test_verify_customer_sql_omits_active_and_fresh_hash(self):
        from utility.encrypt import encrypt

        hashed = encrypt("secret")
        self.cust_cursor.fetchone.return_value = (hashed,)
        self.assertEqual(self.Customers().verify_customer("cust1", "secret"), 1)
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertNotIn("active", sql)
        self.assertNotIn("$2", sql)
        self.assertEqual(self.cust_cursor.execute.call_args.args[1], ("cust1",))

    def test_debit_request_negative_amount_credits_balance(self):
        self.cust_cursor.fetchall.return_value = [(100.0, 1, "checkin")]
        self.assertEqual(self.Customers().debit_request(10, -25.0), "Amount Debited")
        executed = " ".join(str(call.args[0]) for call in self.cust_cursor.execute.call_args_list)
        self.assertRegex(executed.replace(" ", ""), r"balance=balance--25")
        self.cust_db.commit.assert_called()

    def test_credit_request_negative_amount_still_updates(self):
        self.cust_cursor.fetchall.return_value = [(1,)]
        self.assertEqual(self.Customers().credit_request(10, -25.0), "Success")
        executed = " ".join(str(call.args[0]) for call in self.cust_cursor.execute.call_args_list)
        self.assertRegex(executed.replace(" ", ""), r"balance=balance\+-25")

    def test_same_account_transfer_debits_and_credits(self):
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 1, "checkin")],
        ]
        receipt = self.Customers().fund_transfers(10, 10, 40.0)
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt["from_account"], 10)
        self.assertEqual(receipt["to_account"], 10)
        executed = " ".join(str(call.args[0]) for call in self.cust_cursor.execute.call_args_list)
        self.assertIn("balance=balance-", executed)
        self.assertIn("balance=balance +", executed)

    def test_fund_transfers_commits_before_balance_mutation(self):
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 1, "checkin")],
        ]
        commit_before_update = []

        def _commit():
            executed = [str(call.args[0]) for call in self.cust_cursor.execute.call_args_list]
            commit_before_update.append(any("SET balance" in sql for sql in executed))

        self.cust_db.commit.side_effect = _commit
        self.Customers().fund_transfers(10, 20, 40.0)
        self.assertGreaterEqual(self.cust_db.commit.call_count, 2)
        self.assertIn(False, commit_before_update)

    def test_make_appointment_interpolates_time(self):
        injected = "10:00'); DROP TABLE Appointments;--"
        self.assertEqual(self.Customers().make_appointment("cust1", injected), "Appointment fixed")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn(injected, sql)
        self.assertNotIn("%s", sql)

    def test_deposit_check_interpolates_userid(self):
        injected = "cust1' OR '1'='1"
        self.cust_cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().deposit_check(injected, 5), "Invalid Cheque")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn(injected, sql)
        self.assertNotIn("%s", sql)

    def test_deny_update_info_sql_omits_userid(self):
        self.assertEqual(self.Employee().deny_update_info("attacker", 12), "Done")
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertNotIn("attacker", sql)
        self.assertNotIn("userid", sql.lower())
        self.assertIn("12", sql)

    def test_employee_connect_uses_hardcoded_credentials(self):
        src = inspect.getsource(self.employee_module)
        self.assertIn('host="localhost"', src)
        self.assertIn('user="root"', src)
        self.assertIn('password="root"', src)
        self.assertNotIn("os.getenv", src)

    def test_customer_connect_uses_env_vars(self):
        src = inspect.getsource(self.customer_module)
        self.assertIn("os.getenv('DB_HOST')", src)
        self.assertIn("os.getenv('DB_PASSWORD')", src)
        self.assertNotIn('host="localhost"', src)

    def test_nested_verify_employee_is_dead_inside_get_tier2(self):
        # Inner def is indented under getTier2_emp and is never bound on Employee.
        src = inspect.getsource(self.Employee.getTier2_emp)
        self.assertIn("def verify_employee", src)
        outer = inspect.getsource(self.Employee.verify_employee)
        self.assertIn("active=1", outer)
        self.assertNotIn("SELECT customer_id FROM Employees", outer)


if __name__ == "__main__":
    unittest.main()
