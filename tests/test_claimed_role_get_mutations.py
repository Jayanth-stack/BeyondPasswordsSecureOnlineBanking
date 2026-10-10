"""GET mutations and claimed-role IDOR still untested after the NSF/register suite.

Login persists client-supplied usertype. Several money/admin routes never re-check
it (or only match JSON userid), and most of them are registered for GET as well.
This file also covers leftover approve/request-funds/modify-employee/deny-PII GET
holes and claimed-employee updateEmployee/getEmployee IDOR.
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


_MODIFY_CUSTOMER = {
    "customer_id": "cust9",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "999887777",
    "dob": "d",
    "address": "x",
}

_MODIFY_EMPLOYEE = {
    "userid": "emp1",
    "emp_id": "emp9",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "999-00-0000",
    "dob": "d",
    "address": "x",
    "tier": 3,
}


class ClaimedRoleGetMutationTests(unittest.TestCase):
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

    def test_get_open_new_account_still_opens(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust9",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("cust9", "savings")

    def test_claimed_employee_get_open_new_account_for_another_customer(self):
        # employee is in the allowed usertype list; customer_id is not bound to session.
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust9",
                    "account_type": "credit",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("cust9", "credit")

    def test_get_make_appointment_still_books(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.get(
                "/makeAppointment",
                json={"customer_id": "cust1", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with("cust1", "10:00")

    def test_claimed_employee_get_make_appointment_for_another_customer(self):
        # customer_id bind only applies when usertype == customer.
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.get(
                "/makeAppointment",
                json={"customer_id": "cust9", "time": "11:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with("cust9", "11:00")

    def test_claimed_employee_get_modify_customer_rewrites_other_pii(self):
        # Matching JSON userid skips the other-user deny; customer_id is not the session user.
        self._session("emp1", "employee", emp_tier=1)
        payload = dict(_MODIFY_CUSTOMER, userid="emp1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyCustomer", json=payload)
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once_with(
            "cust9", "L", "", "F", "1", "a@b.com", "999887777", "d", "x"
        )

    def test_claimed_admin_get_modify_customer_skips_userid_match(self):
        self._session("mallory", "admin", emp_tier=3)
        payload = dict(_MODIFY_CUSTOMER, userid="someone-else")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyCustomer", json=payload)
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once()

    def test_claimed_employee_get_load_employee_reads_session_pii(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {"ssn": "hidden"}
            emp_cls.return_value.fund_transfer_requests.return_value = "None"
            emp_cls.return_value.update_info_request_list.return_value = 0
            response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.get_employee_details.assert_called_once_with("cust1")
        self.assertEqual(response.get_json()["Info"]["ssn"], "hidden")

    def test_claimed_employee_get_withdraw_debits_arbitrary_account(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 888, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(888, 25)

    def test_claimed_employee_get_fund_transfer_queues_arbitrary_accounts(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.get(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 111,
                    "toAccount": 222,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(111, 222, 50.0)

    def test_claimed_employee_get_reset_password_unbound_to_session(self):
        # Route never reads session; OTP + JSON userid is enough. Default store is Employee.
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            emp_cls.return_value.reset_fpassword.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get(
                "/resetPassword",
                json={"userid": "victim", "newPassword": "n3w", "otp": "123456"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.reset_fpassword.assert_called_once_with("victim", "n3w")

    def test_claimed_admin_post_deactivate_customer_is_unauthorized(self):
        # Gate is usertype == tier2, not admin.
        self._session("mallory", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/deactivateCustomer",
                json={"userid": "someone-else", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 401)
        emp_cls.return_value.deactivate_customer.assert_not_called()

    def test_claimed_tier2_get_deactivate_customer_ignores_json_userid(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_customer.return_value = "Customer deactivated"
            response = self.client.get(
                "/deactivateCustomer",
                json={"userid": "emp9", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_customer.assert_called_once_with("emp9", "cust1")

    def test_claimed_tier2_get_deactivate_account_closes_arbitrary_account(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_account.return_value = "Account Closed"
            response = self.client.get(
                "/deactivateAccount",
                json={"userid": "emp1", "account_no": 999},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_account.assert_called_once_with("emp1", 999)

    def test_claimed_employee_get_cashier_cheque_issues_from_arbitrary_account(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.get(
                "/getCashierCheque",
                json={
                    "userid": "cust1",
                    "to_account": 20,
                    "from_account": 888,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "cust1", 20, 888, 50
        )

    def test_claimed_employee_get_approve_update_info_writes_pii(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.get(
                "/approveUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with("emp1", 9)

    def test_claimed_employee_get_approve_request_emp_moves_money(self):
        # GET still mutates. Gate is session userid match only — no usertype,
        # ownership, or fetched-tier check before fund_transfers.
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_employee_tier.return_value = "None"
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.get(
                "/approveRequestEmp",
                json={"userid": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 8)
        emp.get_employee_tier.assert_called_once_with("cust1")

    def test_claimed_employee_get_request_funds_inserts_on_arbitrary_accounts(self):
        # requestFunds matches JSON userid to session; claimed usertype and
        # account ownership are unused. GET still inserts.
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.get(
                "/requestFunds",
                json={
                    "userid": "cust1",
                    "fromAccount": 111,
                    "toAccount": 222,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(111, 222, 15)

    def test_claimed_employee_get_modify_employee_rewrites_other_emp(self):
        # Matching JSON userid skips the other-employee deny (emp_tier < 3).
        # emp_id is not bound to the session user. GET still writes.
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp9", "L", "", "F", "1", "a@b.com", "999-00-0000", "d", "x", 3
        )

    def test_claimed_employee_get_deny_update_info_skips_userid_match(self):
        # denyUpdateInfo checks claimed usertype/emp_tier only; JSON userid is
        # forwarded to the helper with no session bind. GET still mutates.
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Done"
            response = self.client.get(
                "/denyUpdateInfo",
                json={"userid": "emp9", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("emp9", 9)

    def test_claimed_employee_update_employee_matching_userid_rewrites_other_emp(self):
        # POST-only route: matching JSON userid skips the admin/hr deny even
        # when emp_id is someone else. Kwargs still do not match the helper.
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post(
                "/updateEmployee",
                json={
                    "userid": "emp1",
                    "emp_id": "emp9",
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
        emp_cls.return_value.update_employee.assert_called_once_with(
            emp_id="emp9",
            email="a@b.com",
            firstname="A",
            midname="",
            lastname="B",
            phone="1",
            dob="d",
            ssn="1",
            address="x",
        )

    def test_claimed_employee_get_employee_reads_other_emp_pii(self):
        # getEmployee binds JSON userid to session, not emp_id.
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {"ssn": "hidden"}
            response = self.client.get(
                "/getEmployee",
                json={"userid": "emp1", "emp_id": "emp9"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.get_employee_details.assert_called_once_with("emp9")
        self.assertEqual(response.get_json()["Info"]["ssn"], "hidden")

    def test_get_login_still_writes_session(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt(
                "pw"
            )
            customers_cls.return_value.retrieve_phone_number.return_value = (
                "+14155552671"
            )
            twilio.verify.v2.services.return_value.verifications.create.return_value = (
                object()
            )
            response = self.client.get(
                "/login",
                json={"userid": "cust1", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("userid"), "cust1")
            self.assertEqual(sess.get("usertype"), "customer")

    def test_get_logout_still_clears_session(self):
        self._session("cust1", "customer")
        response = self.client.get("/logout", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertNotIn("userid", sess)
            self.assertNotIn("usertype", sess)


if __name__ == "__main__":
    unittest.main()
