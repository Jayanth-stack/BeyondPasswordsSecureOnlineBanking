"""Leftover claimed non-canonical GET core-banking money, approve, and PII IDOR.

Prior coverage locked claimed `employee`/`admin`/`tier1`/`tier2`/`hr` GET
/fundTransfer, /depositAmount, /withdrawAmount, /requestFunds,
/getCashierCheque, /resetPassword, /approveRequestEmp, /makeAppointment,
/updateInfo, and /sendOTP. Sibling login-stored usertypes such as
`manager`/`supervisor` are still just client-claimed session values
(login stores the supplied usertype; they are not allowlisted staff).
Those money routes never re-check usertype (userid match only),
/resetPassword and /sendOTP never bind to the session userid, and
/makeAppointment binds customer_id only when usertype is customer. GET is
registered and still mutates.

Claimed `manager`/`supervisor` GET /openNewAccount stays 403 (allowlist is
customer/employee/admin). Claimed `manager`/`supervisor` GET
/loadEmployee stays 401 (allowlist is admin/employee/tier1/tier2).
Claimed `manager`/`supervisor` GET /approveUpdateInfo /denyUpdateInfo
/modifyEmployee /getEmployee stay 403 (allowlist is admin/employee).
Claimed `manager`/`supervisor` GET /getCustomer stays 403 (allowlist is
tier1/tier2/employee). They are not in wire/ACH EMPLOYEE_ROLES. Do not
lock those as mutating.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

CLAIMED_NONCANONICAL = ("manager", "supervisor")


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class RemainingClaimedNoncanonicalCoreMoneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid, usertype="manager", emp_tier=3):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            sess["emp_tier"] = emp_tier

    def test_claimed_noncanonical_get_fund_transfer_queues_arbitrary_accounts(self):
        # fundTransfer matches JSON userid to session; claimed usertype and
        # account ownership are unused. GET still queues.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
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
                emp_cls.return_value.add_transaction.assert_called_once_with(
                    111, 222, 50.0
                )

    def test_claimed_noncanonical_get_deposit_amount_queues_arbitrary_account(self):
        # depositAmount only matches session userid; claimed usertype is unused.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Employee") as emp_cls:
                    emp_cls.return_value.add_transaction_deposit.return_value = (
                        "queued"
                    )
                    response = self.client.get(
                        "/depositAmount",
                        json={"userid": "cust1", "account": 999, "amount": 40},
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.add_transaction_deposit.assert_called_once_with(
                    999, 40
                )

    def test_claimed_noncanonical_get_withdraw_debits_arbitrary_account(self):
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.debit_request.return_value = (
                        "Amount Debited"
                    )
                    response = self.client.get(
                        "/withdrawAmount",
                        json={"userid": "cust1", "account": 888, "amount": 25},
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.debit_request.assert_called_once_with(
                    888, 25
                )

    def test_claimed_noncanonical_get_request_funds_inserts_on_arbitrary_accounts(
        self,
    ):
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.fund_request.return_value = (
                        "Request Sent"
                    )
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
                customers_cls.return_value.fund_request.assert_called_once_with(
                    111, 222, 15
                )

    def test_claimed_noncanonical_get_cashier_cheque_issues_from_arbitrary_account(
        self,
    ):
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.make_cashier_check.return_value = (
                        "Success"
                    )
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

    def test_claimed_noncanonical_get_reset_password_unbound_to_session(self):
        # Route never reads session; OTP + JSON userid is enough. Default store
        # is Employee when requester is omitted.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype)
                with patch("app.Employee") as emp_cls, patch(
                    "app.twilio_client"
                ) as twilio:
                    emp_cls.return_value.retrieve_phone_number.return_value = (
                        "+14155552671"
                    )
                    emp_cls.return_value.reset_fpassword.return_value = (
                        "Password Updated"
                    )
                    twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                        "approved"
                    )
                    response = self.client.get(
                        "/resetPassword",
                        json={
                            "userid": "victim",
                            "newPassword": "n3w",
                            "otp": "123456",
                        },
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.reset_fpassword.assert_called_once_with(
                    "victim", "n3w"
                )

    def test_claimed_noncanonical_get_approve_request_emp_moves_money(self):
        # GET still mutates. Gate is session userid match only — no
        # usertype, ownership, or fetched-tier check before fund_transfers.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Employee") as emp_cls, patch(
                    "app.Customers"
                ) as customers_cls:
                    emp = emp_cls.return_value
                    emp.get_amount_of_transaction.return_value = 50
                    emp.get_employee_tier.return_value = "None"
                    emp.get_fromAccount_of_transaction.return_value = 10
                    emp.get_toAccount_of_transaction.return_value = 20
                    emp.get_transaction_status.return_value = 1
                    customers_cls.return_value.fund_transfers.return_value = {
                        "amount": 50
                    }
                    response = self.client.get(
                        "/approveRequestEmp",
                        json={"userid": "cust1", "transaction_no": 8},
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.fund_transfers.assert_called_once_with(
                    10, 20, 50, 8
                )
                emp.get_employee_tier.assert_called_once_with("cust1")

    def test_claimed_noncanonical_get_make_appointment_for_another_customer(self):
        # customer_id bind only applies when usertype == customer.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.make_appointment.return_value = (
                        "Appointment fixed"
                    )
                    response = self.client.get(
                        "/makeAppointment",
                        json={"customer_id": "cust9", "time": "11:00"},
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.make_appointment.assert_called_once_with(
                    "cust9", "11:00"
                )

    def test_claimed_noncanonical_get_update_info_still_queues(self):
        # /updateInfo checks session userid, not usertype. GET still queues.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.update_info_reqest.return_value = (
                        "Update Info Request Placed"
                    )
                    response = self.client.get(
                        "/updateInfo",
                        json={
                            "userid": "cust1",
                            "email": "evil@x.com",
                            "contact_no": "1",
                            "address": "x",
                            "requester": "Employee",
                        },
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.update_info_reqest.assert_called_once_with(
                    "Employee", "cust1", "evil@x.com", "1", "x"
                )

    def test_claimed_noncanonical_get_send_otp_for_another_user(self):
        # Route never reads session; JSON userid is enough. Default store
        # is Employee when requester is omitted. GET still sends.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype)
                with patch("app.Employee") as emp_cls, patch(
                    "app.twilio_client"
                ) as twilio:
                    emp_cls.return_value.retrieve_phone_number.return_value = (
                        "+14155552671"
                    )
                    twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                        "VA9"
                    )
                    response = self.client.get(
                        "/sendOTP", json={"userid": "alice"}
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.retrieve_phone_number.assert_called_once_with(
                    "alice"
                )


if __name__ == "__main__":
    unittest.main()
