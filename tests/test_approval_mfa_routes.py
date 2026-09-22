"""Remaining high-risk route gaps: money approval happy paths, MFA, and session gates."""
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


class ApprovalAndMfaRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid, usertype, emp_tier=None, customer_id=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier
            if customer_id is not None:
                sess["customer_id"] = customer_id

    def test_approve_request_emp_executes_open_transfer(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 75.0
            emp.get_employee_tier.return_value = 2
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {
                "amount": 75.0,
                "signature": "abc",
            }
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 75.0, 44)
        self.assertEqual(response.get_json()["message"]["signature"], "abc")

    def test_approve_request_customer_escalates_over_threshold(self):
        self._session("cust1", "customer", customer_id="cust1")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp_cls.return_value.get_amount_of_transaction.return_value = 1500
            emp_cls.return_value.transfer_transaction_to_tier2.return_value = (
                "Request Sent to Tier2 employee"
            )
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.transfer_transaction_to_tier2.assert_called_once_with(8)
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_approve_request_customer_executes_at_or_below_threshold(self):
        self._session("cust1", "customer", customer_id="cust1")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 1000
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 1000}
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        emp.transfer_transaction_to_tier2.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 1000, 8)

    def test_deny_request_requires_body_and_fields(self):
        self.assertEqual(self.client.post("/denyRequest", json={}).status_code, 400)
        response = self.client.post("/denyRequest", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Some data missing")

    def test_deny_request_unauthenticated_redirects(self):
        response = self.client.post(
            "/denyRequest",
            json={"userid": "cust1", "transaction_no": 1},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_login_missing_phone_is_rejected(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = None
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "No phone number available")

    def test_login_twilio_failure_is_500(self):
        from twilio.base.exceptions import TwilioRestException
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.side_effect = (
                TwilioRestException(500, "/verifications", "boom")
            )
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], "Failed to send OTP")

    def test_login_success_redirects_to_otp_page(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = MagicMock()
            response = self.client.post(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/otp_page", response.headers.get("Location", ""))

    def test_verify_otp_rejects_invalid_code(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "pending"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "000000"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Invalid OTP")

    def test_verify_otp_customer_redirects_to_dashboard(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/customer_dash", response.headers.get("Location", ""))

    def test_verify_otp_admin_redirects_to_admin_dashboard(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/admin", response.headers.get("Location", ""))

    def test_send_otp_customer_uses_customer_store(self):
        with patch("app.Customers") as customers_cls, patch("app.twilio_client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = "SM123"
            response = self.client.post(
                "/sendOTP",
                json={"userid": "cust1", "requester": "Customer"},
            )
        customers_cls.return_value.retrieve_phone_number.assert_called_once_with("cust1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sid"], "SM123")

    def test_send_otp_defaults_to_employee_store(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = "SM9"
            response = self.client.post("/sendOTP", json={"userid": "emp1"})
        emp_cls.return_value.retrieve_phone_number.assert_called_once_with("emp1")
        self.assertEqual(response.status_code, 200)

    def test_reset_password_rejects_failed_otp(self):
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

    def test_reset_password_approved_otp_calls_reset(self):
        with patch("app.Customers") as customers_cls, patch("app.twilio_client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            customers_cls.return_value.reset_fpassword.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post(
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

    def test_get_cheque_list_rejects_employee_session(self):
        self._session("emp1", "tier1", emp_tier=1)
        response = self.client.post("/getChequeList", json={"userid": "emp1"})
        self.assertIn(response.status_code, (301, 302))

    def test_get_cheque_list_userid_mismatch_redirects(self):
        self._session("cust1", "customer")
        response = self.client.post("/getChequeList", json={"userid": "cust2"})
        self.assertIn(response.status_code, (301, 302))

    def test_get_cheque_list_returns_customer_cheques(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_cheque_list.return_value = [(1, 20, 10, 50.0, 1)]
            response = self.client.post("/getChequeList", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"][0][0], 1)

    def test_make_appointment_customer_mismatch(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/makeAppointment",
            json={"customer_id": "cust2", "time": "10:00"},
        )
        self.assertEqual(response.status_code, 401)

    def test_make_appointment_success(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.post(
                "/makeAppointment",
                json={"customer_id": "cust1", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with("cust1", "10:00")

    def test_get_appointment_list_unauthenticated_redirects(self):
        response = self.client.post("/getAppointmentList", json={"customer_id": "cust1"})
        self.assertIn(response.status_code, (301, 302))

    def test_modify_employee_rejects_non_staff(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/modifyEmployee",
            json={
                "userid": "cust1",
                "emp_id": "emp1",
                "last_name": "L",
                "middle_name": "",
                "first_name": "F",
                "contact_no": "1",
                "email_id": "a@b.com",
                "ssn": "1",
                "dob": "d",
                "address": "x",
                "tier": 1,
            },
        )
        self.assertIn(response.status_code, (301, 302))

    def test_modify_employee_non_admin_cannot_edit_another(self):
        self._session("emp1", "employee", emp_tier=1)
        response = self.client.post(
            "/modifyEmployee",
            json={
                "userid": "emp2",
                "emp_id": "emp2",
                "last_name": "L",
                "middle_name": "",
                "first_name": "F",
                "contact_no": "1",
                "email_id": "a@b.com",
                "ssn": "1",
                "dob": "d",
                "address": "x",
                "tier": 2,
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_open_account_rejects_tier1_usertype(self):
        self._session("emp1", "tier1", emp_tier=1)
        response = self.client.post(
            "/openNewAccount",
            json={"userid": "emp1", "customer_id": "cust1", "account_type": "savings"},
        )
        self.assertEqual(response.status_code, 403)

    def test_load_employee_rejects_customer(self):
        self._session("cust1", "customer")
        response = self.client.post("/loadEmployee")
        self.assertEqual(response.status_code, 401)

    def test_load_employee_returns_staff_payload(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {"tier": 2}
            emp_cls.return_value.fund_transfer_requests.return_value = "None"
            emp_cls.return_value.update_info_request_list.return_value = 0
            response = self.client.post("/loadEmployee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["tier"], 2)

    def test_get_system_logs_missing_file(self):
        self._session("admin1", "admin")
        with patch("app.os.path.exists", return_value=False):
            response = self.client.post("/getSystemLogs", json={"userid": "admin1"})
        self.assertEqual(response.status_code, 404)

    def test_register_employee_rejects_duplicate_ssn(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 1
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
        emp.check_existing_ssn.assert_called_once_with("123456789")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "SSN already registered")

    def test_deactivate_account_authorized_calls_helper(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_account.return_value = "Account Closed"
            response = self.client.post(
                "/deactivateAccount",
                json={"userid": "emp1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_account.assert_called_once_with("emp1", 10)


if __name__ == "__main__":
    unittest.main()
