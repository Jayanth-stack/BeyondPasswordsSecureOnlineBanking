"""claimed-hr is omitted from modifyCustomer/openNewAccount/modifyEmployee allowlists.

Unique vs still-open PRs #213/#211/#209/#207/#205: those locked claimed-hr GET
/getEmployee /getCustomer 403, GET /loadEmployee 401, POST /deactivateEmployee
403, GET /modifyEmployee writes for employee/admin, GET /modifyCustomer writes
for claimed-employee, and POST /updateEmployee which *does* allow hr.
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
    "userid": "hr1",
    "customer_id": "cust1",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "1",
    "dob": "d",
    "address": "x",
}

_MODIFY_EMPLOYEE = {
    "userid": "hr1",
    "emp_id": "emp2",
    "last_name": "L",
    "middle_name": "",
    "first_name": "F",
    "contact_no": "1",
    "email_id": "a@b.com",
    "ssn": "1",
    "dob": "d",
    "address": "x",
    "tier": 1,
}


class HrModifyOpenGateTests(unittest.TestCase):
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

    def test_get_customer_unauthenticated_no_json_redirects(self):
        # Session is checked before get_json, unlike GET /getEmployee which 415s first.
        response = self.client.get("/getCustomer")
        self.assertIn(response.status_code, (301, 302))

    def test_claimed_hr_get_modify_customer_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/modifyCustomer", json=_MODIFY_CUSTOMER)
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.update_account_info.assert_not_called()

    def test_claimed_hr_post_modify_customer_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.post("/modifyCustomer", json=_MODIFY_CUSTOMER)
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.update_account_info.assert_not_called()

    def test_claimed_hr_get_open_account_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "hr1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_hr_post_open_account_is_403(self):
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "hr1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_hr_get_modify_employee_redirects(self):
        # hr is omitted from ['admin', 'employee']; session is checked before get_json.
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertIn(response.status_code, (301, 302))
        emp_cls.return_value.update_account_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
