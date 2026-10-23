"""Leftover claimed-admin/tier1/tier2 GET admin/PII IDOR.

Prior coverage locked claimed `employee` GET /openNewAccount,
/makeAppointment, /updateInfo, /sendOTP, and /loadEmployee.
Sibling `admin`/`tier1`/`tier2` are still just client-claimed session
usertypes. /openNewAccount allowlists claimed admin (customer_id unbound).
/makeAppointment binds customer_id only when usertype is customer.
/updateInfo matches JSON userid, not usertype. /sendOTP never reads
session. /loadEmployee allowlists claimed admin/tier1/tier2. GET is
registered and still mutates.
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


class RemainingClaimedRoleAdminPiiTests(unittest.TestCase):
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

    def test_claimed_admin_get_open_new_account_for_another_customer(self):
        # admin is in the allowed usertype list; customer_id is not bound
        # to session. GET still opens.
        self._session("cust1", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust9",
                    "account_type": "credit",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with(
            "cust9", "credit"
        )

    def test_claimed_staff_get_make_appointment_for_another_customer(self):
        # customer_id bind only applies when usertype == customer.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype, emp_tier=emp_tier)
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

    def test_claimed_staff_get_update_info_still_queues(self):
        # /updateInfo checks session userid, not usertype. GET still queues.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
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

    def test_claimed_staff_get_send_otp_for_another_user(self):
        # Route never reads session; JSON userid is enough. Default store
        # is Employee when requester is omitted. GET still sends.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype, emp_tier=emp_tier)
                with patch("app.Employee") as emp_cls, patch(
                    "app.twilio_client"
                ) as twilio:
                    emp_cls.return_value.retrieve_phone_number.return_value = (
                        "+14155552671"
                    )
                    twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                        "VA9"
                    )
                    response = self.client.get("/sendOTP", json={"userid": "alice"})
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.retrieve_phone_number.assert_called_once_with(
                    "alice"
                )

    def test_claimed_staff_get_load_employee_reads_session_pii(self):
        # loadEmployee allowlists claimed admin/tier1/tier2 and reads
        # employee PII for the session userid.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Employee") as emp_cls:
                    emp_cls.return_value.get_employee_details.return_value = {
                        "ssn": "hidden"
                    }
                    emp_cls.return_value.fund_transfer_requests.return_value = "None"
                    emp_cls.return_value.update_info_request_list.return_value = 0
                    response = self.client.get("/loadEmployee")
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.get_employee_details.assert_called_once_with(
                    "cust1"
                )
                self.assertEqual(response.get_json()["Info"]["ssn"], "hidden")


if __name__ == "__main__":
    unittest.main()
