"""Leftover claimed-employee GET money, cheque, and password IDOR.

Prior coverage locked claimed `employee` wire/ACH create, beneficiary-list
origination, and link confirm/resend. Core ledger routes still trust a
matching JSON userid and ignore `usertype`: claimed `employee` GET can
transfer, deposit, withdraw, request funds, or issue a cashier cheque against
accounts they do not own. `/resetPassword` is still unbound to the session.
`/depositCheck` and `/approveRequest` require `usertype == customer`;
`/denyRequest` 403s staff after PR #75 — GET still reaches those gates.
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


class ClaimedEmployeeMoneyGetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid="bob", usertype="employee", emp_tier=None):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype
            if emp_tier is not None:
                sess["emp_tier"] = emp_tier

    def test_claimed_employee_get_fund_transfer_moves_arbitrary_accounts(self):
        self._session()
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.get(
                "/fundTransfer",
                json={
                    "userid": "bob",
                    "fromAccount": 1001,
                    "toAccount": 2002,
                    "amount": 50,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(1001, 2002, 50.0)

    def test_claimed_employee_get_deposit_amount_queues_arbitrary_account(self):
        self._session()
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.get(
                "/depositAmount",
                json={"userid": "bob", "account": 1001, "amount": 40},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(1001, 40)

    def test_claimed_employee_get_withdraw_debits_arbitrary_account(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/withdrawAmount",
                json={"userid": "bob", "account": 1001, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.debit_request.assert_called_once_with(1001, 25)

    def test_claimed_employee_get_request_funds_inserts_arbitrary_accounts(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.fund_request.return_value = "Request Sent"
            response = self.client.get(
                "/requestFunds",
                json={
                    "userid": "bob",
                    "fromAccount": 1001,
                    "toAccount": 2002,
                    "amount": 30,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_request.assert_called_once_with(1001, 2002, 30)

    def test_claimed_employee_get_cashier_cheque_issues_for_arbitrary_accounts(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.make_cashier_check.return_value = "Success"
            response = self.client.get(
                "/getCashierCheque",
                json={
                    "userid": "bob",
                    "to_account": 2002,
                    "from_account": 1001,
                    "amount": 15,
                },
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.make_cashier_check.assert_called_once_with(
            "bob", 2002, 1001, 15
        )

    def test_claimed_employee_get_deposit_check_redirects(self):
        # Customer-only usertype gate; claimed employee never reaches the helper.
        self._session()
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/depositCheck",
                json={"userid": "bob", "cheque_no": 5},
            )
        self.assertIn(response.status_code, (301, 302))
        customers_cls.return_value.deposit_check.assert_not_called()


class ClaimedEmployeePasswordAndApprovalGetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _session(self, userid="bob", usertype="employee"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_claimed_employee_get_reset_password_is_not_bound_to_session(self):
        self._session()
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            emp_cls.return_value.reset_fpassword.return_value = "Password Updated"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.get(
                "/resetPassword",
                json={
                    "userid": "alice",
                    "newPassword": "n3w",
                    "otp": "123456",
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.reset_fpassword.assert_called_once_with("alice", "n3w")
        emp_cls.return_value.reset_password.assert_not_called()

    def test_claimed_employee_get_approve_request_redirects(self):
        # /approveRequest requires usertype == customer; staff path is /approveRequestEmp.
        self._session()
        with patch("app.Employee") as emp_cls, patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/approveRequest",
                json={"customer_id": "alice", "transaction_no": 8},
            )
        self.assertIn(response.status_code, (301, 302))
        emp_cls.return_value.get_amount_of_transaction.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_not_called()
        customers_cls.return_value.owns_pending_transaction.assert_not_called()

    def test_claimed_employee_get_deny_request_is_403(self):
        # PR #75: matching userid is not enough; usertype must be customer.
        self._session()
        with patch("app.Customers") as customers_cls:
            response = self.client.get(
                "/denyRequest",
                json={"userid": "bob", "transaction_no": 9},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "Unauthorized access")
        customers_cls.return_value.deny_funds_requested.assert_not_called()


if __name__ == "__main__":
    unittest.main()
