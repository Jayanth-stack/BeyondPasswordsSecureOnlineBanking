"""High-risk money, NSF, cheque-retry, and Fedwire integrity gaps.

Locks current production contracts — including still-present holes —
so a later change cannot silently reopen double-deposit, ownerless
cancels, mutating GET, or global wire-trace replay.
"""
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401

from utility.crypto_receipt import generate_receipt
from utility.wire import (
    MemoryWireStore,
    WireCalendar,
    WireError,
    WirePolicy,
    WireService,
)

ET = timezone(timedelta(hours=-4))


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class ChequeRetryAndNsfTests(unittest.TestCase):
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
        self.cursor.fetchone.side_effect = None
        self.cursor.rowcount = 1
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def tearDown(self):
        self.db.commit.side_effect = None
        self.db.rollback.side_effect = None

    def _live_cheque_rows(self, sender=(1000.0, 1, "checkin")):
        # cheque lookup, receiver active, deposit flag for txn=-1, sender
        return [[(20, 10, 75.0, 1)], [(1,)], [(0,)], [sender]]

    def test_live_receipt_leaves_cheque_active_and_retryable(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = self._live_cheque_rows() + self._live_cheque_rows()

        first = customer.deposit_check("cust1", 5)
        self.assertIsInstance(first, dict)
        self.assertIn("signature", first)

        first_sql = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("UPDATE Cheque SET active=0" in sql for sql in first_sql))
        first_debits = sum(1 for sql in first_sql if "balance=balance-" in sql)
        self.assertEqual(first_debits, 1)

        second = customer.deposit_check("cust1", 5)
        self.assertIsInstance(second, dict)
        all_sql = [str(call.args[0]) for call in self.cursor.execute.call_args_list]
        self.assertFalse(any("UPDATE Cheque SET active=0" in sql for sql in all_sql))
        second_debits = sum(1 for sql in all_sql if "balance=balance-" in sql)
        self.assertEqual(second_debits, 2)

    def test_checking_nsf_leaves_pending_open(self):
        customer = self.Customers()
        self.cursor.fetchall.side_effect = [[(1,)], [(0,)], [(50.0, 1, "checkin")]]
        with patch.object(customer, "_cancel_pending_transaction") as cancel:
            self.assertEqual(
                customer.fund_transfers(10, 20, 100.0, transaction_no=55),
                "Insufficient Balance",
            )
            cancel.assert_not_called()

    def test_credit_nsf_cancel_has_no_owner_filter(self):
        customer = self.Customers()
        customer._cancel_pending_transaction(55)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("transaction_no", sql)
        self.assertIn("status = 1", sql)
        self.assertNotIn("approver1_id", sql)
        self.assertEqual(params, (55,))

    def test_owns_pending_non_numeric_txn_valueerrors(self):
        with self.assertRaises(ValueError):
            self.Customers().owns_pending_transaction("alice", "not-a-number")

    def test_deny_funds_non_numeric_txn_valueerrors(self):
        with self.assertRaises(ValueError):
            self.Customers().deny_funds_requested("abc", "alice")


class EmployeeDenyFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import employee as employee_module

        importlib.reload(employee_module)
        cls.Employee = employee_module.Employee
        cls.cursor = employee_module.cursor
        cls.db = employee_module.db

    def setUp(self):
        self.cursor.reset_mock()
        self.db.reset_mock()
        self.db.commit.side_effect = None

    def test_employee_deny_has_no_owner_or_status_where(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            self.assertEqual(emp.deny_funds_requested("emp1", 9), "Request Cancelled")
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("transaction_no = 9", sql)
        self.assertNotIn("approver1", sql)
        self.assertNotIn("AND status", sql)

    def test_employee_deny_non_numeric_txn_valueerrors(self):
        emp = self.Employee()
        with patch.object(emp, "get_employee_tier", return_value=2):
            with self.assertRaises(ValueError):
                emp.deny_funds_requested("emp1", "abc")


class MutatingGetMoneyRouteTests(unittest.TestCase):
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

    def test_get_approve_request_still_moves_money(self):
        self._session("cust1", "customer")
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            customers_cls.return_value.owns_pending_transaction.return_value = True
            emp_cls.return_value.get_amount_of_transaction.return_value = 40
            emp_cls.return_value.get_fromAccount_of_transaction.return_value = 10
            emp_cls.return_value.get_toAccount_of_transaction.return_value = 20
            emp_cls.return_value.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 40}
            response = self.client.get(
                "/approveRequest",
                json={"customer_id": "cust1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 40, 8)

    def test_get_approve_request_emp_skips_ownership(self):
        self._session("emp1", "employee", emp_tier=1)
        with patch("app.Customers") as customers_cls, patch("app.Employee") as emp_cls:
            emp_cls.return_value.get_amount_of_transaction.return_value = 50
            emp_cls.return_value.get_employee_tier.return_value = 1
            emp_cls.return_value.get_fromAccount_of_transaction.return_value = 10
            emp_cls.return_value.get_toAccount_of_transaction.return_value = 20
            emp_cls.return_value.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = {"amount": 50}
            response = self.client.get(
                "/approveRequestEmp",
                json={"userid": "emp1", "transaction_no": 8},
            )
        self.assertEqual(response.status_code, 200)
        customers_cls.return_value.owns_pending_transaction.assert_not_called()
        customers_cls.return_value.fund_transfers.assert_called_once_with(10, 20, 50, 8)

    def test_get_deposit_amount_still_queues(self):
        self._session("cust1", "customer")
        with patch("app.Employee") as emp_cls:
            emp_cls.return_value.add_transaction_deposit.return_value = "queued"
            response = self.client.get(
                "/depositAmount",
                json={"userid": "cust1", "account": 10, "amount": 25},
            )
        self.assertEqual(response.status_code, 200)
        emp_cls.return_value.add_transaction_deposit.assert_called_once_with(10, 25)

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


class WireIntegrityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.now = [_ts(2024, 6, 14, 16, 0)]
        self.debits = []
        self.credits = []
        self.accounts = {
            "alice": {
                "checkin": {"Account": 1001, "Balance": 5000},
                "savings": {"Account": 1002, "Balance": 80},
                "credit": {"Account": 1003, "Balance": -20},
            },
            "bob": {
                "checkin": {"Account": 2001, "Balance": 4000},
                "savings": {"Account": 2002, "Balance": 10},
                "credit": "None",
            },
        }

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return "Amount Debited"

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return "Success"

        self.service = WireService(
            WirePolicy(outbound_fee=Decimal("25.00"), dual_control_threshold=Decimal("10000.00")),
            MemoryWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: self.accounts.get(userid, {}),
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, owner="alice", actor=None, actor_type="customer", **kwargs):
        return self.service.add_beneficiary(
            owner_userid=owner,
            actor=actor or owner,
            actor_type=actor_type,
            nickname=kwargs.pop("nickname", "Chase Checking"),
            legal_name=kwargs.pop("legal_name", "Ada Lovelace"),
            aba=kwargs.pop("aba", "021000021"),
            account_number=kwargs.pop("account_number", "77881234"),
            street=kwargs.pop("street", "1 Federal St"),
            city=kwargs.pop("city", "New York"),
            state=kwargs.pop("state", "NY"),
            postal=kwargs.pop("postal", "10004"),
            default_account=kwargs.pop("default_account", "1001"),
        )

    def test_trace_id_is_global_so_bob_can_replay_alices_wire(self):
        alice_bene = self._add()
        bob_bene = self._add(
            owner="bob",
            nickname="Ally",
            legal_name="Bob Builder",
            account_number="11223344",
            default_account="2001",
        )
        first, created = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=alice_bene.beneficiary_id,
            amount="40.00",
            trace_id="wire-shared",
        )
        self.assertTrue(created)
        replay, created_again = self.service.originate(
            owner_userid="bob",
            actor="bob",
            actor_type="customer",
            beneficiary_id=bob_bene.beneficiary_id,
            amount="80.00",
            trace_id="wire-shared",
        )
        self.assertFalse(created_again)
        self.assertEqual(replay.wire_id, first.wire_id)
        self.assertEqual(replay.userid, "alice")
        principal_debits = [row for row in self.debits if row[1] == "40.00"]
        self.assertEqual(len(principal_debits), 1)
        self.assertEqual(principal_debits[0][0], "1001")
        self.assertFalse(any(row[0] == "2001" for row in self.debits))

    def test_hmac_receipt_from_debit_is_treated_as_failed_wire(self):
        bene = self._add()
        self.service.debit_fn = lambda account, amount, remark: generate_receipt(
            {"from_account": account, "amount": amount, "status": "done"}
        )
        with self.assertRaises(WireError) as ctx:
            self.service.originate(
                owner_userid="alice",
                actor="alice",
                actor_type="customer",
                beneficiary_id=bene.beneficiary_id,
                amount="30.00",
                trace_id="rcpt-wire",
            )
        self.assertEqual(ctx.exception.code, "failed")
        stored = self.service.store.get_wire_by_trace("rcpt-wire")
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.imad, "")

    def test_fee_nsf_still_marks_sent_with_imad(self):
        def debit(account, amount, remark):
            self.debits.append((account, amount, remark))
            if "wire fee" in remark:
                return "Insufficient Balance"
            return "Amount Debited"

        self.service.debit_fn = debit
        bene = self._add()
        wire, created = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="50.00",
            trace_id="fee-nsf",
        )
        self.assertTrue(created)
        self.assertEqual(wire.status, "sent")
        self.assertTrue(wire.imad)
        self.assertEqual(wire.fee_status, "nsf")

    def test_pending_release_transmits_after_cutoff(self):
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="maker",
            actor_type="tier1",
            beneficiary_id=bene.beneficiary_id,
            amount="10000.00",
            trace_id="hv-late",
        )
        self.assertEqual(wire.status, "pending_release")
        self.now[0] = _ts(2024, 6, 14, 18, 0)
        released = self.service.release_wire(
            wire_id=wire.wire_id, actor="checker", actor_type="tier2"
        )
        self.assertEqual(released.status, "sent")
        self.assertTrue(released.imad)
        self.assertEqual(self.debits[0][1], "10000.00")

    def test_queued_release_after_cutoff_requeues(self):
        self.now[0] = _ts(2024, 6, 14, 18, 0)
        bene = self._add()
        wire, _ = self.service.originate(
            owner_userid="alice",
            actor="alice",
            actor_type="customer",
            beneficiary_id=bene.beneficiary_id,
            amount="22.00",
            trace_id="q-late",
        )
        self.assertEqual(wire.status, "queued")
        released = self.service.release_wire(
            wire_id=wire.wire_id, actor="teller", actor_type="tier1"
        )
        self.assertEqual(released.status, "queued")
        self.assertEqual(self.debits, [])

    def test_empty_account_directory_skips_ownership(self):
        self.service.accounts_fn = lambda userid: {}
        bene = self._add(default_account="9999")
        self.assertEqual(bene.default_account, "9999")

    def test_claimed_admin_can_originate_for_another_customer(self):
        bene = self._add(
            owner="bob",
            actor="alice",
            actor_type="admin",
            nickname="BobsChase",
            legal_name="Bob Builder",
            account_number="55667788",
            default_account="2001",
        )
        self.assertEqual(bene.userid, "bob")
        wire, created = self.service.originate(
            owner_userid="bob",
            actor="alice",
            actor_type="admin",
            beneficiary_id=bene.beneficiary_id,
            amount="40.00",
            trace_id="admin-idor",
        )
        self.assertTrue(created)
        self.assertEqual(wire.userid, "bob")
        self.assertEqual(wire.status, "sent")
        self.assertEqual(self.debits[0], ("2001", "40.00", "wire to BobsChase"))


if __name__ == "__main__":
    unittest.main()
