"""Remaining high-risk route gaps: PII admin writes, MFA edges, and approval gates."""
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


class RemainingRiskRouteTests(unittest.TestCase):
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

    def test_update_employee_unauthenticated_redirects(self):
        response = self.client.post(
            "/updateEmployee",
            json={
                "userid": "admin1",
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
        self.assertIn(response.status_code, (301, 302))

    def test_update_employee_non_admin_cannot_edit_another(self):
        self._session("emp1", "employee", emp_tier=1)
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
        self.assertEqual(response.status_code, 403)

    def test_update_employee_admin_forwards_current_kwargs(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post(
                "/updateEmployee",
                json={
                    "userid": "admin1",
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
        # Route currently omits acting userid and uses phone/ssn kwargs the helper does not accept.
        emp_cls.return_value.update_employee.assert_called_once_with(
            emp_id="emp2",
            email="a@b.com",
            firstname="A",
            midname="",
            lastname="B",
            phone="1",
            dob="d",
            ssn="1",
            address="x",
        )

    def test_modify_customer_admin_can_edit_another(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.post(
                "/modifyCustomer",
                json={
                    "userid": "emp2",
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
        customers_cls.return_value.update_account_info.assert_called_once_with(
            "cust1", "L", "", "F", "1", "a@b.com", "1", "d", "x"
        )

    def test_modify_employee_admin_calls_helper(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.post(
                "/modifyEmployee",
                json={
                    "userid": "admin1",
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
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp2", "L", "", "F", "1", "a@b.com", "1", "d", "x", 2
        )

    def test_verify_otp_missing_phone_is_400(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.retrieve_phone_number.return_value = None
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Phone number could not be retrieved")

    def test_verify_otp_twilio_failure_is_500(self):
        from twilio.base.exceptions import TwilioRestException

        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.side_effect = (
                TwilioRestException(500, "/verification_checks", "boom")
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertEqual(response.status_code, 500)

    def test_verify_otp_tier2_redirects_to_tier_dashboard(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/tier2", response.headers.get("Location", ""))

    def test_deny_request_allows_userid_session_key(self):
        # Gate is `values['userid'] in session` (session keys), not session['userid'].
        with self.client.session_transaction() as sess:
            sess["cust1"] = "cust1"
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.post(
                "/denyRequest",
                json={"userid": "cust1", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(9)

    def test_get_customer_rejects_admin_usertype(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post(
            "/getCustomer",
            json={"userid": "admin1", "customer_id": "cust1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_approve_update_info_userid_mismatch(self):
        self._session("emp1", "employee", emp_tier=2)
        response = self.client.post(
            "/approveUpdateInfo",
            json={"userid": "emp2", "update_req_no": 9},
        )
        self.assertEqual(response.status_code, 401)

    def test_approve_update_info_tier2_usertype_is_forbidden(self):
        # Route only allows usertype admin/employee with emp_tier >= 2.
        self._session("emp1", "tier2", emp_tier=2)
        response = self.client.post(
            "/approveUpdateInfo",
            json={"userid": "emp1", "update_req_no": 9},
        )
        self.assertEqual(response.status_code, 403)

    def test_deny_update_info_low_tier_employee_is_forbidden(self):
        self._session("emp1", "employee", emp_tier=1)
        response = self.client.post(
            "/denyUpdateInfo",
            json={"userid": "emp1", "update_req_no": 9},
        )
        self.assertEqual(response.status_code, 403)

    def test_register_customer_self_register_uses_dashboard_redirect(self):
        with patch("app.Customers") as customers_cls, patch(
            "app.url_for", return_value="/customer_dash"
        ) as url_for:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.post(
                "/registerCustomer",
                json={
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
                },
            )
        self.assertIn(response.status_code, (301, 302))
        cust.create_customer_id.assert_called_once()
        self.assertEqual(url_for.call_args.args[0], "get_customer_dashboard_ui")

    def test_register_customer_create_failure(self):
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = -1
            response = self.client.post(
                "/registerCustomer",
                json={
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
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_open_account_employee_allowed(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "emp1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("cust1", "savings")

    def test_open_account_unauthenticated_redirects(self):
        response = self.client.post(
            "/openNewAccount",
            json={"userid": "cust1", "customer_id": "cust1", "account_type": "savings"},
        )
        self.assertIn(response.status_code, (301, 302))

    def test_approve_request_invalid_transaction_details(self):
        self._session("cust1", "customer", customer_id="cust1")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 100
            emp.get_fromAccount_of_transaction.return_value = -1
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Invalid transaction details")
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_approve_request_emp_none_amount_is_404(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp_cls.return_value.get_amount_of_transaction.return_value = None
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 404)
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_reset_password_employee_store_without_requester(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            emp_cls.return_value.reset_password.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post(
                "/resetPassword",
                json={"userid": "emp1", "newPassword": "n3w", "otp": "123456"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.reset_password.assert_called_once_with("emp1", "n3w")

    def test_make_appointment_staff_can_book_for_any_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.post(
                "/makeAppointment",
                json={"customer_id": "cust9", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with("cust9", "10:00")

    def test_load_customer_helper_failure_is_500(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 500)

    def test_send_otp_requires_body(self):
        self.assertEqual(self.client.post("/sendOTP", json={}).status_code, 400)

    def test_transaction_history_requires_fields(self):
        self._session("cust1", "customer")
        response = self.client.post("/getTransactionHistory", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)

    def test_deposit_check_requires_fields(self):
        self._session("cust1", "customer")
        response = self.client.post("/depositCheck", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
