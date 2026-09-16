"""Authorized money-movement and admin helpers still untested on the route layer."""
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


class MoneyAdminRouteTests(unittest.TestCase):
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

    def test_withdraw_calls_debit_with_coerced_amount(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.post(
                "/withdrawAmount",
                json={"userid": "cust1", "account": 10, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, 25)
        self.assertEqual(response.get_json()["message"], "Amount Debited")

    def test_deposit_queues_employee_deposit(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = (
                "Request to be approved by tier1 employee"
            )
            response = self.client.post(
                "/depositAmount",
                json={"userid": "cust1", "account": 10, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, 40)

    def test_request_funds_inserts_pending_for_session_user(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.post(
                "/requestFunds",
                json={"userid": "cust1", "fromAccount": 10, "toAccount": 20, "amount": 15},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, 15)

    def test_cashier_cheque_issues_for_session_user(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.post(
                "/getCashierCheque",
                json={
                    "userid": "cust1",
                    "to_account": 20,
                    "from_account": 10,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "cust1", 20, 10, 50
        )

    def test_deposit_check_authorized_customer(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deposit_check.return_value = "Success"
            response = self.client.post(
                "/depositCheck",
                json={"userid": "cust1", "cheque_no": 7},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deposit_check.assert_called_once_with("cust1", 7)

    def test_load_customer_returns_dashboard_payload(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 10, "Balance": 100}
            }
            customers_cls.return_value.get_customer_details.return_value = {
                "first_name": "A"
            }
            customers_cls.return_value.get_funds_requests.return_value = "None"
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["Accounts"]["checkin"]["Account"], 10)
        self.assertEqual(body["Info"]["first_name"], "A")
        customers_cls.return_value.get_all_account.assert_called_once_with("cust1")

    def test_transaction_history_authorized_customer(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_transaction_history.return_value = [
                ("$10 transfered",)
            ]
            response = self.client.post(
                "/getTransactionHistory",
                json={"userid": "cust1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["transactions"][0][0], "$10 transfered")

    def test_update_info_places_request_for_session_user(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_info_reqest.return_value = (
                "Update Info Request Placed"
            )
            response = self.client.post(
                "/updateInfo",
                json={
                    "userid": "cust1",
                    "email": "a@b.com",
                    "contact_no": "1",
                    "address": "x",
                    "requester": "Customer",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_info_reqest.assert_called_once_with(
            "Customer", "cust1", "a@b.com", "1", "x"
        )

    def test_get_customer_allows_tier1(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {"credit": "None"}
            customers_cls.return_value.get_customer_details.return_value = {
                "first_name": "A"
            }
            response = self.client.post(
                "/getCustomer",
                json={"userid": "emp1", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["first_name"], "A")

    def test_get_employee_allows_admin(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_employee_details.return_value = {"tier": 2}
            response = self.client.post(
                "/getEmployee",
                json={"userid": "admin1", "emp_id": "emp2"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Info"]["tier"], 2)

    def test_get_employee_userid_mismatch(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post(
            "/getEmployee",
            json={"userid": "admin2", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 401)

    def test_logout_clears_userid_key(self):
        with self.client.session_transaction() as sess:
            sess["cust1"] = "cust1"
            sess["userid"] = "cust1"
        response = self.client.post("/logout", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertNotIn("cust1", sess)

    def test_deactivate_customer_authorized_calls_helper(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_customer.return_value = "Customer deactivated"
            response = self.client.post(
                "/deactivateCustomer",
                json={"userid": "emp1", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_customer.assert_called_once_with("cust1")

    def test_deactivate_employee_authorized_calls_helper(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_employee.return_value = "Employee deactivated"
            response = self.client.post(
                "/deactivateEmployee",
                json={"userid": "admin1", "emp_id": "emp2"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_employee.assert_called_once_with("emp2")

    def test_approve_update_info_authorized_calls_helper(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.post(
                "/approveUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with(9)

    def test_deny_update_info_authorized_employee(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deny_update_info.return_value = "Done"
            response = self.client.post(
                "/denyUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deny_update_info.assert_called_once_with("emp1", 9)

    def test_get_appointment_list_customer_mismatch(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/getAppointmentList", json={"customer_id": "cust2"}
        )
        self.assertEqual(response.status_code, 401)

    def test_get_appointment_list_returns_rows(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_appointment.return_value = [
                (1, "cust1", "10:00", 1)
            ]
            response = self.client.post(
                "/getAppointmentList", json={"customer_id": "cust1"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"][0][0], 1)

    def test_login_employee_stores_tier_and_sends_otp(self):
        from utility.encrypt import encrypt

        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp = emp_cls.return_value
            emp.retrieve_hashed_password.return_value = encrypt("pw")
            emp.get_employee_tier.return_value = 2
            emp.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = (
                None
            )
            response = self.client.post(
                "/login",
                json={"userid": "emp1", "password": "pw", "usertype": "tier2"},
            )
        self.assertIn(response.status_code, (301, 302))
        emp.get_employee_tier.assert_called_once_with("emp1")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["userid"], "emp1")
            self.assertEqual(sess["usertype"], "tier2")
            self.assertEqual(sess["emp_tier"], 2)

    def test_register_customer_employee_created_returns_done(self):
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.post(
                "/registerCustomer",
                json={
                    "empid": "emp1",
                    "userid": "cust1",
                    "password": "pw",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Done")
        cust.create_customer_id.assert_called_once()

    def test_register_employee_creates_when_unique(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = 1
            response = self.client.post(
                "/registerEmployee",
                json={
                    "userid": "emp1",
                    "password": "pw",
                    "email": "a@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 1,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], 1)
        emp.create_employee.assert_called_once()

    def test_open_account_customer_calls_helper(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.post(
                "/openNewAccount",
                json={
                    "userid": "cust1",
                    "customer_id": "cust1",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with(
            "cust1", "savings"
        )

    def test_modify_customer_userid_mismatch_non_admin(self):
        self._session("emp1", "tier1", emp_tier=1)
        response = self.client.post(
            "/modifyCustomer",
            json={
                "userid": "emp2",
                "customer_id": "cust1",
                "last_name": "L",
                "middle_name": "",
                "first_name": "F",
                "contact_no": "1",
                "email_id": "a@b.com",
                "ssn": "1",
                "dob": "d",
                "address": "x",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_cashier_cheque_unauthenticated_redirects(self):
        response = self.client.post(
            "/getCashierCheque",
            json={
                "userid": "cust1",
                "to_account": 20,
                "from_account": 10,
                "amount": 50,
            },
        )
        self.assertIn(response.status_code, (301, 302))

    def test_get_system_logs_sends_file_when_present(self):
        self._session("admin1", "admin")
        with patch("app.os.path.exists", return_value=True), patch(
            "app.send_from_directory"
        ) as send:
            send.return_value = "log-bytes"
            response = self.client.post(
                "/getSystemLogs", json={"userid": "admin1"}
            )
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(kwargs.get("filename") or (args[1] if len(args) > 1 else None), "bank.log")
        self.assertTrue(kwargs.get("as_attachment"))


if __name__ == "__main__":
    unittest.main()
