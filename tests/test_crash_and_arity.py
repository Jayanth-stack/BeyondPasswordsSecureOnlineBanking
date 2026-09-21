"""Remaining high-risk contracts: transfer IndexErrors, helper arity, and unauth money/OTP."""
import importlib
import inspect
import unittest
from unittest.mock import patch

import tests  # noqa: F401


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


class FundTransferCrashTests(unittest.TestCase):
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
        self.cursor.fetchall.return_value = []
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def tearDown(self):
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def test_missing_deposit_row_indexerrors(self):
        # Direct transfers default transaction_no=-1 and still query Transactions.
        # An empty result is truthy-as-a-list-check is skipped; result[0] raises.
        self.cursor.fetchall.side_effect = [[(1,)], []]
        with self.assertRaises(IndexError):
            self.Customers().fund_transfers(10, 20, 10.0)

    def test_missing_receiver_indexerrors_instead_of_not_exists(self):
        # `if result is not None` is true for [] so the "doesn't exist" branch is dead.
        self.cursor.fetchall.return_value = []
        with self.assertRaises(IndexError):
            self.Customers().fund_transfers(10, 20, 10.0)

    def test_missing_sender_indexerrors_instead_of_not_exists(self):
        self.cursor.fetchall.side_effect = [[(1,)], [(0,)], []]
        with self.assertRaises(IndexError):
            self.Customers().fund_transfers(10, 20, 10.0)

    def test_success_receipt_includes_nonce_and_timestamp(self):
        self.cursor.fetchall.side_effect = [[(1,)], [(0,)], [(1000.0, 1, "checkin")]]
        receipt = self.Customers().fund_transfers(10, 20, 40.0)
        self.assertIsInstance(receipt, dict)
        self.assertIn("nonce", receipt)
        self.assertTrue(receipt["nonce"])
        self.assertIn("timestamp", receipt)
        self.assertIn("signature", receipt)

    def test_valid_cheque_crashes_when_deposit_flag_missing(self):
        # deposit_check calls fund_transfers without a transaction_no.
        self.cursor.fetchall.side_effect = [
            [(20, 10, 75.0, 1)],
            [(1,)],
            [],
        ]
        with self.assertRaises(IndexError):
            self.Customers().deposit_check("cust1", 5)

    def test_debit_missing_account_indexerrors(self):
        self.cursor.fetchall.return_value = []
        with self.assertRaises(IndexError):
            self.Customers().debit_request(10, 20.0)

    def test_credit_missing_account_indexerrors(self):
        self.cursor.fetchall.return_value = []
        with self.assertRaises(IndexError):
            self.Customers().credit_request(10, 20.0)

    def test_contact_lookup_missing_customer_indexerrors(self):
        self.cursor.fetchall.return_value = []
        with self.assertRaises(IndexError):
            self.Customers().get_customer_contactNo("ghost")

    def test_fund_request_missing_account_still_inserts(self):
        customer = self.Customers()
        with patch.object(customer, "get_customerID_from_account", return_value=-1):
            self.assertEqual(customer.fund_request(10, 20, 15), "Request Sent")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("'-1'", sql)

    def test_get_customer_id_from_missing_account_returns_minus_one(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Customers().get_customerID_from_account(99), -1)

    def test_handle_appointment_commit_failure_rolls_back(self):
        self.db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Customers().handle_appointment(3), "Try again later")
        self.db.rollback.assert_called()

    def test_update_login_history_commit_failure_rolls_back(self):
        self.db.commit.side_effect = RuntimeError("disk full")
        self.Customers().update_login_history("cust1")
        self.db.rollback.assert_called()

    def test_open_account_eleven_arg_form_is_shadowed(self):
        params = list(inspect.signature(self.Customers.open_account).parameters)
        self.assertEqual(params[1:], ["customer_id", "account_type"])


class EmployeeContractGapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        importlib.reload(employee_module)
        cls.employee_module = employee_module
        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.cursor.fetchall.side_effect = None
        self.cursor.fetchone.side_effect = None
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = None
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def tearDown(self):
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def test_deactivate_account_route_arity_typeerrors(self):
        with self.assertRaises(TypeError):
            self.Employee().deactivate_account(10)

    def test_deactivate_customer_route_arity_typeerrors(self):
        with self.assertRaises(TypeError):
            self.Employee().deactivate_customer("cust1")

    def test_deactivate_employee_route_arity_typeerrors(self):
        with self.assertRaises(TypeError):
            self.Employee().deactivate_employee("emp2")

    def test_approve_update_info_route_arity_typeerrors(self):
        with self.assertRaises(TypeError):
            self.Employee().approve_update_info(9)

    def test_reset_password_route_arity_typeerrors(self):
        with self.assertRaises(TypeError):
            self.Employee().reset_password("emp1", "n3w")

    def test_update_employee_route_kwargs_typeerror(self):
        with self.assertRaises(TypeError):
            self.Employee().update_employee(
                emp_id="emp2",
                email="a@b.com",
                firstname="A",
                midname="",
                lastname="B",
                phone="1",
                dob="d",
                ssn="1",
                address="x",
            )

    def test_verify_employee_interpolates_fresh_bcrypt_not_stored_hash(self):
        from utility.encrypt import encrypt

        stored = encrypt("secret")
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.Employee().verify_employee("emp1", "secret"), 0)
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn(stored, sql)
        self.assertIn("$2", sql)

    def test_check_user_id_does_not_require_active(self):
        self.cursor.fetchall.return_value = [("emp1",)]
        self.assertEqual(self.Employee().check_user_id("emp1"), 1)
        sql = self.cursor.execute.call_args.args[0]
        self.assertNotIn("active", sql)

    def test_check_existing_ssn_looks_up_plaintext(self):
        from utility.encrypt import encrypt_ssn

        hashed = encrypt_ssn("123456789")
        self.Employee().check_existing_ssn("123456789")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("ssn='123456789'", sql)
        self.assertNotIn(hashed, sql)

    def test_retrieve_hashed_password_missing_is_none(self):
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.Employee().retrieve_hashed_password("ghost"))

    def test_approve_fund_request_uses_module_singleton(self):
        instance = self.Employee()
        with patch.object(
            self.employee_module.emp, "get_fromAccount_of_transaction", return_value=10
        ), patch.object(
            self.employee_module.emp, "get_toAccount_of_transaction", return_value=20
        ), patch.object(
            self.employee_module.emp, "get_amount_of_transaction", return_value=50
        ), patch.object(
            self.employee_module.emp, "get_transaction_status", return_value=1
        ), patch.object(
            instance, "get_fromAccount_of_transaction"
        ) as self_from, patch(
            "employee.Customers"
        ) as customers_cls:
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            result = instance.approve_fund_request(5)
        self_from.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 5)
        self.assertEqual(result["amount"], 50)

    def test_fund_transfer_requests_commit_failure_returns_none(self):
        emp = self.Employee()
        self.db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertIsNone(emp.fund_transfer_requests("emp1"))
        self.db.rollback.assert_called()

    def test_deny_update_info_commit_failure_rolls_back(self):
        self.db.commit.side_effect = RuntimeError("disk full")
        self.assertEqual(self.Employee().deny_update_info("emp1", 9), "Try again later")
        self.db.rollback.assert_called()

    def test_update_employee_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "get_employee_tier", return_value=3):
            self.assertEqual(
                emp.update_employee(
                    "admin1", "emp2", "a@b.com", "A", "", "B", "1", "dob", "addr"
                ),
                "Cannot update Employee",
            )
        self.db.rollback.assert_called()

    def test_reset_fpassword_commit_failure_rolls_back(self):
        emp = self.Employee()
        self.db.commit.side_effect = RuntimeError("disk full")
        with patch.object(emp, "check_user_id", return_value=1):
            self.assertEqual(emp.reset_fpassword("emp1", "n3w"), "Try Again Later")
        self.db.rollback.assert_called()


class CrashAndArityRouteTests(unittest.TestCase):
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

    def test_deactivate_account_authorized_matches_helper_arity(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_account.return_value = "Account Closed"
            response = self.client.post(
                "/deactivateAccount",
                json={"userid": "emp1", "account_no": 10},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_account.assert_called_once_with("emp1", 10)

    def test_deactivate_customer_authorized_matches_helper_arity(self):
        self._session("emp1", "tier2", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.deactivate_customer.return_value = "Customer deactivated"
            response = self.client.post(
                "/deactivateCustomer",
                json={"userid": "emp1", "customer_id": "cust1"},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.deactivate_customer.assert_called_once_with("emp1", "cust1")

    def test_deactivate_employee_authorized_is_500_wrong_arity(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post(
            "/deactivateEmployee",
            json={"userid": "admin1", "emp_id": "emp2"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("Failed to deactivate employee", response.get_json()["message"])

    def test_approve_update_info_authorized_matches_helper_arity(self):
        self._session("emp1", "employee", emp_tier=2)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.approve_update_info.return_value = "Customer Updated"
            response = self.client.post(
                "/approveUpdateInfo",
                json={"userid": "emp1", "update_req_no": 9},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.approve_update_info.assert_called_once_with("emp1", 9)

    def test_update_employee_authorized_is_500_bad_kwargs(self):
        self._session("admin1", "admin", emp_tier=3)
        response = self.client.post(
            "/updateEmployee",
            json={
                "userid": "admin1",
                "emp_id": "emp2",
                "email": "a@b.com",
                "firstname": "A",
                "midname": "",
                "lastname": "B",
                "phone": "1",
                "dob": "d",
                "ssn": "1",
                "address": "x",
            },
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("Failed to update employee", response.get_json()["message"])

    def test_reset_password_approved_otp_calls_force_reset(self):
        with patch.object(
            self.app_module.Employee, "retrieve_phone_number", return_value="+14155552671"
        ), patch.object(
            self.app_module.Employee, "reset_fpassword", return_value="Password Updated"
        ) as reset_fpassword, patch("app.twilio_client") as twilio:
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            response = self.client.post(
                "/resetPassword",
                json={"userid": "emp1", "newPassword": "n3w", "otp": "123456"},
            )
        self.assertEqual(response.status_code, 200)
        reset_fpassword.assert_called_once_with("emp1", "n3w")

    def test_send_otp_unauthenticated_still_sends(self):
        with patch("app.Employee") as emp_cls, patch("app.twilio_client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verifications.create.return_value.sid = (
                "VA123"
            )
            response = self.client.post("/sendOTP", json={"userid": "emp1"})
        self.assertEqual(response.status_code, 200)
        twilio.verify.v2.services.return_value.verifications.create.assert_called_once()

    def test_fund_transfer_zero_amount_is_queued(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = (
                "Request to be approved by tier1 employee"
            )
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "cust1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 0,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 0.0)

    def test_fund_transfer_does_not_require_customer_usertype(self):
        self._session("emp1", "tier1", emp_tier=1)
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction.return_value = "queued"
            response = self.client.post(
                "/fundTransfer",
                json={
                    "userid": "emp1",
                    "fromAccount": 10,
                    "toAccount": 20,
                    "amount": 5,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction.assert_called_once_with(10, 20, 5.0)

    def test_verify_otp_unknown_tier_dashboard_raises(self):
        from werkzeug.routing.exceptions import BuildError

        self._session("emp1", "employee", emp_tier=3)
        with patch("app.Employee") as emp_cls, patch("app.client") as twilio:
            emp_cls.return_value.retrieve_phone_number.return_value = "+14155552671"
            twilio.verify.v2.services.return_value.verification_checks.create.return_value.status = (
                "approved"
            )
            with self.assertRaises(BuildError):
                self.client.post("/verify-otp", json={"otp_code": "123456"})

    def test_get_system_logs_authorized_is_500_flask3_path_kwarg(self):
        # Route still passes filename=; Flask 3 send_from_directory requires path=.
        self._session("admin1", "admin")
        response = self.client.post("/getSystemLogs", json={"userid": "admin1"})
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["message"], "Failed to retrieve system logs")
        self.assertIn("path", body["error"])

    def test_register_employee_unauthenticated_creates(self):
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
                    "userid": "emp9",
                    "password": "pw",
                    "email": "e@b.com",
                    "firstname": "A",
                    "midname": "",
                    "lastname": "B",
                    "phone": "4155552671",
                    "dob": "2000-01-01",
                    "ssn": "123456789",
                    "address": "x",
                    "tier": 2,
                },
            )
        self.assertEqual(response.status_code, 200)
        emp.create_employee.assert_called_once()

    def test_withdraw_missing_fields(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/withdrawAmount", json={"userid": "cust1", "account": 10}
        )
        self.assertEqual(response.status_code, 400)

    def test_deposit_missing_fields(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/depositAmount", json={"userid": "cust1", "account": 10}
        )
        self.assertEqual(response.status_code, 400)

    def test_open_account_missing_fields(self):
        self._session("cust1", "customer")
        response = self.client.post("/openNewAccount", json={"userid": "cust1"})
        self.assertEqual(response.status_code, 400)

    def test_cashier_cheque_missing_fields(self):
        self._session("cust1", "customer")
        response = self.client.post(
            "/getCashierCheque", json={"userid": "cust1", "to_account": 20}
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
