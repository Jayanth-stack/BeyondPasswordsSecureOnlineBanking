"""GET still dumps remaining identity reads that #207 never locked.

Unique vs still-open coverage PRs #207/#205/#203/#201/#199/#197/#195/#193/#191/#189:
those cover GET /registerCustomer, /registerEmployee, /login, /verify-otp,
/logout, /modifyCustomer, /modifyEmployee, /makeAppointment, /getSystemLogs,
/getEmployee, /getAppointmentList, and money GET mutations. They do not lock
GET /loadEmployee (no JSON body), /getChequeList, or /getTransactionHistory.
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


class IdentityGetReadTests(unittest.TestCase):
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

    def test_get_load_employee_without_body_still_dumps_session_pii(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {
                "emp_id": "emp1",
                "ssn": "123-45-6789",
            }
            emp_cls.return_value.fund_transfer_requests.return_value = "None"
            emp_cls.return_value.update_info_request_list.return_value = 0
            response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["ssn"], "123-45-6789")
        emp_cls.return_value.get_employee_details.assert_called_once_with("emp1")

    def test_get_load_employee_customer_is_401(self):
        self._session("cust1", "customer")
        response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 401)

    def test_get_cheque_list_still_returns_customer_cheques(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_cheque_list.return_value = [
                (1, 20, 10, 50.0, 1)
            ]
            response = self.client.get("/getChequeList", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"][0][0], 1)
        customers_cls.return_value.get_cheque_list.assert_called_once_with("cust1")

    def test_get_cheque_list_claimed_employee_still_redirects(self):
        self._session("emp1", "employee", emp_tier=1)
        response = self.client.get("/getChequeList", json={"userid": "emp1"})
        self.assertIn(response.status_code, (301, 302))

    def test_get_transaction_history_still_returns_and_skips_account_owner(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_transaction_history.return_value = [
                ("$10 transfered",)
            ]
            response = self.client.get(
                "/getTransactionHistory",
                json={"userid": "cust1", "account_no": 9999},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["transactions"][0][0], "$10 transfered")
        customers_cls.return_value.get_transaction_history.assert_called_once_with(9999)

    def test_get_transaction_history_claimed_staff_is_403(self):
        self._session("emp1", "employee", emp_tier=1)
        response = self.client.get(
            "/getTransactionHistory",
            json={"userid": "emp1", "account_no": 10},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
