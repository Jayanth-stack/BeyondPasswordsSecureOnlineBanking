"""Leftover claimed-admin/tier1/tier2 staff-approve and PII IDOR.

Prior coverage locked claimed `employee` GET /approveRequestEmp,
/approveUpdateInfo, /modifyEmployee, /denyUpdateInfo, POST
/updateEmployee, and GET /getEmployee. Sibling `admin`/`tier1`/`tier2`
are still just client-claimed session usertypes.

/approveRequestEmp never re-checks usertype (userid match only) and GET
still moves money. /approveUpdateInfo and /denyUpdateInfo allowlist
claimed admin (plus emp_tier>=2). /modifyEmployee and /getEmployee
allowlist claimed admin. /updateEmployee allowlists claimed admin for
any emp_id, and matching JSON userid lets claimed tier1/tier2 rewrite
another emp_id.
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


CLAIMED_ROLES = (("admin", 3), ("tier1", 1), ("tier2", 2))

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


class RemainingClaimedRoleStaffApprovePiiTests(unittest.TestCase):
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

    def test_claimed_staff_get_approve_request_emp_moves_money(self):
        # GET still mutates. Gate is session userid match only — no
        # usertype, ownership, or fetched-tier check before fund_transfers.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
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

    def test_claimed_admin_get_approve_update_info_writes_pii(self):
        # allowlist is ['admin', 'employee'] and emp_tier>=2. GET still writes.
        self._session("emp1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.get(
                "/approveUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with("emp1", 9)

    def test_claimed_admin_get_modify_employee_rewrites_other_emp(self):
        # Matching JSON userid skips the other-employee deny (emp_tier < 3).
        # emp_id is not bound to the session user. GET still writes.
        self._session("emp1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp9", "L", "", "F", "1", "a@b.com", "999-00-0000", "d", "x", 3
        )

    def test_claimed_admin_get_deny_update_info_skips_userid_match(self):
        # denyUpdateInfo checks claimed usertype/emp_tier only; JSON userid
        # is forwarded to the helper with no session bind. GET still mutates.
        self._session("emp1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Done"
            response = self.client.get(
                "/denyUpdateInfo",
                json={"userid": "emp9", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("emp9", 9)

    def test_claimed_admin_update_employee_rewrites_other_emp(self):
        # POST-only route: claimed admin skips the admin/hr deny even when
        # JSON userid and emp_id are someone else.
        self._session("mallory", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_employee.return_value = "Employee Updated"
            body = dict(_UPDATE_EMPLOYEE, userid="emp1")
            response = self.client.post("/updateEmployee", json=body)
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

    def test_claimed_staff_update_employee_matching_userid_rewrites_other_emp(self):
        # Matching JSON userid skips the admin/hr deny even when emp_id is
        # someone else. Kwargs still do not match the helper.
        for usertype, emp_tier in (("tier1", 1), ("tier2", 2)):
            with self.subTest(usertype=usertype):
                self._session("emp1", usertype, emp_tier=emp_tier)
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

    def test_claimed_admin_get_employee_reads_other_emp_pii(self):
        # getEmployee binds JSON userid to session, not emp_id. Allowlist
        # includes claimed admin.
        self._session("emp1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {"ssn": "hidden"}
            response = self.client.get(
                "/getEmployee",
                json={"userid": "emp1", "emp_id": "emp9"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.get_employee_details.assert_called_once_with("emp9")
        self.assertEqual(response.get_json()["Info"]["ssn"], "hidden")


if __name__ == "__main__":
    unittest.main()
