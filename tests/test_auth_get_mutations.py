"""GET still mutates identity routes that money-route GET PRs never locked.

Unique vs still-open coverage PRs #203/#201/#199/#197/#195/#193/#191/#189:
those cover ACH/wire snapshot GET settle/expire/force-verify and money
GET mutations (fundTransfer, withdraw, deposit, approve, resetPassword,
sendOTP). They do not lock GET /registerCustomer, /registerEmployee,
/login, /verify-otp, /logout, or /modifyCustomer.
"""
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

_EMPLOYEE_REG = {
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
}

_MODIFY_CUSTOMER = {
    "userid": "emp1",
    "customer_id": "cust9",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "1",
    "dob": "d",
    "address": "x",
}


class AuthGetMutationTests(unittest.TestCase):
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

    def test_get_register_customer_still_creates(self):
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.get("/registerCustomer", json=_CUSTOMER_REG)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Done")
        cust.create_customer_id.assert_called_once()

    def test_get_register_customer_self_register_still_redirects(self):
        payload = dict(_CUSTOMER_REG, empid="None")
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.get("/registerCustomer", json=payload)
        self.assertIn(response.status_code, (301, 302))
        cust.create_customer_id.assert_called_once()
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("cust1"), "cust1")

    def test_get_register_employee_still_creates_without_auth(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = 1
            response = self.client.get("/registerEmployee", json=_EMPLOYEE_REG)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], 1)
        emp.create_employee.assert_called_once()

    def test_get_login_still_sends_otp_and_writes_session(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            cust = customers_cls.return_value
            cust.retrieve_hashed_password.return_value = encrypt("pw")
            cust.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = None
            response = self.client.get(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        twilio.verify.v2.services.return_value.verifications.create.assert_called_once()
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["userid"], "cust1")
            self.assertEqual(sess["usertype"], "customer")

    def test_get_verify_otp_still_authenticates(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get("/verify-otp", json={"otp_code": "123456"})
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("/customer_dash", response.headers.get("Location", ""))
        twilio.verify.v2.services.return_value.verification_checks.create.assert_called_once()

    def test_get_logout_still_clears_session(self):
        self._session("cust1", "customer")
        response = self.client.get("/logout", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertNotIn("userid", sess)
            self.assertNotIn("usertype", sess)

    def test_get_modify_customer_still_writes_pii(self):
        self._session("emp1", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "Updated"
            response = self.client.get("/modifyCustomer", json=_MODIFY_CUSTOMER)
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once_with(
            "cust9", "L", "", "F", "1", "a@b.com", "1", "d", "x"
        )


if __name__ == "__main__":
    unittest.main()
