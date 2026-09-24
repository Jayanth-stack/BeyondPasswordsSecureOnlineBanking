"""Remaining high-risk contracts: identity uniqueness, unused approval tier, money integrity."""
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


_CUSTOMER_REG = {
    "empid": "emp1",
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


class IdentityAndSessionGapTests(unittest.TestCase):
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

    def test_register_customer_ssn_uniqueness_uses_email_checker(self):
        # Route uniqueness-checks SSN via check_existing_email, not check_existing_ssn.
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.side_effect = lambda value: int(value == "123456789")
            response = self.client.post("/registerCustomer", json=_CUSTOMER_REG)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "SSN already registered")
        cust.check_existing_ssn.assert_not_called()
        self.assertEqual(cust.check_existing_email.call_args_list[-1].args[0], "123456789")
        cust.create_customer_id.assert_not_called()

    def test_register_customer_rejects_duplicate_email(self):
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 1
            response = self.client.post("/registerCustomer", json=_CUSTOMER_REG)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Email already registered")
        cust.create_customer_id.assert_not_called()

    def test_register_customer_requires_body(self):
        response = self.client.post("/registerCustomer", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "No data Found")

    def test_register_employee_requires_body(self):
        response = self.client.post("/registerEmployee", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "No data Found")

    def test_register_employee_rejects_duplicate_email(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 1
            response = self.client.post(
                "/registerEmployee",
                json={
                    "userid": "emp1",
                    "password": "pw",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 1,
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Email already registered")
        emp.create_employee.assert_not_called()

    def test_approve_request_emp_does_not_enforce_fetched_tier(self):
        # Tier is loaded but never used; a matching session userid is enough.
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 40.0
            emp.get_employee_tier.return_value = 1
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 40.0}
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 200)
        emp.get_employee_tier.assert_called_once_with("emp1")
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 40.0, 44)

    def test_update_employee_allows_self_edit(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post(
                "/updateEmployee",
                json={
                    "userid": "emp1",
                    "emp_id": "emp1",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "1",
                    "dob": "d",
                    "ssn": "1",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_employee.assert_called_once()

    def test_update_employee_hr_can_edit_another(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post(
                "/updateEmployee",
                json={
                    "userid": "emp2",
                    "emp_id": "emp2",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "1",
                    "dob": "d",
                    "ssn": "1",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 200)

    def test_update_employee_missing_fields(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post(
            "/updateEmployee",
            json={"userid": "admin1", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.get_json()["missing_fields"])

    def test_get_employee_missing_details_is_treated_as_found(self):
        # Helper returns the string "None"; the route treats any truthy value as a hit.
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = "None"
            response = self.client.post(
                "/getEmployee",
                json={"userid": "admin1", "emp_id": "ghost"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"], "None")

    def test_get_customer_unauthenticated_redirects(self):
        response = self.client.post(
            "/getCustomer",
            json={"userid": "emp1", "customer_id": "cust1"},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_get_customer_does_not_require_userid_match(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {}
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "A"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "someone-else", "customer_id": "cust9"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_customer_details.assert_called_once_with("cust9")

    def test_deny_update_info_does_not_require_userid_match(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Done"
            response = self.client.post(
                "/denyUpdateInfo",
                json={"userid": "emp9", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("emp9", 9)

    def test_modify_customer_missing_userid_errors_for_non_admin(self):
        # userid is not in required[], but the privilege check still reads it.
        self._session("emp1", "employee", emp_tier=1)
        with self.assertRaises(KeyError):
            self.client.post(
                "/modifyCustomer",
                json={
                    "customer_id": "cust1",
                    "last_name": "L",
                    "middle_name": "",
                    "first_name": "F",
                    "contact_no": "1",
                    "email_id": "a@b.com",
                    "ssn": "1",
                    "dob": "d",
                    "address": "x",
                },
            )

    def test_modify_customer_employee_can_edit_when_userid_matches(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.post(
                "/modifyCustomer",
                json={
                    "userid": "emp1",
                    "customer_id": "cust1",
                    "last_name": "L",
                    "middle_name": "",
                    "first_name": "F",
                    "contact_no": "1",
                    "email_id": "a@b.com",
                    "ssn": "1",
                    "dob": "d",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once()

    def test_get_appointment_list_staff_can_read_any_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [
                (1, "cust9", "10:00", 1)
            ]
            response = self.client.post(
                "/getAppointmentList",
                json={"customer_id": "cust9"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_appointment.assert_called_once_with("cust9")

    def test_send_otp_requires_userid(self):
        response = self.client.post("/sendOTP", json={"requester": "Customer"})
        self.assertEqual(response.status_code, 400)

    def test_verify_otp_employee_without_tier_defaults_to_tier1(self):
        self._session("emp1", "employee")
        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/tier1", response.headers.get("Location", ""))

    def test_get_system_logs_requires_fields(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post("/getSystemLogs", json={})
        self.assertEqual(response.status_code, 400)

    def test_update_info_unauthenticated_redirects(self):
        response = self.client.post(
            "/updateInfo",
            json={
                "userid": "cust1",
                "email": "a@b.com",
                "contact_no": "1",
                "address": "x",
                "requester": "Customer",
            },
        )
        self.assertIn(response.status_code, (301, 302))

    def test_request_funds_unauthenticated_redirects(self):
        response = self.client.post(
            "/requestFunds",
            json={"userid": "cust1", "fromAccount": 1, "toAccount": 2, "amount": 10},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_withdraw_unauthenticated_redirects(self):
        response = self.client.post(
            "/withdrawAmount",
            json={"userid": "cust1", "account": 1, "amount": 10},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_deposit_unauthenticated_redirects(self):
        response = self.client.post(
            "/depositAmount",
            json={"userid": "cust1", "account": 1, "amount": 10},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_load_employee_helper_failure_is_500(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.side_effect = RuntimeError("db down")
            response = self.client.post("/loadEmployee")
        self.assertEqual(response.status_code, 500)

    def test_transaction_history_helper_failure_is_500(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_transaction_history.side_effect = RuntimeError(
                "db down"
            )
            response = self.client.post(
                "/getTransactionHistory",
                json={"userid": "cust1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 500)

    def test_logout_missing_userid_field(self):
        response = self.client.post("/logout", json={"usertype": "customer"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_session_cookie_flags(self):
        self.assertTrue(self.app.config["SESSION_COOKIE_SECURE"])
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_get_employee_rejects_tier2_usertype(self):
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post(
            "/getEmployee",
            json={"userid": "emp1", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 403)

    def test_approve_request_requires_fields(self):
        self._session("cust1", "customer")
        response = self.client.post("/approveRequest", json={"customer_id": "cust1"})
        self.assertEqual(response.status_code, 400)

    def test_get_cheque_list_requires_body(self):
        self._session("cust1", "customer")
        response = self.client.post("/getChequeList", json={})
        self.assertEqual(response.status_code, 400)

    def test_make_appointment_unauthenticated_redirects(self):
        response = self.client.post(
            "/makeAppointment",
            json={"customer_id": "cust1", "time": "10:00"},
        )
        self.assertIn(response.status_code, (301, 302))


class MoneyIntegrityGapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.cust_cursor = customer_module.cursor
        cls.cust_db = customer_module.db
        cls.emp_cursor = employee_module.cursor
        cls.emp_db = employee_module.db

    def setUp(self):
        for mock in (self.cust_cursor, self.cust_db, self.emp_cursor, self.emp_db):
            mock.reset_mock()
        self.cust_cursor.fetchall.side_effect = None
        self.cust_cursor.fetchall.return_value = []
        self.emp_cursor.fetchall.side_effect = None
        self.emp_cursor.fetchall.return_value = []
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None
        self.cust_db.rollback.side_effect = None
        self.emp_db.rollback.side_effect = None

    def tearDown(self):
        # reset_mock() does not clear side_effect; later suites share these mocks.
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None
        self.cust_db.rollback.side_effect = None
        self.emp_db.rollback.side_effect = None

    def test_add_transaction_truncates_fractional_amount(self):
        emp = self.Employee()
        with patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            emp.add_transaction(10, 20, 100.9)
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertNotIn("100.9", sql)
        self.assertRegex(sql.replace(" ", ""), r",100,1,0\)")

    def test_add_transaction_deposit_truncates_fractional_amount(self):
        self.Employee().add_transaction_deposit(10, 50.9)
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertNotIn("50.9", sql)
        self.assertRegex(sql.replace(" ", ""), r",50,1,1\)")

    def test_deposit_queue_does_not_verify_account_exists(self):
        with patch("employee.Customers") as customers_cls:
            self.Employee().add_transaction_deposit(10, 50)
            customers_cls.return_value.verify_account.assert_not_called()

    def test_deactivate_customer_string_id_typeerrors_before_existence_check(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            with self.assertRaises(TypeError):
                emp.deactivate_customer("emp1", "cust1")
            customers_cls.return_value.check_user_id.assert_not_called()

    def test_check_existing_ssn_looks_up_plaintext_not_hash(self):
        from utility.encrypt import encrypt_ssn

        hashed = encrypt_ssn("123456789")
        self.cust_cursor.fetchall.return_value = []
        self.Customers().check_existing_ssn("123456789")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn("ssn='123456789'", sql)
        self.assertNotIn(hashed, sql)

    def test_deposit_flag_closes_pending_transaction(self):
        customer = self.Customers()
        self.cust_cursor.fetchall.side_effect = [[(1,)], [(1,)]]
        result = customer.fund_transfers(10, 20, 25.0, transaction_no=9)
        self.assertIsInstance(result, dict)
        executed = [str(call.args[0]) for call in self.cust_cursor.execute.call_args_list]
        self.assertTrue(any("status=0" in sql and "9" in sql for sql in executed))

    def test_open_account_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        customer = self.Customers()
        with patch.object(customer, "check_account", return_value=0):
            self.assertEqual(customer.open_account("cust1", "savings"), "Try Again")
        self.cust_db.rollback.assert_called()

    def test_make_cashier_check_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        customer = self.Customers()
        with patch.object(customer, "verify_account", return_value=1):
            self.assertEqual(customer.make_cashier_check("cust1", 20, 10, 50), "Fail")
        self.cust_db.rollback.assert_called()

    def test_get_all_account_commit_failure_rolls_back(self):
        self.cust_cursor.fetchall.return_value = [(11, "checkin", 100.0)]
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Customers().get_all_account("cust1"), "Try again later")
        self.cust_db.rollback.assert_called()

    def test_update_account_info_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(
            self.Customers().update_account_info(
                "cust1", "L", "", "F", "1", "a@b.com", "123456789", "dob", "addr"
            ),
            "Try Again Later",
        )
        self.cust_db.rollback.assert_called()

    def test_update_info_request_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(
            self.Customers().update_info_reqest("Customer", "cust1", "a@b.com", "1", "x"),
            "Try Again Later",
        )
        self.cust_db.rollback.assert_called()

    def test_deny_funds_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(
            self.Customers().deny_funds_requested(42, "cust1"),
            "Please try again later",
        )
        self.cust_db.rollback.assert_called()

    def test_reset_password_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        customer = self.Customers()
        with patch.object(customer, "verify_customer", return_value=1):
            self.assertEqual(customer.reset_password("cust1", "old", "new"), "Try Again Later")
        self.cust_db.rollback.assert_called()

    def test_reset_fpassword_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=1):
            self.assertEqual(customer.reset_fpassword("cust1", "new"), "Try Again Later")
        self.cust_db.rollback.assert_called()

    def test_get_cheque_list_commit_failure_still_returns_rows(self):
        rows = [(5, 20, 10, 40.0, 1)]
        self.cust_cursor.fetchall.return_value = rows
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Customers().get_cheque_list("cust1"), rows)
        self.cust_db.rollback.assert_called()

    def test_make_appointment_commit_failure_rolls_back(self):
        self.cust_db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Customers().make_appointment("cust1", "10:00"), "Try again later")
        self.cust_db.rollback.assert_called()

    def test_employee_create_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.emp_db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "check_user_id", return_value=0), patch.object(
            emp, "check_existing_contact", return_value=0
        ), patch.object(emp, "check_existing_ssn", return_value=0), patch.object(
            emp, "check_existing_email", return_value=0
        ):
            self.assertEqual(
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
                    1,
                ),
                -1,
            )
        self.emp_db.rollback.assert_called()

    def test_employee_deny_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.emp_db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(emp.deny_funds_requested("emp1", 9), "Please try again later")
        self.emp_db.rollback.assert_called()

    def test_transfer_to_tier2_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.emp_db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "getTier2_emp", return_value="t2"):
            self.assertEqual(emp.transfer_transaction_to_tier2(77), 0)
        self.emp_db.rollback.assert_called()

    def test_employee_handle_appointment_requires_missing_emp_id(self):
        with self.assertRaises(AttributeError):
            self.Employee().handle_appointment()

    def test_employee_system_logs_requires_missing_tier(self):
        with self.assertRaises(AttributeError):
            self.Employee().system_logs()

    def test_get_customer_id_from_account_returns_owner(self):
        self.cust_cursor.fetchall.return_value = [("cust1",)]
        self.assertEqual(self.Customers().get_customerID_from_account(10), "cust1")

    def test_employee_reset_password_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.emp_db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "verify_employee", return_value=1):
            self.assertEqual(emp.reset_password("emp1", "old", "new"), "Try Again Later")
        self.emp_db.rollback.assert_called()

    def test_employee_deactivate_account_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.emp_db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "get_employee_tier", return_value=2), patch.object(
            emp, "getTier2_emp", return_value="t2"
        ), patch("employee.Customers") as customers_cls:
            customers_cls.return_value.verify_account.return_value = 1
            self.assertEqual(emp.deactivate_account("emp1", 10), "Account cannot be Closed")
        self.emp_db.rollback.assert_called()


if __name__ == "__main__":
    unittest.main()
