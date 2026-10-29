"""Leftover claimed-Customer reset/OTP/PII/approve and CUSTOMER/Employee
GET money/appointment IDOR.

Prior coverage locked claimed `Customer` GET /fundTransfer,
/depositAmount, /withdrawAmount, /requestFunds, /getCashierCheque,
/makeAppointment, and /getAppointmentList. Sibling GET /resetPassword,
/sendOTP, /updateInfo, and /approveRequestEmp were covered for
employee/admin/tier1/tier2/hr/manager/supervisor but not for injected
`Customer`. /resetPassword and /sendOTP never bind to the session
userid, /updateInfo checks session userid not usertype, and
/approveRequestEmp is userid match only (fetched tier unused). GET is
registered and still mutates.

Prior coverage also locked claimed `Customer` money/appointment GET.
Sibling all-caps `CUSTOMER` and capitalized `Employee` are not the
exact strings `customer` or `employee`, so /makeAppointment and
/getAppointmentList skip the customer_id bind. Money routes never
re-check usertype (userid match only). GET still mutates.

Claimed `CUSTOMER`/`Employee` GET /openNewAccount stays 403 (allowlist
is customer/employee/admin). Claimed `CUSTOMER`/`Employee` GET
/loadCustomer stays 401 and GET /depositCheck /getChequeList
/approveRequest redirect (exact usertype == customer). Claimed
`CUSTOMER`/`Employee` GET /loadEmployee stays 401 (allowlist is
admin/employee/tier1/tier2). Claimed `CUSTOMER`/`Employee` GET
/approveUpdateInfo /denyUpdateInfo /modifyEmployee /getEmployee stay
403 or redirect (allowlist is admin/employee). Claimed `CUSTOMER`/
`Employee` GET /getCustomer stays 403 (allowlist is
tier1/tier2/employee). Claimed `CUSTOMER`/`Employee` GET
/modifyCustomer stays 403 (allowlist is admin/employee/tier1/tier2).
Do not lock those as mutating. They are not in wire/ACH EMPLOYEE_ROLES.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

CLAIMED_CAPITALIZED_CUSTOMER = ("Customer",)
CLAIMED_CASE_VARIANTS = ("CUSTOMER", "Employee")


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class RemainingClaimedCaseVariantIdorTests(unittest.TestCase):
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

    def test_claimed_capitalized_customer_get_reset_password_unbound_to_session(
        self,
    ):
        # Route never reads session; OTP + JSON userid is enough. Default
        # store is Employee when requester is omitted.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_send_otp_for_another_user(self):
        # Route never reads session; JSON userid is enough. Default store
        # is Employee when requester is omitted. GET still sends.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_update_info_still_queues(self):
        # /updateInfo checks session userid, not usertype. GET still queues.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_approve_request_emp_moves_money(
        self,
    ):
        # GET still mutates. Gate is session userid match only — no
        # usertype, ownership, or fetched-tier check before fund_transfers.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_case_variant_get_fund_transfer_queues_arbitrary_accounts(
        self,
    ):
        # fundTransfer matches JSON userid to session; claimed usertype and
        # account ownership are unused. GET still queues.
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_deposit_amount_queues_arbitrary_account(
        self,
    ):
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_withdraw_debits_arbitrary_account(self):
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_request_funds_inserts_on_arbitrary_accounts(
        self,
    ):
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_cashier_cheque_issues_from_arbitrary_account(
        self,
    ):
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_make_appointment_for_another_customer(
        self,
    ):
        # customer_id bind only applies when usertype == customer. All-caps
        # CUSTOMER and capitalized Employee skip it and book for another
        # customer_id. GET still writes.
        for usertype in CLAIMED_CASE_VARIANTS:
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

    def test_claimed_case_variant_get_appointment_list_for_another_customer(
        self,
    ):
        # Same exact-string bind as makeAppointment. GET still reads.
        for usertype in CLAIMED_CASE_VARIANTS:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.get_appointment.return_value = [
                        (1, "cust9", "10:00", 1)
                    ]
                    response = self.client.get(
                        "/getAppointmentList",
                        json={"customer_id": "cust9"},
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.get_appointment.assert_called_once_with(
                    "cust9"
                )
                self.assertEqual(response.get_json()["message"][0][1], "cust9")


if __name__ == "__main__":
    unittest.main()
