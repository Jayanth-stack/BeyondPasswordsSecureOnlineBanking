"""Leftover mutating-GET money routes, staff-gate crashes, and replay holes.

Prior coverage locked fundTransfer/withdraw/resetPassword/approve/deny/depositCheck
GETs and the live cheque receipt. These paths still mutate or crash the same way.
"""
import importlib
import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401

from employee import Employee, cursor as emp_cursor


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class RemainingMutatingGetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid="alice", usertype="customer", emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_get_deposit_amount_still_queues(self):
        self._session()
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.get(
                "/depositAmount",
                json={"userid": "alice", "account": 10, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, 40)

    def test_get_request_funds_still_inserts(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.get(
                "/requestFunds",
                json={
                    "userid": "alice",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 30,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, 30)

    def test_get_cashier_cheque_still_issues(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.get(
                "/getCashierCheque",
                json={
                    "userid": "alice",
                    "to_account": 20,
                    "from_account": 10,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "alice", 20, 10, 15
        )

    def test_get_open_account_still_creates(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.open_account.return_value = "Done"
            response = self.client.get(
                "/openNewAccount",
                json={
                    "userid": "alice",
                    "customer_id": "alice",
                    "account_type": "savings",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.open_account.assert_called_once_with("alice", "savings")

    def test_get_modify_customer_still_writes_pii(self):
        self._session("admin1", "admin", emp_tier=3)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_account_info.return_value = "updated"
            response = self.client.get(
                "/modifyCustomer",
                json={
                    "userid": "admin1",
                    "customer_id": "cust1",
                    "last_name": "L",
                    "middle_name": "",
                    "first_name": "F",
                    "contact_no": "1",
                    "email_id": "a@b.com",
                    "ssn": "123456789",
                    "dob": "d",
                    "address": "x",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_account_info.assert_called_once()

    def test_get_update_info_still_queues_pii(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.update_info_reqest.return_value = (
                "Update Info Request Placed"
            )
            response = self.client.get(
                "/updateInfo",
                json={
                    "userid": "alice",
                    "email": "a@b.com",
                    "contact_no": "1",
                    "address": "x",
                    "requester": "alice",
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.update_info_reqest.assert_called_once()

    def test_get_deactivate_customer_still_deletes(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_customer.return_value = "Customer deactivated"
            response = self.client.get(
                "/deactivateCustomer",
                json={"userid": "emp1", "customer_id": "cust9"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_customer.assert_called_once_with("emp1", "cust9")

    def test_get_login_still_authenticates(self):
        from utility.encrypt import encrypt

        with patch("app.Customers") as customers_cls, patch("app.client") as twilio:
            customers_cls.return_value.retrieve_hashed_password.return_value = encrypt("pw")
            customers_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value = (
                MagicMock()
            )
            response = self.client.get(
                "/login",
                json={"userid": "alice", "password": "pw", "usertype": "customer"},
            )
        self.assertIn(response.status_code, (301, 302))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("userid"), "alice")
            self.assertEqual(sess.get("usertype"), "customer")

    def test_get_send_otp_still_sends(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "VA1"
            )
            response = self.client.get("/sendOTP", json={"userid": "emp1"})
        self.assertEqual(response.status_code, 200)
        twilio.verify.v2.services.return_value.verifications.create.assert_called_once()


class RemainingApprovalCrashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_approve_request_emp_none_string_amount_crashes_instead_of_404(self):
        # Helper missing-txn sentinel is -1; the route 404s only on None.
        # A leftover 'None' string is truthy and is forwarded to fund_transfers.
        with self.client.session_transaction() as sess:
            sess["userid"] = "teller"
            sess["usertype"] = "tier2"
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = "None"
            emp.get_employee_tier.return_value = 2
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.side_effect = ValueError(
                "could not convert string to float: 'None'"
            )
            with self.assertRaises(ValueError):
                self.client.post(
                    "/approveRequestEmp",
                    json={"userid": "teller", "transaction_no": 44},
                )
            customers_cls.return_value.fund_transfers.assert_called_once_with(
                10, 20, "None", 44
            )

    def test_employee_deny_non_numeric_raises(self):
        emp_cursor.reset_mock()
        emp = Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            with self.assertRaises(ValueError):
                emp.deny_funds_requested("emp1", "not-a-number")
        emp_cursor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
