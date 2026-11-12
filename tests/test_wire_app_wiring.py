"""Real app.py Fedwire wiring — stub Flask tests miss the closed-over service.

Routes close over the import-time service; loadCustomer/getCustomer read
get_wire_service(); session usertype is trusted as the Fedwire actor role.
Clock is pinned to Friday 2024-06-14 11:00 ET so cutoff/weekend cannot flake.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.wire import MemoryWireStore, WireCalendar, set_service

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def _load_app():
    with patch("twilio.rest.Client"):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        return app_module


def _accounts(_userid="alice"):
    return {
        "checkin": {"Account": 1001, "Balance": 5000},
        "savings": {"Account": 1002, "Balance": 80},
        "credit": "None",
    }


class WireAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []
        self.service = self.app_module.wire_service
        self.service.store = MemoryWireStore()
        self.service.clock = lambda: self.now[0]
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        self.service.debit_fn = self._debit
        self.service.credit_fn = self._credit
        self.service.accounts_fn = lambda userid: _accounts(userid)
        set_service(self.service)

    def _debit(self, account, amount, remark):
        self.debits.append((account, amount, remark))
        return "Amount Debited"

    def _credit(self, account, amount, remark):
        self.credits.append((account, amount, remark))
        return "Success"

    def _session(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _bene_payload(self, **overrides):
        body = {
            "userid": "alice",
            "nickname": "Chase",
            "legal_name": "Ada Lovelace",
            "aba": "021000021",
            "account_number": "77881234",
            "street": "1 Federal St",
            "city": "New York",
            "state": "NY",
            "postal": "10004",
            "default_account": "1001",
        }
        body.update(overrides)
        return body

    def _add_alice_bene(self, **overrides):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload(**overrides))
        self.assertEqual(added.status_code, 201)
        return added.get_json()["beneficiary"]["beneficiary_id"]

    def test_load_customer_includes_wires_without_account_number(self):
        bene_id = self._add_alice_bene()
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "dash-1",
                },
            )
            self.assertEqual(sent.status_code, 201)
            self.assertNotIn("account_number", sent.get_json()["wire"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]
        self.assertEqual(wires["beneficiaries"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", wires["beneficiaries"][0])
        self.assertNotIn("account_number", wires["wires"][0])
        self.assertEqual(wires["wires"][0]["account_last4"], "1234")

    def test_claimed_admin_session_can_wire_another_customer(self):
        bene_id = self._add_alice_bene()
        self._session("admin1", "admin")
        sent = self.client.post(
            "/sendWire",
            json={
                "userid": "admin1",
                "customer_id": "alice",
                "beneficiary_id": bene_id,
                "amount": "40.00",
                "trace_id": "admin-idor",
            },
        )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.get_json()["wire"]["userid"], "alice")
        self.assertEqual(self.debits[0], ("1001", "40.00", "wire to Chase"))

    def test_accounts_helper_failure_skips_ownership(self):
        self.service.accounts_fn = self.app_module._link_accounts
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.side_effect = RuntimeError("db down")
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(default_account="9999"),
            )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["beneficiary"]["default_account"], "9999")

    def test_get_send_and_add_still_mutate(self):
        self._session()
        added = self.client.get("/addWireBeneficiary", json=self._bene_payload(nickname="Ally"))
        self.assertEqual(added.status_code, 201)
        bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
        sent = self.client.get(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "40.00",
                "trace_id": "get-send",
            },
        )
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(self.debits[0][1], "40.00")

    def test_claimed_employee_get_run_due_transmits_another_customers_queue(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene_id = self._add_alice_bene()
        queued = self.client.post(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "50.00",
                "trace_id": "queued-1",
            },
        )
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        self._session("ghost", "employee")
        due = self.client.get(
            "/runDueWires",
            json={"userid": "ghost", "customer_id": "alice"},
        )
        self.assertEqual(due.status_code, 200)
        self.assertEqual(due.get_json()["Wires"]["wires"][0]["status"], "sent")
        self.assertEqual(self.debits[0], ("1001", "50.00", "wire to Chase"))

    def test_claimed_employee_get_reject_and_complete(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        bene_id = self._add_alice_bene()
        queued = self.client.post(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "33.00",
                "trace_id": "rej-1",
            },
        )
        wire_id = queued.get_json()["wire"]["wire_id"]
        self._session("ghost", "employee")
        rejected = self.client.get(
            "/rejectWire",
            json={"userid": "ghost", "wire_id": wire_id},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["wire"]["status"], "rejected")

        self.now[0] = ts(2024, 6, 14, 11, 0)
        self._session()
        live = self.client.post(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "22.00",
                "trace_id": "live-complete",
            },
        )
        live_id = live.get_json()["wire"]["wire_id"]
        self._session("ghost", "employee")
        completed = self.client.get(
            "/completeWire",
            json={"userid": "ghost", "wire_id": live_id},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")

    def test_dual_control_same_approver_on_real_app(self):
        bene_id = self._add_alice_bene()
        self._session("maker", "tier1")
        pending = self.client.post(
            "/sendWire",
            json={
                "userid": "maker",
                "customer_id": "alice",
                "beneficiary_id": bene_id,
                "amount": "10000.00",
                "trace_id": "dc-1",
            },
        )
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()["wire"]["status"], "pending_release")
        self.assertEqual(self.debits, [])
        same = self.client.post(
            "/releaseWire",
            json={"userid": "maker", "wire_id": pending.get_json()["wire"]["wire_id"]},
        )
        self.assertEqual(same.status_code, 403)
        self.assertEqual(same.get_json()["error"], "same_approver")
        self._session("checker", "tier2")
        released = self.client.post(
            "/releaseWire",
            json={"userid": "checker", "wire_id": pending.get_json()["wire"]["wire_id"]},
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        self.assertEqual(self.debits[0][1], "10000.00")

    def test_preview_does_not_debit_customer_waive_is_403(self):
        bene_id = self._add_alice_bene()
        self.now[0] = ts(2024, 6, 14, 18, 0)
        queued = self.client.post(
            "/sendWire",
            json={
                "userid": "alice",
                "beneficiary_id": bene_id,
                "amount": "40.00",
                "trace_id": "preview-1",
            },
        )
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.assertEqual(self.debits, [])
        preview = self.client.post(
            "/previewWire",
            json={"userid": "alice", "beneficiary_id": bene_id, "amount": "40.00"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()["preview"]["fee"], "25.00")
        self.assertEqual(self.debits, [])
        waived = self.client.post(
            "/waiveWireFee",
            json={"userid": "alice", "wire_id": queued.get_json()["wire"]["wire_id"]},
        )
        self.assertEqual(waived.status_code, 403)
        self.assertEqual(waived.get_json()["error"], "wire_forbidden")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        set_service(None)
        self._session()
        added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Wires"]["enabled"], False)
        self.assertEqual(dash.get_json()["Wires"]["beneficiaries"], [])


if __name__ == "__main__":
    unittest.main()
