"""Leftover claimed-manager/supervisor POST /updateEmployee and capitalized
Customer GET money/appointment IDOR.

Prior coverage locked claimed `employee`/`admin`/`tier1`/`tier2`/`hr`
POST /updateEmployee (admin/hr skip the deny for any emp_id; matching
JSON userid lets employee/tier1/tier2 rewrite another emp_id). Sibling
login-stored `manager`/`supervisor` are still just client-claimed
session values. Matching JSON userid skips the admin/hr deny even when
emp_id is someone else.

Prior coverage also locked claimed `employee`/`admin`/`tier1`/`tier2`/
`hr`/`manager`/`supervisor` GET /fundTransfer, /depositAmount,
/withdrawAmount, /requestFunds, /getCashierCheque, and /makeAppointment.
Sibling injected `Customer` (login stores the supplied usertype; live
login AttributeErrors on get_employee_tier after Customers auth) is not
the exact string `customer`, so /makeAppointment and /getAppointmentList
skip the customer_id bind. Money routes never re-check usertype (userid
match only). GET is registered and still mutates.

Claimed `Customer` GET /openNewAccount stays 403 (allowlist is
customer/employee/admin). Claimed `Customer` GET /loadCustomer stays 401
and GET /depositCheck /getChequeList /approveRequest redirect (exact
usertype == customer). Claimed `Customer` GET /loadEmployee stays 401
(allowlist is admin/employee/tier1/tier2). Claimed `Customer` GET
/approveUpdateInfo /denyUpdateInfo /modifyEmployee /getEmployee stay 403
or redirect (allowlist is admin/employee). Claimed `Customer` GET
/getCustomer stays 403 (allowlist is tier1/tier2/employee). Claimed
`Customer` GET /modifyCustomer stays 403 (allowlist is
admin/employee/tier1/tier2). Do not lock those as mutating.
"""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

CLAIMED_NONCANONICAL = ("manager", "supervisor")
CLAIMED_CAPITALIZED_CUSTOMER = ("Customer",)

_UPDATE_EMPLOYEE = {
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
}


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class RemainingClaimedCapitalizedCustomerTests(unittest.TestCase):
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

    def test_claimed_noncanonical_update_employee_matching_userid_rewrites_other_emp(
        self,
    ):
        # Matching JSON userid skips the admin/hr deny even when emp_id is
        # someone else. Kwargs still do not match the helper.
        for usertype in CLAIMED_NONCANONICAL:
            with self.subTest(usertype=usertype):
                self._session("emp1", usertype, emp_tier=3)
                with patch("app.Employee") as emp_cls:
                    emp_cls.return_value.update_employee.return_value = (
                        "Employee Updated"
                    )
                    response = self.client.post(
                        "/updateEmployee", json=_UPDATE_EMPLOYEE
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

    def test_customer_session_update_employee_matching_userid_rewrites_other_emp(self):
        # Real customer login path: usertype is customer, JSON userid matches
        # the session, emp_id is someone else. Gate never checks usertype.
        self._session("emp1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            response = self.client.post("/updateEmployee", json=_UPDATE_EMPLOYEE)
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

    def test_claimed_capitalized_customer_get_fund_transfer_queues_arbitrary_accounts(
        self,
    ):
        # fundTransfer matches JSON userid to session; claimed usertype and
        # account ownership are unused. GET still queues.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_deposit_amount_queues_arbitrary_account(
        self,
    ):
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_withdraw_debits_arbitrary_account(self):
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_request_funds_inserts_on_arbitrary_accounts(
        self,
    ):
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_cashier_cheque_issues_from_arbitrary_account(
        self,
    ):
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_make_appointment_for_another_customer(
        self,
    ):
        # customer_id bind only applies when usertype == customer. Capitalized
        # Customer skips it and books for another customer_id. GET still writes.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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

    def test_claimed_capitalized_customer_get_appointment_list_for_another_customer(
        self,
    ):
        # Same exact-string bind as makeAppointment. GET still reads.
        for usertype in CLAIMED_CAPITALIZED_CUSTOMER:
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
