"""PR #75 leftover holes: NSF cancel asymmetry, mutating GET approve, raw txn coercion."""
import unittest
from unittest.mock import patch

import tests  # noqa: F401

with patch("twilio.rest.Client"):
    from app import app

from customer import Customers, cursor as cust_cursor, db as cust_db
from employee import Employee, cursor as emp_cursor, db as emp_db


class PendingTxnHelperTests(unittest.TestCase):
    def setUp(self):
        self.cust_cursor = cust_cursor
        self.cust_db = cust_db
        self.emp_cursor = emp_cursor
        self.emp_db = emp_db
        for cur, db in ((cust_cursor, cust_db), (emp_cursor, emp_db)):
            cur.reset_mock()
            db.reset_mock()
            db.commit.side_effect = None
            db.rollback.side_effect = None
            cur.fetchall.side_effect = None
            cur.fetchone.side_effect = None
            cur.rowcount = 0

    def tearDown(self):
        self.cust_db.commit.side_effect = None
        self.emp_db.commit.side_effect = None

    def test_debit_nsf_on_pending_txn_does_not_cancel(self):
        customer = Customers()
        # receiver active, deposit=0, checking balance below amount
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(10.0, 1, "checking")],
        ]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 50.0, transaction_no=55),
                "Insufficient Balance",
            )
            cancel.assert_not_called()

    def test_credit_nsf_cancel_sql_has_no_owner_filter(self):
        customer = Customers()
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(0.0, 1, "credit")],
        ]
        result = customer.fund_transfers(10, 20, 5001.0, transaction_no=55)

        self.assertEqual(result, "Insufficient Balance in Credit Card")
        sql, params = self.cust_cursor.execute.call_args[0]
        self.assertIn("status = 1", sql)
        self.assertNotIn("approver1_id", sql)
        self.assertEqual(params, (55,))
        self.cust_db.commit.assert_called()

    def test_deny_funds_requested_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            Customers().deny_funds_requested("not-a-number", "alice")
        self.cust_cursor.execute.assert_not_called()

    def test_deny_funds_requested_none_txn_typeerrors(self):
        with self.assertRaises(TypeError):
            Customers().deny_funds_requested(None, "alice")
        self.cust_cursor.execute.assert_not_called()

    def test_cancel_pending_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            Customers()._cancel_pending_transaction("not-a-number")
        self.cust_cursor.execute.assert_not_called()

    def test_owns_pending_none_txn_typeerrors(self):
        with self.assertRaises(TypeError):
            Customers().owns_pending_transaction("alice", None)
        self.cust_cursor.execute.assert_not_called()

    def test_employee_deny_has_no_owner_or_status_filter(self):
        emp = Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(emp.deny_funds_requested("emp1", 9), "Request Cancelled")
        sql = self.emp_cursor.execute.call_args.args[0]
        compact = sql.replace(" ", "").replace("\n", "")
        self.assertIn("Request Denied by Bank", sql)
        self.assertIn("transaction_no=9", compact)
        self.assertNotIn("approver1_id", sql)
        self.assertNotIn("status=1", compact)


class PendingTxnRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _login_customer(self, userid="alice"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "customer"

    def _login_tier2(self, userid="emp1"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = "tier2"
            sess["emp_tier"] = 2

    def _open_transfer_emp(self, emp):
        emp.get_amount_of_transaction.return_value = 50
        emp.get_employee_tier.return_value = 2
        emp.get_fromAccount_of_transaction.return_value = 10
        emp.get_toAccount_of_transaction.return_value = 20
        emp.get_transaction_status.return_value = 1

    def test_get_approve_request_still_mutates(self):
        self._login_customer("alice")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = "done"
            response = self.client.get(
                "/approveRequest",
                json={"customer_id": "alice", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 10)

    def test_get_approve_request_emp_still_mutates(self):
        self._login_tier2("emp1")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            self._open_transfer_emp(emp_cls.return_value)
            customers_cls.return_value.fund_transfers.return_value = "done"
            response = self.client.get(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 44)
        customers_cls.return_value.owns_pending_transaction.assert_not_called()

    def test_approve_request_emp_skips_ownership_check(self):
        self._login_tier2("emp1")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            self._open_transfer_emp(emp_cls.return_value)
            customers_cls.return_value.fund_transfers.return_value = "done"
            response = self.client.post(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 44},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.owns_pending_transaction.assert_not_called()
        emp_cls.return_value.get_employee_tier.assert_called()

    def test_approve_request_none_string_amount_is_wrong_txn(self):
        self._login_customer("alice")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp_cls.return_value.get_amount_of_transaction.return_value = "None"
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "alice", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "Wrong Transaction number")
        customers_cls.return_value.fund_transfers.assert_not_called()
        emp_cls.return_value.transfer_transaction_to_tier2.assert_not_called()

    def test_deny_request_forwards_non_numeric_txn(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.side_effect = ValueError(
                "invalid literal"
            )
            with self.assertRaises(ValueError):
                self.client.post(
                    "/denyRequest",
                    json={"userid": "alice", "transaction_no": "abc"},
                )
            customers_cls.return_value.deny_funds_requested.assert_called_once_with(
                "abc", "alice"
            )

    def test_approve_request_forwards_non_numeric_txn(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.owns_pending_transaction.side_effect = ValueError(
                "invalid literal"
            )
            with self.assertRaises(ValueError):
                self.client.post(
                    "/approveRequest",
                    json={"customer_id": "alice", "transaction_no": "abc"},
                )
            customers_cls.return_value.owns_pending_transaction.assert_called_once_with(
                "alice", "abc"
            )

    def test_unauthorized_approve_logs_txn_access(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls, patch("app.logging.warning") as warn:
            customers_cls.return_value.owns_pending_transaction.return_value = False
            response = self.client.post(
                "/approveRequest",
                json={"customer_id": "alice", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 403)
        warn.assert_called()
        self.assertIn("Unauthorized transaction access", warn.call_args[0][0])
        self.assertEqual(warn.call_args[0][1], "alice")
        self.assertEqual(warn.call_args[0][2], 10)

    def test_foreign_deny_logs_rejected_result(self):
        self._login_customer("alice")
        with patch("app.Customers") as customers_cls, patch("app.logging.warning") as warn:
            customers_cls.return_value.deny_funds_requested.return_value = (
                "Unauthorized or invalid transaction"
            )
            response = self.client.post(
                "/denyRequest",
                json={"userid": "alice", "transaction_no": 99},
            )
        self.assertEqual(response.status_code, 403)
        warn.assert_called()
        self.assertIn("DenyRequest rejected", warn.call_args[0][0])
        self.assertEqual(warn.call_args[0][1], "alice")
        self.assertEqual(warn.call_args[0][2], 99)


if __name__ == "__main__":
    unittest.main()
