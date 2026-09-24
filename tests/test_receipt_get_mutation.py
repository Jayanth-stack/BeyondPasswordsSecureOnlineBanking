"""Post-revert holes: cheque receipt vs GET money mutations, customer emp-approve."""
import unittest
from unittest.mock import patch

import tests  # noqa: F401

with patch("twilio.rest.Client"):
    from app import app

from customer import Customers, cursor as cust_cursor, db as cust_db


class ChequeDepositRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _login_customer(self, userid="alice"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "customer"

    def test_deposit_check_returns_receipt_dict_as_message(self):
        self._login_customer("alice")
        receipt = {
            "transaction_no": -1,
            "from_account": 10,
            "to_account": 20,
            "amount": 75.0,
            "signature": "deadbeef",
        }
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deposit_check.return_value = receipt
            response = self.client.post(
                "/depositCheck",
                json={"userid": "alice", "cheque_no": 5},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], receipt)
        customers_cls.return_value.deposit_check.assert_called_once_with("alice", 5)

    def test_get_deposit_check_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deposit_check.return_value = "Success"
            response = self.client.get(
                "/depositCheck",
                json={"userid": "alice", "cheque_no": 5},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deposit_check.assert_called_once_with("alice", 5)


class MutatingGetRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _login_customer(self, userid="alice"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "customer"

    def test_get_deny_request_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.get(
                "/denyRequest",
                json={"userid": "alice", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(10, "alice")

    def test_get_fund_transfer_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = (
                "Request to be approved by tier1 employee"
            )
            response = self.client.get(
                "/fundTransfer",
                json={
                    "userid": "alice",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 50.0)

    def test_get_cashier_cheque_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.get(
                "/getCashierCheque",
                json={
                    "userid": "alice",
                    "to_account": 20,
                    "from_account": 10,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "alice", 20, 10, 50
        )

    def test_get_reset_password_still_mutates_without_session(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            emp_cls.return_value.reset_fpassword.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get(
                "/resetPassword",
                json={"userid": "emp1", "newPassword": "n3w", "otp": "123456"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.reset_fpassword.assert_called_once_with("emp1", "n3w")

    def test_get_withdraw_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/withdrawAmount",
                json={"userid": "alice", "account": 10, "amount": 20},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(10, 20)

    def test_customer_session_can_approve_via_emp_route(self):
        # /approveRequestEmp only matches session userid; usertype/tier unused.
        with self.client.session_transaction() as sess:
            sess["userid"] = "alice"
            sess["usertype"] = "customer"
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_employee_tier.return_value = "None"
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {
                "signature": "deadbeef"
            }
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "alice", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.owns_pending_transaction.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 44)

    def test_deactivate_employee_route_one_arg_is_500(self):
        with self.client.session_transaction() as sess:
            sess["userid"] = "admin1"
            sess["usertype"] = "admin"
        response = self.client.post(
            "/deactivateEmployee",
            json={"userid": "admin1", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["message"], "Failed to deactivate employee")
        self.assertIn("error", body)


class PendingTxnCancelGapTests(unittest.TestCase):
    def setUp(self):
        self.cursor = cust_cursor
        self.db = cust_db
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None
        self.cursor.fetchall.side_effect = None
        self.cursor.fetchone.side_effect = None

    def tearDown(self):
        self.db.commit.side_effect = None

    def test_inactive_sender_does_not_cancel_pending(self):
        customer = Customers()
        self.cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 0, "checking")],
        ]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 50.0, transaction_no=55),
                "Sender's Account not active",
            )
            cancel.assert_not_called()

    def test_inactive_receiver_does_not_cancel_pending(self):
        customer = Customers()
        self.cursor.fetchall.return_value = [(0,)]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 50.0, transaction_no=55),
                "Receiver's Account not active",
            )
            cancel.assert_not_called()

    def test_success_status_update_has_no_owner_filter(self):
        customer = Customers()
        self.cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 1, "checking")],
        ]
        result = customer.fund_transfers(10, 20, 50.0, transaction_no=77)
        self.assertIsInstance(result, dict)
        status_sql = [
            str(call.args[0])
            for call in self.cursor.execute.call_args_list
            if "Request Approved" in str(call.args[0])
        ]
        self.assertEqual(len(status_sql), 1)
        self.assertIn("transaction_no = 77", status_sql[0])
        self.assertNotIn("approver1_id", status_sql[0])
        self.assertNotIn("status = 1", status_sql[0].replace("status=0", ""))


if __name__ == "__main__":
    unittest.main()
