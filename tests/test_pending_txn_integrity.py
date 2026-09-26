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

    def test_inactive_accounts_do_not_cancel_pending(self):
        customer = Customers()
        self.cust_cursor.fetchall.return_value = [(0,)]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 10.0, transaction_no=55),
                "Receiver's Account not active",
            )
            cancel.assert_not_called()

        self.cust_cursor.reset_mock()
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 0, "savings")],
        ]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 10.0, transaction_no=55),
                "Sender's Account not active",
            )
            cancel.assert_not_called()

    def test_success_status_update_has_no_owner_filter(self):
        customer = Customers()
        self.cust_cursor.fetchall.side_effect = [
            [(1,)],
            [(0,)],
            [(1000.0, 1, "checkin")],
        ]
        result = customer.fund_transfers(10, 20, 10.0, transaction_no=77)
        self.assertIsInstance(result, dict)
        status_sql = [
            str(call.args[0])
            for call in self.cust_cursor.execute.call_args_list
            if "UPDATE Transactions SET status=0" in str(call.args[0])
        ]
        self.assertTrue(status_sql)
        self.assertTrue(any("77" in sql for sql in status_sql))
        self.assertFalse(any("approver1_id" in sql for sql in status_sql))

    def test_employee_deny_has_no_owner_or_status_filter(self):
        emp = Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            emp.deny_funds_requested("emp1", 88)
        sql = self.emp_cursor.execute.call_args.args[0]
        self.assertIn("transaction_no = 88", sql)
        self.assertNotIn("approver1_id", sql)
        where = sql.split("WHERE", 1)[-1]
        self.assertNotIn("status", where)

    def test_deny_funds_requested_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            Customers().deny_funds_requested("not-a-number", "alice")
        self.cust_cursor.execute.assert_not_called()

    def test_deny_funds_requested_none_txn_typeerrors(self):
        with self.assertRaises(TypeError):
            Customers().deny_funds_requested(None, "alice")
        self.cust_cursor.execute.assert_not_called()

    def test_owns_pending_none_txn_typeerrors(self):
        with self.assertRaises(TypeError):
            Customers().owns_pending_transaction("alice", None)
        self.cust_cursor.execute.assert_not_called()

    def test_owns_pending_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            Customers().owns_pending_transaction("alice", "abc")
        self.cust_cursor.execute.assert_not_called()


class MutatingGetApproveDenyTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _login(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_get_deny_request_still_mutates(self):
        self._login()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = "Request Cancelled"
            response = self.client.get(
                "/denyRequest",
                json={"userid": "alice", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deny_funds_requested.assert_called_once_with(10, "alice")

    def test_get_approve_request_still_mutates(self):
        self._login()
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.get(
                "/approveRequest",
                json={"customer_id": "alice", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once()

    def test_get_approve_request_emp_still_mutates(self):
        self._login("teller", "tier2")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_employee_tier.return_value = 2
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.get(
                "/approveRequestEmp",
                json={"userid": "teller", "transaction_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 10)
        customers_cls.return_value.owns_pending_transaction.assert_not_called()

    def test_get_deposit_check_still_mutates(self):
        self._login()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.deposit_check.return_value = "Success"
            response = self.client.get(
                "/depositCheck",
                json={"userid": "alice", "cheque_no": 5},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.deposit_check.assert_called_once_with("alice", 5)

    def test_approve_request_emp_int_coerces_txn_and_crashes(self):
        self._login("teller", "tier2")
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            emp = emp_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 10
            emp.get_toAccount_of_transaction.return_value = 20
            emp.get_transaction_status.return_value = 1
            with self.assertRaises(ValueError):
                self.client.post(
                    "/approveRequestEmp",
                    json={"userid": "teller", "transaction_no": "not-a-number"},
                )
            customers_cls.return_value.fund_transfers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
