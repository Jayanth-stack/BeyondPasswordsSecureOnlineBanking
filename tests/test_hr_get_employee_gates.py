"""HR/admin GET identity gates that #211 never locked.

Unique vs still-open coverage PRs #211/#209/#207/#205/#203/#201/#199/#197/#195/#193/#191/#189/#174:
those lock claimed-hr GET /loadEmployee as 401, claimed-admin GET /loadEmployee
dumping SSN with no JSON body, claimed-employee GET /loadCustomer 401,
GET /updateEmployee and GET /deactivateEmployee as 405, and claimed-employee
GET /getEmployee still dumping coworker PII. They do not lock GET /getEmployee
with no JSON body (415) or empty JSON (400) — unlike GET /loadEmployee which
dumps PII without a body — claimed-hr GET /getEmployee or GET /getCustomer as
403 (hr is omitted from those allowlists), or claimed-hr POST
/deactivateEmployee as 403 (hr can POST /updateEmployee but cannot deactivate).
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


class HrGetEmployeeGateTests(unittest.TestCase):
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

    def test_get_get_employee_without_json_is_415_and_does_not_dump_pii(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/getEmployee")
        self.assertEqual(response.status_code, 415)
        emp_cls.return_value.get_employee_details.assert_not_called()

    def test_get_get_employee_empty_json_is_400_and_does_not_dump_pii(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/getEmployee", json={})
        self.assertEqual(response.status_code, 400)
        emp_cls.return_value.get_employee_details.assert_not_called()

    def test_get_get_employee_claimed_hr_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get(
                "/getEmployee", json={"userid": "hr1", "emp_id": "emp9"}
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.get_employee_details.assert_not_called()

    def test_get_get_customer_claimed_hr_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/getCustomer", json={"userid": "hr1", "customer_id": "alice"}
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.get_customer_details.assert_not_called()
        customers_cls.return_value.get_all_account.assert_not_called()

    def test_post_deactivate_employee_claimed_hr_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/deactivateEmployee", json={"userid": "hr1", "emp_id": "emp9"}
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.deactivate_employee.assert_not_called()


if __name__ == "__main__":
    unittest.main()
