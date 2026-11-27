"""claimed-admin/tier1 are omitted from getCustomer/openNewAccount/deactivateAccount.

Unique vs still-open PRs #214/#213/#211/#209: those locked claimed-hr GET
/getCustomer 403, claimed-hr GET/POST /openNewAccount 403, claimed-hr GET
/modifyEmployee redirect, claimed-admin GET /loadEmployee dumping SSN, and
GET /getEmployee with no JSON as 415 (get_json before session). They never
locked that admin is omitted from getCustomer (unlike employee), that tier1
is omitted from openNewAccount (unlike employee), that admin cannot
deactivateAccount (only tier2), that claimed-hr POST /modifyEmployee also
redirects, or that authenticated GET /getCustomer with no JSON is 415 after
the session check.
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


class AdminGetCustomerGateTests(unittest.TestCase):
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

    def test_claimed_admin_get_get_customer_is_403(self):
        # admin is omitted from ['tier1', 'tier2', 'employee']; unlike GET /loadEmployee.
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/getCustomer", json={"userid": "admin1", "customer_id": "alice"}
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.get_customer_details.assert_not_called()
        customers_cls.return_value.get_all_account.assert_not_called()

    def test_claimed_admin_post_get_customer_is_403(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            response = self.client.post(
                "/getCustomer", json={"userid": "admin1", "customer_id": "alice"}
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.get_customer_details.assert_not_called()

    def test_get_get_customer_authenticated_no_json_is_415(self):
        # Session passes first; then get_json with no body is 415. Unauthenticated
        # GET redirects (#214); GET /getEmployee 415s before session (#213).
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/getCustomer")
        self.assertEqual(response.status_code, 415)
        customers_cls.return_value.get_customer_details.assert_not_called()

    def test_claimed_tier1_get_open_account_is_403(self):
        # tier1 is omitted from ['customer', 'employee', 'admin'].
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "emp1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_tier1_post_open_account_is_403(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "emp1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_admin_get_deactivate_account_is_403(self):
        # Only usertype=='tier2' may deactivate; admin is omitted.
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get(
                "/deactivateAccount",
                json={"userid": "admin1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.deactivate_account.assert_not_called()

    def test_claimed_admin_post_deactivate_account_is_403(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/deactivateAccount",
                json={"userid": "admin1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.deactivate_account.assert_not_called()

    def test_claimed_hr_post_modify_employee_redirects(self):
        # hr is omitted from ['admin', 'employee']; session is checked before get_json.
        # #214 locked GET only.
        self._session("hr1", "hr", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.post("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertIn(response.status_code, (301, 302))
        emp_cls.return_value.update_account_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
