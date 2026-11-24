"""HR/admin GET identity gates that #209 never locked.

Unique vs still-open coverage PRs #209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189:
those lock GET /loadEmployee for an employee session (no JSON body) and
customer 401, GET /getChequeList, GET /getTransactionHistory, GET
/modifyEmployee, and money GET mutations. They do not lock claimed-hr
exclusion from /loadEmployee, claimed-admin GET /loadEmployee dumping SSN,
claimed-employee GET /loadCustomer, or POST-only /updateEmployee and
/deactivateEmployee staying 405 (unlike GET /modifyEmployee which still writes).
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


_UPDATE_EMPLOYEE = {
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
}


class HrAdminGetGateTests(unittest.TestCase):
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

    def test_get_load_employee_claimed_hr_is_401(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 401)
        emp_cls.return_value.get_employee_details.assert_not_called()

    def test_get_load_employee_claimed_admin_still_dumps_session_pii(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {
                "emp_id": "admin1",
                "ssn": "123-45-6789",
            }
            emp_cls.return_value.fund_transfer_requests.return_value = "None"
            emp_cls.return_value.update_info_request_list.return_value = 0
            response = self.client.get("/loadEmployee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["ssn"], "123-45-6789")
        emp_cls.return_value.get_employee_details.assert_called_once_with("admin1")

    def test_get_load_customer_claimed_employee_is_401(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/loadCustomer")
        self.assertEqual(response.status_code, 401)
        customers_cls.return_value.get_all_account.assert_not_called()

    def test_get_update_employee_is_405_and_does_not_write(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/updateEmployee", json=_UPDATE_EMPLOYEE)
        self.assertEqual(response.status_code, 405)
        emp_cls.return_value.update_employee.assert_not_called()

    def test_get_deactivate_employee_is_405_and_does_not_write(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get(
                "/deactivateEmployee", json={"userid": "admin1", "emp_id": "emp9"}
            )
        self.assertEqual(response.status_code, 405)
        emp_cls.return_value.deactivate_employee.assert_not_called()


if __name__ == "__main__":
    unittest.main()
