"""Customer lookup/history/appointment helpers used by dashboards and money queues."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401


class CustomerLookupTests(unittest.TestCase):
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
        self.cursor.fetchall.return_value = []

    def test_credit_overdraft_on_pending_txn_denies_request(self):
        customer = self.Customers()
        # receiver active, deposit=0, sender credit over limit
        self.cursor.fetchall.side_effect = [[(1,)], [(0,)], [(0.0, 1, "credit")]]
        with patch.object(customer, "_cancel_pending_transaction") as cancel, patch.object(
            customer, "deny_funds_requested"
        ) as deny:
            self.assertEqual(
                customer.fund_transfers(10, 20, 5001.0, transaction_no=55),
                "Insufficient Balance in Credit Card",
            )
            cancel.assert_called_once_with(55)
            deny.assert_not_called()

    def test_credit_overdraft_without_pending_txn_does_not_deny(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = [[(1,)], [(0,)], [(0.0, 1, "credit")]]
        with patch.object(customer, "_cancel_pending_transaction") as cancel, patch.object(
            customer, "deny_funds_requested"
        ) as deny:
            customer.fund_transfers(10, 20, 5001.0)
            cancel.assert_not_called()
            deny.assert_not_called()

    def test_make_appointment_inserts_open_row(self):
        self.assertEqual(
            self.Customers().make_appointment("cust1", "10:00"),
            "Appointment fixed",
        )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("Appointments", sql)
        self.assertIn("cust1", sql)
        self.assertIn("10:00", sql)
        self.db.commit.assert_called()

    def test_handle_appointment_marks_done(self):
        self.assertEqual(self.Customers().handle_appointment(3), "Appointment done")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("status = 0", sql)
        self.assertIn("3", sql)

    def test_get_appointment_empty(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_appointment("cust1"), "None")

    def test_get_appointment_returns_open_rows(self):
        self.cursor.fetchall.return_value = [(1, "cust1", "10:00", 1)]
        self.assertEqual(self.Customers().get_appointment("cust1")[0][0], 1)

    def test_get_funds_requests_empty(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_funds_requests("cust1"), "None")

    def test_get_funds_requests_filters_open_for_approver(self):
        self.cursor.fetchall.return_value = [(9, 10, 20)]
        result = self.Customers().get_funds_requests("cust1")
        self.assertEqual(result, [(9, 10, 20)])
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("approver1_id='cust1'", sql)
        self.assertIn("status=1", sql)

    def test_get_cheque_list_empty(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_cheque_list("cust1"), "None")

    def test_get_cheque_list_returns_rows(self):
        rows = [(5, 20, 10, 40.0, 1)]
        self.cursor.fetchall.return_value = rows
        self.assertEqual(self.Customers().get_cheque_list("cust1"), rows)

    def test_get_transaction_history_returns_rows(self):
        self.cursor.fetchall.return_value = [("$10 transfered",)]
        self.assertEqual(
            self.Customers().get_transaction_history(10),
            [("$10 transfered",)],
        )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("transaction_history", sql)
        self.assertIn("10", sql)

    def test_update_login_history_concatenates(self):
        self.Customers().update_login_history("cust1")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("login_history=concat", sql)
        self.assertIn("cust1", sql)
        self.db.commit.assert_called()

    def test_check_user_id_requires_active(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().check_user_id("cust1"), 0)
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("active=1", sql)

    def test_retrieve_hashed_password_is_parameterized(self):
        self.cursor.fetchone.return_value = ("$2b$hash",)
        hashed = self.Customers().retrieve_hashed_password("cust1")
        self.assertEqual(hashed, "$2b$hash")
        args, kwargs = self.cursor.execute.call_args
        self.assertEqual(args[1], ("cust1",))
        self.assertNotIn("'cust1'", args[0])

    def test_update_account_info_writes_provided_ssn(self):
        self.assertEqual(
            self.Customers().update_account_info(
                "cust1", "L", "", "F", "1", "a@b.com", "123456789", "dob", "addr"
            ),
            "updated",
        )
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("123456789", sql)
        self.assertIn("cust1", sql)


if __name__ == "__main__":
    unittest.main()
