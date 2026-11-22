"""GET still mutates staff identity routes that #205 never locked.

Unique vs still-open coverage PRs #205/#203/#201/#199/#197/#195/#193/#191/#189:
those cover GET /registerCustomer, /registerEmployee, /login, /verify-otp,
/logout, /modifyCustomer, ACH/wire snapshot GET settle/expire/force-verify,
and money GET mutations. They do not lock GET /modifyEmployee,
/makeAppointment, /getSystemLogs, /getEmployee, or /getAppointmentList.
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
    "userid": "emp1",
    "emp_id": "emp1",
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


class StaffGetMutationTests(unittest.TestCase):
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

    def test_get_modify_employee_still_writes_pii(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "Updated"
            response = self.client.get("/modifyEmployee", json=_MODIFY_EMPLOYEE)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp1", "L", "", "F", "1", "a@b.com", "1", "d", "x", 1
        )

    def test_get_modify_employee_admin_still_writes_another(self):
        self._session("admin1", "admin", emp_tier=3)
        payload = dict(_MODIFY_EMPLOYEE, userid="admin1", emp_id="emp9", tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.update_account_info.return_value = "Updated"
            response = self.client.get("/modifyEmployee", json=payload)
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.update_account_info.assert_called_once_with(
            "emp9", "L", "", "F", "1", "a@b.com", "1", "d", "x", 2
        )

    def test_get_make_appointment_staff_still_books_for_any_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_appointment.return_value = "Appointment fixed"
            response = self.client.get(
                "/makeAppointment",
                json={"customer_id": "cust9", "time": "10:00"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_appointment.assert_called_once_with("cust9", "10:00")

    def test_get_make_appointment_customer_mismatch_is_401(self):
        self._session("cust1", "customer")
        response = self.client.get(
            "/makeAppointment",
            json={"customer_id": "cust2", "time": "10:00"},
        )
        self.assertEqual(response.status_code, 401)

    def test_get_system_logs_claimed_admin_still_sends_file(self):
        self._session("admin1", "admin")
        with patch("app.os.path.exists", return_value=True), patch(
            "app.send_from_directory"
        ) as send:
            send.return_value = "log-bytes"
            response = self.client.get("/getSystemLogs", json={"userid": "admin1"})
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(kwargs.get("filename") or (args[1] if len(args) > 1 else None), "bank.log")
        self.assertTrue(kwargs.get("as_attachment"))
        self.assertEqual(response.status_code, 200)

    def test_get_employee_claimed_staff_still_returns_coworker_pii(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {
                "emp_id": "emp9",
                "ssn": "123-45-6789",
            }
            response = self.client.get(
                "/getEmployee",
                json={"userid": "emp1", "emp_id": "emp9"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["ssn"], "123-45-6789")
        emp_cls.return_value.get_employee_details.assert_called_once_with("emp9")

    def test_get_appointment_list_staff_still_reads_another_customer(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [("cust9", "10:00")]
            response = self.client.get(
                "/getAppointmentList",
                json={"customer_id": "cust9"},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.get_appointment.assert_called_once_with("cust9")


if __name__ == "__main__":
    unittest.main()
