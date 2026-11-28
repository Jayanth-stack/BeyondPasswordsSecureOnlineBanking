"""claimed-tier2 cannot open accounts; admin deactivateCustomer is 401.

Unique vs still-open PRs #216/#214/#213/#211: those locked claimed-tier1
GET/POST /openNewAccount 403, claimed-hr GET/POST /openNewAccount 403,
claimed-admin GET/POST /deactivateAccount 403, claimed-hr POST
/deactivateEmployee 403, claimed-admin GET/POST /getCustomer 403, and
claimed-hr GET /getEmployee 403. They never locked that tier2 is also
omitted from openNewAccount (unlike employee), that admin deactivateCustomer
is 401 (usertype folded into the session check, unlike deactivateAccount
403), that authenticated GET /deactivateCustomer with no JSON is 401 not
415, or that claimed-tier1 GET/POST /getEmployee is 403 (tier1 can
getCustomer).
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


_OPEN = {
    "userid": "emp1",
    "customer_id": "cust1",
    "account_type": "savings",
}


class Tier2OpenAdminDeactivateGateTests(unittest.TestCase):
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

    def test_claimed_tier2_get_open_account_is_403(self):
        # tier2 is omitted from ['customer', 'employee', 'admin']; unlike employee.
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/openNewAccount", json=_OPEN)
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_tier2_post_open_account_is_403(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Customers") as customers_cls:
            response = self.client.post("/openNewAccount", json=_OPEN)
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.open_account.assert_not_called()

    def test_claimed_admin_get_deactivate_customer_is_401(self):
        # Only usertype=='tier2'; admin is 401 (session check), unlike
        # /deactivateAccount which 403s after get_json (#216).
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get(
                "/deactivateCustomer",
                json={"userid": "admin1", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 401)
        emp_cls.return_value.deactivate_customer.assert_not_called()

    def test_claimed_admin_post_deactivate_customer_is_401(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/deactivateCustomer",
                json={"userid": "admin1", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 401)
        emp_cls.return_value.deactivate_customer.assert_not_called()

    def test_get_deactivate_customer_authenticated_no_json_is_401(self):
        # Session+usertype fail first; unlike authenticated GET /getCustomer
        # 415 (#216) or GET /getEmployee 415 before session (#213).
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            response = self.client.get("/deactivateCustomer")
        self.assertEqual(response.status_code, 401)
        emp_cls.return_value.deactivate_customer.assert_not_called()

    def test_claimed_tier1_get_get_employee_is_403(self):
        # tier1 is omitted from ['admin', 'employee']; unlike GET /getCustomer.
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.get(
                "/getEmployee", json={"userid": "emp1", "emp_id": "emp2"}
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.get_employee_details.assert_not_called()

    def test_claimed_tier1_post_get_employee_is_403(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/getEmployee", json={"userid": "emp1", "emp_id": "emp2"}
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.get_employee_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()
