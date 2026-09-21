"""Remaining high-risk contracts: MFA-skippable sessions, claimed usertype, inactive login."""
import importlib
import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


_CUSTOMER_LOGIN = {"userid": "cust1", "password": "pw", "usertype": "customer"}
_EMPLOYEE_LOGIN = {"userid": "emp1", "password": "pw", "usertype": "admin"}


class MfaSkipAndClaimedRoleTests(unittest.TestCase):
    """Password success writes session before OTP; money/admin gates never check MFA."""

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

    def test_login_client_is_constructed_with_placeholder_sid(self):
        self.assertEqual(self.app_module.account_sid, "your Account_sid")
        self.assertEqual(self.app_module.auth_token, "your Auth_token")

    def test_login_otp_uses_placeholder_client_not_env_client(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch(
            "app.client"
        ) as placeholder, patch("app.twilio_client") as env_client:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            placeholder.verify.v2.services.return_value.verifications.create.return_value = (
                MagicMock()
            )
            response = self.client.post("/login", json=_CUSTOMER_LOGIN)
        self.assertIn(response.status_code, (301, 302))
        placeholder.verify.v2.services.assert_called()
        env_client.verify.v2.services.assert_not_called()

    def test_verify_otp_uses_placeholder_client_not_env_client(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch(
            "app.client"
        ) as placeholder, patch("app.twilio_client") as env_client:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            placeholder.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        placeholder.verify.v2.services.assert_called()
        env_client.verify.v2.services.assert_not_called()

    def test_sendotp_uses_env_client_not_placeholder(self):
        with patch("app.Customers") as customers_cls, patch(
            "app.client"
        ) as placeholder, patch("app.twilio_client") as env_client:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            env_client.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "SM123"
            )
            response = self.client.post(
                "/sendOTP",
                json={"userid": "cust1", "requester": "Customer"},
            )
        self.assertEqual(response.status_code, 200)
        env_client.verify.v2.services.assert_called()
        placeholder.verify.v2.services.assert_not_called()

    def test_password_success_session_skips_otp_for_money_routes(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio, patch(
            "app.Employee"
        ) as emp_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = (
                MagicMock()
            )
            emp_cls.return_value.add_transaction.return_value = "queued"
            login = self.client.post("/login", json=_CUSTOMER_LOGIN)
            self.assertIn(login.status_code, (301, 302))
            money = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 25,
                },
            )
        self.assertEqual(money.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 25.0)

    def test_login_twilio_failure_still_authenticates_session(self):
        from twilio.base.exceptions import TwilioRestException
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio, patch(
            "app.Employee"
        ) as emp_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.side_effect = (
                TwilioRestException(500, "/verifications", "boom")
            )
            emp_cls.return_value.add_transaction.return_value = "queued"
            login = self.client.post("/login", json=_CUSTOMER_LOGIN)
            self.assertEqual(login.status_code, 500)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess.get("userid"), "cust1")
                self.assertEqual(sess.get("usertype"), "customer")
            money = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 10,
                },
            )
        self.assertEqual(money.status_code, 200)

    def test_login_missing_phone_still_authenticates_session(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = None
            login = self.client.post("/login", json=_CUSTOMER_LOGIN)
        self.assertEqual(login.status_code, 400)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("userid"), "cust1")
            self.assertEqual(sess.get("usertype"), "customer")

    def test_claimed_admin_usertype_passes_system_log_gate(self):
        from utility.encrypt import encrypt

        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp = emp_cls.return_value
            emp.retrieve_hashed_password.return_value = encrypt("pw")
            emp.get_employee_tier.return_value = 1
            emp.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = (
                MagicMock()
            )
            login = self.client.post("/login", json=_EMPLOYEE_LOGIN)
        self.assertIn(login.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("usertype"), "admin")
            self.assertEqual(sess.get("emp_tier"), 1)
        response = self.client.post("/getSystemLogs", json={"userid": "emp1"})
        # Admin usertype gate passed; file send / Flask 3 path kwarg may still 500.
        self.assertNotIn(response.status_code, (301, 302, 401, 403))

    def test_verify_otp_empty_json_object_is_not_session_error(self):
        self._session("cust1", "customer")
        response = self.client.post("/verify-otp", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "OTP code is required")

    def test_verify_otp_null_json_body_attributeerrors(self):
        # Route does not guard `values.get` when get_json() returns None.
        self._session("cust1", "customer")
        with self.assertRaises(AttributeError):
            self.client.post(
                "/verify-otp",
                data="null",
                content_type="application/json",
            )

    def test_deactivate_customer_does_not_require_userid_match(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_customer.return_value = "Customer deactivated"
            response = self.client.post(
                "/deactivateCustomer",
                json={"userid": "emp9", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_customer.assert_called_once_with("emp9", "cust1")

    def test_logout_unauthenticated_rejects_userid_mismatch(self):
        response = self.client.post("/logout", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 401)

    def test_update_info_customer_can_spoof_employee_requester(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_info_reqest.return_value = (
                "Update Info Request Placed"
            )
            response = self.client.post(
                "/updateInfo",
                json={
                    "userid": "cust1",
                    "email": "a@b.com",
                    "contact_no": "1",
                    "address": "x",
                    "requester": "Employee",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_info_reqest.assert_called_once_with(
            "Employee", "cust1", "a@b.com", "1", "x"
        )

    def test_get_appointment_list_staff_can_read_any_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [
                (1, "cust9", "10:00", 1)
            ]
            response = self.client.post(
                "/getAppointmentList", json={"customer_id": "cust9"}
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_appointment.assert_called_once_with("cust9")

    def test_register_employee_create_failure_is_still_200(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = -1
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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], -1)

    def test_request_funds_zero_amount_is_accepted(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.post(
                "/requestFunds",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 0,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, 0)

    def test_deposit_amount_does_not_require_customer_usertype(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.post(
                "/depositAmount",
                json={"userid": "emp1", "account": 10, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, 40)


class InactiveLoginAndSqlInterpolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import customer as customer_module
        import employee as employee_module

        importlib.reload(customer_module)
        importlib.reload(employee_module)
        cls.Customers = customer_module.Customers
        cls.Employee = employee_module.Employee
        cls.customer_module = customer_module
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

    def tearDown(self):
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def test_customer_password_lookup_does_not_filter_active(self):
        self.cust_cursor.fetchone.return_value = ("$2b$hash",)
        self.assertEqual(self.Customers().retrieve_hashed_password("cust1"), "$2b$hash")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertNotIn("active", sql)

    def test_employee_password_lookup_does_not_filter_active(self):
        self.emp_cursor.fetchone.return_value = ("$2b$hash",)
        self.assertEqual(self.Employee().retrieve_hashed_password("emp1"), "$2b$hash")
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertNotIn("active", sql)

    def test_retrieve_phone_prefixes_invalid_national_without_e164(self):
        from customer import format_phone_number

        self.cust_cursor.fetchone.return_value = ("not-a-phone",)
        self.assertEqual(self.Customers().retrieve_phone_number("cust1"), "+1not-a-phone")
        self.assertIsNone(format_phone_number("not-a-phone"))

    def test_create_customer_interpolates_last_name(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=0), patch.object(
            customer, "check_existing_contact", return_value=0
        ), patch.object(customer, "check_existing_ssn", return_value=0), patch.object(
            customer, "check_existing_email", return_value=0
        ):
            self.assertEqual(
                customer.create_customer_id(
                    "cust1",
                    "O'Brien",
                    "",
                    "F",
                    "4155552671",
                    "a@b.com",
                    "pw",
                    "123456789",
                    "2000-01-01",
                ),
                1,
            )
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn("O'Brien", sql)
        self.assertNotIn("%s", sql)

    def test_customer_force_reset_interpolates_userid(self):
        customer = self.Customers()
        with patch.object(customer, "check_user_id", return_value=1):
            self.assertEqual(customer.reset_fpassword("cust'1", "n3w"), "Password Updated")
        sql = self.cust_cursor.execute.call_args.args[0]
        self.assertIn("cust'1", sql)
        self.assertNotIn("%s", sql)

    def test_employee_force_reset_interpolates_userid(self):
        emp = self.Employee()
        with patch.object(emp, "check_user_id", return_value=1):
            self.assertEqual(emp.reset_fpassword("emp'1", "n3w"), "Password Updated")
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("emp'1", sql)

    def test_transfer_receipt_timestamp_uses_getdate_not_iso(self):
        customer = self.Customers()
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 1, "checkin")],
        ]
        frozen = "16/09/2026 10:00:00"
        with patch.object(self.customer_module, "getdate", return_value=frozen):
            result = customer.fund_transfers(10, 20, 25.0)
        self.assertEqual(result["timestamp"], frozen)
        self.assertFalse(result["timestamp"].endswith("Z"))
        self.assertNotIn("T", result["timestamp"])

    def test_employee_requester_update_escalates_to_admin_approver(self):
        msg = self.Customers().update_info_reqest(
            "Employee", "cust1", "a@b.com", "1", "x"
        )
        self.assertEqual(msg, "Update Info Request Placed")
        sql = self.cust_cursor.execute.call_args.args[0]
        compact = sql.replace(" ", "")
        self.assertIn(",3)", compact)
        self.assertIn("'Employee'", sql)


class OtpInterfaceGapTests(unittest.TestCase):
    def test_send_otp_does_not_touch_totp(self):
        from otp import OtpInterface

        iface = OtpInterface()
        totp = MagicMock()
        iface.totp = totp
        self.assertIsNone(iface.send_otp("4155552671"))
        totp.assert_not_called()
        totp.verify.assert_not_called()

    def test_get_obj_treats_totp_as_callable(self):
        from otp import OtpInterface

        iface = OtpInterface()
        with self.assertRaises(TypeError):
            iface.getObj()


if __name__ == "__main__":
    unittest.main()
