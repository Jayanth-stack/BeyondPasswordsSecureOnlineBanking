"""Ownership-gate follow-up: NSF cancel vs owner-scoped deny, mutating GET money."""
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


class PendingTxnNsfCancelTests(unittest.TestCase):
    """Credit NSF closes the pending row; checking NSF leaves it open."""

    @classmethod
    def setUpClass(cls):
        import customer as customer_module

        importlib.reload(customer_module)
        cls.Customers = customer_module.Customers
        cls.cursor = customer_module.cursor
        cls.db = customer_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None
        self.cursor.fetchone.side_effect = None
        self.cursor.rowcount = 1
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def _regular_transfer_rows(self, sender_row):
        return [[(1,)], [(0,)], [sender_row]]

    def test_credit_nsf_cancels_pending_txn_without_owner_filter(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((0.0, 1, "credit"))

        self.assertEqual(
            customer.fund_transfers(10, 20, 5001.0, transaction_no=55),
            "Insufficient Balance in Credit Card",
        )

        sql, params = self.cursor.execute.call_args[0]
        self.assertIn("Request Denied", sql)
        self.assertIn("status=0", sql)
        self.assertIn("status = 1", sql)
        self.assertNotIn("approver1_id", sql)
        self.assertEqual(params, (55,))
        self.db.commit.assert_called()

    def test_credit_nsf_without_pending_txn_does_not_cancel(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((0.0, 1, "credit"))
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            customer.fund_transfers(10, 20, 5001.0)
            cancel.assert_not_called()

    def test_checking_nsf_leaves_pending_txn_open(self):
        # Regular-account NSF returns before _cancel_pending_transaction.
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((50.0, 1, "checkin"))
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 100.0, transaction_no=55),
                "Insufficient Balance",
            )
            cancel.assert_not_called()

        executed = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("Request Denied" in sql for sql in executed))

    def test_inactive_sender_does_not_cancel_pending_txn(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._regular_transfer_rows((1000.0, 0, "checkin"))
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 10.0, transaction_no=55),
                "Sender's Account not active",
            )
            cancel.assert_not_called()

    def test_cancel_pending_commit_failure_rolls_back(self):
        self.db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(
            self.Customers()._cancel_pending_transaction(55),
            "Please try again later",
        )
        self.db.rollback.assert_called()

    def test_deny_funds_requested_still_requires_owner(self):
        self.cursor.rowcount = 1
        result = self.Customers().deny_funds_requested(55, "alice")
        sql, params = self.cursor.execute.call_args[0]
        self.assertIn("approver1_id", sql)
        self.assertEqual(params, (55, "alice"))
        self.assertEqual(result, "Request Cancelled")


class MutatingGetAndStaffDenyRouteTests(unittest.TestCase):
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

    def test_get_deny_request_still_cancels(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.get(
                "/denyRequest",
                json={"userid": "cust1", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(9, "cust1")

    def test_get_approve_request_still_transfers_when_owned(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.get(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.owns_pending_transaction.assert_called_once_with(
            "cust1", 8
        )
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 8)

    def test_get_deposit_check_still_credits(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deposit_check.return_value = "Success"
            response = self.client.get(
                "/depositCheck",
                json={"userid": "cust1", "cheque_no": 5},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deposit_check.assert_called_once_with("cust1", 5)

    def test_staff_session_cannot_deny_pending_transfer(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/denyRequest",
                json={"userid": "emp1", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "Unauthorized access")
        customers_cls.return_value.deny_funds_requested.assert_not_called()
        emp_cls.return_value.deny_funds_requested.assert_not_called()

    def test_claimed_employee_get_update_info_still_queues(self):
        # /updateInfo checks session userid, not usertype.
        self._session("cust1", "employee", emp_tier=1)
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

    def test_claimed_employee_get_transaction_history_is_forbidden(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/getTransactionHistory",
                json={"userid": "cust1", "account_no": 555},
            )
        self.assertEqual(response.status_code, 403)
        customers_cls.return_value.get_transaction_history.assert_not_called()

    def test_claimed_employee_get_send_otp_for_another_user(self):
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "VA9"
            )
            response = self.client.get("/sendOTP", json={"userid": "alice"})
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.retrieve_phone_number.assert_called_once_with("alice")

    def test_get_deposit_amount_still_queues(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = (
                "Request to be approved by tier1 employee"
            )
            response = self.client.get(
                "/depositAmount",
                json={"userid": "cust1", "account": 10, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, 40)

    def test_get_request_funds_still_inserts(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.get(
                "/requestFunds",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(10, 20, 15)

    def test_get_cashier_cheque_still_issues(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.get(
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

    def test_claimed_employee_get_deposit_amount_still_queues(self):
        # depositAmount only matches session userid; claimed usertype is unused.
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.get(
                "/depositAmount",
                json={"userid": "cust1", "account": 999, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(999, 40)

    def test_get_register_customer_still_creates_without_session(self):
        with patch("app.Customers") as customers_cls, patch(
            "app.url_for", return_value="/customer_dash"
        ):
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.get(
                "/registerCustomer",
                json={
                    "empid": "None",
                    "userid": "cust9",
                    "password": "pw",
                    "email": "c9@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                },
            )
        self.assertIn(response.status_code, (301, 302, 200))
        cust.create_customer_id.assert_called_once()

    def test_claimed_employee_get_register_customer_still_creates(self):
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            cust = customers_cls.return_value
            cust.check_user_id.return_value = 0
            cust.check_existing_contact.return_value = 0
            cust.check_existing_email.return_value = 0
            cust.create_customer_id.return_value = 1
            response = self.client.get(
                "/registerCustomer",
                json={
                    "empid": "emp1",
                    "userid": "victim",
                    "password": "pw",
                    "email": "v@b.com",
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

    def test_get_register_employee_still_creates_admin_tier(self):
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = 1
            response = self.client.get(
                "/registerEmployee",
                json={
                    "userid": "admin9",
                    "password": "pw",
                    "email": "admin9@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 3,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(emp.create_employee.call_args.args[0], "admin9")
        self.assertEqual(emp.create_employee.call_args.args[9], 3)

    def test_claimed_employee_get_register_employee_still_creates(self):
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp = emp_cls.return_value
            emp.check_user_id.return_value = 0
            emp.check_existing_contact.return_value = 0
            emp.check_existing_email.return_value = 0
            emp.check_existing_ssn.return_value = 0
            emp.create_employee.return_value = 1
            response = self.client.get(
                "/registerEmployee",
                json={
                    "userid": "newemp",
                    "password": "pw",
                    "email": "new@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 3,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp.create_employee.assert_called_once()
        self.assertEqual(emp.create_employee.call_args.args[9], 3)

    def test_claimed_employee_post_deactivate_employee_is_forbidden(self):
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            response = self.client.post(
                "/deactivateEmployee",
                json={"userid": "mallory", "emp_id": "emp2"},
            )
        self.assertEqual(response.status_code, 403)
        emp_cls.return_value.deactivate_employee.assert_not_called()

    def test_claimed_employee_get_system_logs_redirects(self):
        self._session("mallory", "employee", emp_tier=1)
        with patch("app.send_from_directory") as send:
            response = self.client.get("/getSystemLogs", json={"userid": "mallory"})
        self.assertIn(response.status_code, (301, 302))
        send.assert_not_called()

    def test_claimed_admin_get_system_logs_still_sends_file(self):
        # getSystemLogs trusts session['usertype'] == 'admin' with no DB check.
        self._session("mallory", "admin")
        with patch("app.os.path.exists", return_value=True), patch(
            "app.send_from_directory"
        ) as send:
            send.return_value = "log-bytes"
            self.client.get("/getSystemLogs", json={"userid": "mallory"})
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(
            kwargs.get("filename") or (args[1] if len(args) > 1 else None),
            "bank.log",
        )
        self.assertTrue(kwargs.get("as_attachment"))

    def test_claimed_employee_get_cheque_list_redirects(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/getChequeList", json={"userid": "cust1"})
        self.assertIn(response.status_code, (301, 302))
        customers_cls.return_value.get_cheque_list.assert_not_called()

    def test_claimed_employee_get_load_customer_is_unauthorized(self):
        self._session("cust1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls:
            response = self.client.get("/loadCustomer")
        self.assertEqual(response.status_code, 401)
        customers_cls.return_value.get_all_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
