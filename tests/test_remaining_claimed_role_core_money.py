"""Leftover claimed-admin/tier1/tier2 GET core-banking money/password IDOR.

Prior coverage locked claimed `employee` GET /fundTransfer, /depositAmount,
/withdrawAmount, /requestFunds, /getCashierCheque, and /resetPassword.
Sibling `admin`/`tier1`/`tier2` are still just client-claimed session
usertypes: those money routes never re-check usertype (userid match only),
and /resetPassword never binds to the session userid at all. GET is
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


class RemainingClaimedRoleCoreMoneyTests(unittest.TestCase):
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

    def test_claimed_staff_get_fund_transfer_queues_arbitrary_accounts(self):
        # fundTransfer matches JSON userid to session; claimed usertype and
        # account ownership are unused. GET still queues.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Employee") as emp_cls:
                    emp_cls.return_value.add_transaction.return_value = "queued"
                    response = self.client.get(
                        "/fundTransfer",
                        json={
                            "userid": "cust1",
                            "fromAccount": 111,
                            "toAccount": 222,
                            "amount": 50,
                        },
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.add_transaction.assert_called_once_with(
                    111, 222, 50.0
                )

    def test_claimed_staff_get_deposit_amount_queues_arbitrary_account(self):
        # depositAmount only matches session userid; claimed usertype is unused.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Employee") as emp_cls:
                    emp_cls.return_value.add_transaction_deposit.return_value = "queued"
                    response = self.client.get(
                        "/depositAmount",
                        json={"userid": "cust1", "account": 999, "amount": 40},
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.add_transaction_deposit.assert_called_once_with(
                    999, 40
                )

    def test_claimed_staff_get_withdraw_debits_arbitrary_account(self):
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.debit_request.return_value = (
                        "Amount Debited"
                    )
                    response = self.client.get(
                        "/withdrawAmount",
                        json={"userid": "cust1", "account": 888, "amount": 25},
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.debit_request.assert_called_once_with(
                    888, 25
                )

    def test_claimed_staff_get_request_funds_inserts_on_arbitrary_accounts(self):
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.fund_request.return_value = (
                        "Request Sent"
                    )
                    response = self.client.get(
                        "/requestFunds",
                        json={
                            "userid": "cust1",
                            "fromAccount": 111,
                            "toAccount": 222,
                            "amount": 15,
                        },
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.fund_request.assert_called_once_with(
                    111, 222, 15
                )

    def test_claimed_staff_get_cashier_cheque_issues_from_arbitrary_account(self):
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("cust1", usertype, emp_tier=emp_tier)
                with patch("app.Customers") as customers_cls:
                    customers_cls.return_value.make_cashier_check.return_value = (
                        "Success"
                    )
                    response = self.client.get(
                        "/getCashierCheque",
                        json={
                            "userid": "cust1",
                            "to_account": 20,
                            "from_account": 888,
                            "amount": 50,
                        },
                    )
                self.assertEqual(response.status_code, 200)
                customers_cls.return_value.make_cashier_check.assert_called_once_with(
                    "cust1", 20, 888, 50
                )

    def test_claimed_staff_get_reset_password_unbound_to_session(self):
        # Route never reads session; OTP + JSON userid is enough. Default store
        # is Employee when requester is omitted.
        for usertype, emp_tier in CLAIMED_ROLES:
            with self.subTest(usertype=usertype):
                self._session("mallory", usertype, emp_tier=emp_tier)
                with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
                    emp_cls.return_value.retrieve_phone_number.return_value = (
                        "+14155552671"
                    )
                    emp_cls.return_value.reset_fpassword.return_value = (
                        "Password Updated"
                    )
                    twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                        "approved"
                    )
                    response = self.client.get(
                        "/resetPassword",
                        json={
                            "userid": "victim",
                            "newPassword": "n3w",
                            "otp": "123456",
                        },
                    )
                self.assertEqual(response.status_code, 200)
                emp_cls.return_value.reset_fpassword.assert_called_once_with(
                    "victim", "n3w"
                )


if __name__ == "__main__":
    unittest.main()
