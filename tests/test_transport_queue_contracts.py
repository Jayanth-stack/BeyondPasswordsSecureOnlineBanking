"""Remaining high-risk contracts: HTTP vs Secure cookies, mutating GET, orphaned queues."""
import importlib
import inspect
import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


_CUSTOMER_REG = {
    "empid": "None",
    "userid": "cust1",
    "password": "pw",
    "email": "a@b.com",
    "firstname": "A",
    "midname": "",
    "lastname": "B",
    "phone": "4155552671",
    "dob": "2000-01-01",
    "ssn": "123456789",
    "address": "x",
}

_EMPLOYEE_REG = {
    "userid": "admin9",
    "password": "pw",
    "email": "admin9@b.com",
    "firstname": "A",
    "midname": "",
    "lastname": "B",
    "phone": "4155552671",
    "dob": "2000-01-01",
    "ssn": "123456789",
    "address": "x",
    "tier": 3,
}


class HttpSecureCookieAndMutatingGetTests(unittest.TestCase):
    """Secure cookies cannot follow the http:// redirects the money/auth flows emit."""

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

    def test_login_otp_redirect_is_http_while_cookie_is_secure(self):
        from utility.encrypt import encrypt

        self.assertTrue(self.app.config["SESSION_COOKIE_SECURE"])
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = MagicMock()
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        location = response.headers.get("Location", "")
        self.assertTrue(location.startswith("http://"), location)
        self.assertFalse(location.startswith("https://"), location)
        self.assertIn("/otp_page", location)

    def test_verify_otp_redirect_is_http_while_cookie_is_secure(self):
        self.assertTrue(self.app.config["SESSION_COOKIE_SECURE"])
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        location = response.headers.get("Location", "")
        self.assertTrue(location.startswith("http://"), location)
        self.assertFalse(location.startswith("https://"), location)

    def test_get_fund_transfer_still_queues(self):
        # Same handler is registered for GET; cookie-authenticated GET mutates money state.
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.get(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 50.0)

    def test_get_withdraw_still_debits(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 10, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, 25)

    def test_get_reset_password_still_resets_without_session(self):
        with patch("app.Customers") as customers_cls, patch("app.twilio_client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            customers_cls.return_value.reset_fpassword.return_value = "Password Updated"
            response = self.client.get(
                "/resetPassword",
                json={
                    "userid": "cust1",
                    "newPassword": "n3w",
                    "otp": "123456",
                    "requester": "Customer",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.reset_fpassword.assert_called_once_with("cust1", "n3w")
        customers_cls.return_value.reset_password.assert_not_called()


class StaffAuthAndDenyHelperTests(unittest.TestCase):
    """Staff creation and deny paths skip the helpers that would enforce role."""

    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_register_customer_staff_path_never_looks_up_employee(self):
        # Emp-id authorization is commented out; any empid other than 'None' creates.
        payload = dict(_CUSTOMER_REG, empid="emp1")
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.post("/registerCustomer", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Done")
        emp_cls.assert_not_called()
        cust.create_customer_id.assert_called_once()

    def test_unauthenticated_register_employee_accepts_admin_tier(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = 1
            response = self.client.post("/registerEmployee", json=_EMPLOYEE_REG)
        self.assertEqual(response.status_code, 200)
        args = emp.create_employee.call_args.args
        self.assertEqual(args[0], "admin9")
        self.assertEqual(args[9], 3)

    def test_deny_request_rejects_staff_session(self):
        # Route requires usertype == customer; Employee.deny_funds_requested is unused.
        with self.client.session_transaction() as sess:
            sess["userid"] = "emp1"
            sess["usertype"] = "employee"
            sess["emp_tier"] = 1
            sess["emp1"] = "emp1"
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.post(
                "/denyRequest",
                json={"userid": "emp1", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "Unauthorized access")
        customers_cls.return_value.deny_funds_requested.assert_not_called()
        emp_cls.return_value.deny_funds_requested.assert_not_called()

    def test_self_register_session_key_does_not_enable_deny_without_login(self):
        with patch("app.Customers") as customers_cls, patch(
            "app.url_for", return_value="/customer_dash"
        ):
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            register = self.client.post("/registerCustomer", json=_CUSTOMER_REG)
        self.assertIn(register.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cust1"), "cust1")
            self.assertNotIn("userid", sess)

        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            deny = self.client.post(
                "/denyRequest",
                json={"userid": "cust1", "transaction_no": 44},
            )
        self.assertIn(deny.status_code, (301, 302))
        customers_cls.return_value.deny_funds_requested.assert_not_called()

    def test_empty_amount_on_fund_transfer_raises_instead_of_400(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        with patch("app.Employee"):
            with self.assertRaises(ValueError):
                self.client.post(
                    "/fundTransfer",
                    json={
                        "userid": "cust1",
                        "fromAccount": 10,
                        "toAccount": 20,
                        "amount": "",
                    },
                )

    def test_non_numeric_account_on_fund_transfer_raises_instead_of_400(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        with patch("app.Employee"):
            with self.assertRaises(ValueError):
                self.client.post(
                    "/fundTransfer",
                    json={
                        "userid": "cust1",
                        "fromAccount": "not-an-account",
                        "toAccount": 20,
                        "amount": 10,
                    },
                )

    def test_boolean_true_amount_is_coerced_to_one(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "cust1"
            sess["usertype"] = "customer"
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 1.0)

    def test_otp_store_stays_empty_across_login(self):
        from utility.encrypt import encrypt

        self.assertEqual(self.app_module.otpSet, {})
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = MagicMock()
            self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertEqual(self.app_module.otpSet, {})


class OrphanedQueueAndConfigTests(unittest.TestCase):
    """Tier1-queued money never appears on the only staff listing that exists."""

    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.customer_module = customer_module
        cls.employee_module = employee_module
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.cust_cursor = customer_module.cursor
        cls.emp_cursor = employee_module.cursor
        cls.cust_db = customer_module.db
        cls.emp_db = employee_module.db

    def setUp(self):
        for mock in (self.cust_cursor, self.emp_cursor, self.cust_db, self.emp_db):
            mock.reset_mock()
        self.cust_cursor.fetchall.side_effect = None
        self.emp_cursor.fetchall.side_effect = None
        self.cust_cursor.fetchall.return_value = []
        self.emp_cursor.fetchall.return_value = []
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def tearDown(self):
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def test_tier1_queued_transfer_is_not_selected_by_tier2_listing(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            msg = emp.add_transaction(10, 20, 1000)
        self.assertEqual(msg, "Request to be approved by tier1 employee")
        insert_sql = self.emp_cursor.execute.call_args.args[0]
        self.assertRegex(insert_sql.replace(" ", ""), r",1,1000,1,0\)")

        self.emp_cursor.reset_mock()
        self.emp_cursor.fetchall.return_value = []
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(emp.fund_transfer_requests("t2"), "None")
        list_sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("approver2 = 2", list_sql)
        self.assertNotIn("approver2 = 1", list_sql)

    def test_deposit_tier1_queue_uses_same_unlistable_approver(self):
        msg = self.Employee().add_transaction_deposit(10, 500)
        self.assertEqual(msg, "Request to be approved by tier1 employee")
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertRegex(sql.replace(" ", ""), r",1,500,1,1\)")

    def test_deactivate_customer_never_executes_active_update(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.check_user_id.return_value = 1
            self.assertEqual(emp.deactivate_customer("emp1", 42), "Customer deactivated")
        executed = [str(call.args[0]) for call in self.emp_cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM Customers" in sql for sql in executed))
        self.assertFalse(any("SET active = 0" in sql for sql in executed))

    def test_get_cheque_list_or_join_does_not_scope_to_owner_role(self):
        self.cust_cursor.fetchall.return_value = [(5, 20, 10, 40.0, 1)]
        self.Customers().get_cheque_list("cust1")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn("from_account = ac.account_no OR c.to_account = ac.account_no", sql)
        self.assertIn("cust1", sql)

    def test_retrieve_phone_does_not_use_e164_formatter(self):
        from customer import format_phone_number

        self.cust_cursor.fetchone.return_value = ("4155552671",)
        with patch("customer.format_phone_number", wraps=format_phone_number) as formatter:
            phone = self.Customers().retrieve_phone_number("cust1")
        self.assertEqual(phone, "+14155552671")
        formatter.assert_not_called()

    def test_create_employee_non_numeric_tier_valueerrors(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=0), patch.object(
            emp, "check_existing_email", return_value=0
        ):
            with self.assertRaises(ValueError):
                emp.create_employee(
                    "emp1",
                    "L",
                    "",
                    "F",
                    "4155552671",
                    "a@b.com",
                    "pw",
                    "123456789",
                    "2000-01-01",
                    "admin",
                )
        self.emp_cursor.execute.assert_not_called()

    def test_logging_appends_audit_file_and_honors_env_path(self):
        import app as app_module

        src = inspect.getsource(app_module)
        self.assertIn("filemode='a'", src)
        self.assertIn("BANK_LOG_FILE", src)
        self.assertNotIn("filemode='w'", src)

    def test_server_binds_all_interfaces_with_debug(self):
        import app as app_module

        src = inspect.getsource(app_module)
        self.assertIn("debug=True", src)
        self.assertIn("host='0.0.0.0'", src)
        self.assertIn("ssl_context=\"adhoc\"", src)


if __name__ == "__main__":
    unittest.main()
