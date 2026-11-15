"""Real app.py wiring for Fedwire staff GET mutations — isolated Flask tests miss this.

Routes close over the import-time service; session usertype is trusted as the
wire actor role. Clock is pinned to Friday 2024-06-14 11:00 ET so cutoff
cannot flake. Unique vs open coverage PRs that already cover add/send GET,
pause GET IDOR, and run-due GET IDOR.
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
        self.service = self.app_module.wire_service
        self.service.store = MemoryWireStore()
        self.service.clock = lambda: self.now[0]
        self.service.calendar = WireCalendar(cutoff_hour=17, tz_offset_hours=-4)
        self.service.policy.outbound_fee = Decimal("25.00")
        self.service.policy.dual_control_threshold = Decimal("10000.00")
        self.service.policy.min_amount = Decimal("10.00")
        self.service.policy.max_amount = Decimal("1000000.00")
        set_service(self.service)

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

    def test_get_customer_includes_wires_for_staff_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        self.assertNotIn("account_number", added.get_json()["beneficiary"])
        self._session("teller", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]
        self.assertEqual(wires["beneficiaries"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", wires["beneficiaries"][0])

    def test_claimed_employee_get_override_ofac_transmits_another_customers_hold(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(nickname="Blocked", legal_name="Blocked Person"),
            )
            self.assertEqual(added.status_code, 201)
            held = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "40.00",
                    "trace_id": "ofac-idor-1",
                },
            )
        self.assertEqual(held.status_code, 201)
        self.assertEqual(held.get_json()["wire"]["status"], "held")
        wire_id = held.get_json()["wire"]["wire_id"]
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            overridden = self.client.get(
                "/overrideOfac",
                json={"userid": "bob", "wire_id": wire_id, "note": "cleared"},
            )
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.get_json()["wire"]["status"], "sent")
        self.assertEqual(overridden.get_json()["wire"]["userid"], "alice")
        customers_cls.return_value.debit_request.assert_called()

    def test_claimed_employee_get_waive_then_release_after_cutoff_stays_queued(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(nickname="Queued")
            )
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "33.00",
                    "trace_id": "waive-rel-1",
                },
            )
        self.assertEqual(queued.status_code, 201)
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        wire_id = queued.get_json()["wire"]["wire_id"]
        self._session("bob", "employee")
        waived = self.client.get(
            "/waiveWireFee",
            json={"userid": "bob", "wire_id": wire_id},
        )
        self.assertEqual(waived.status_code, 200)
        self.assertEqual(waived.get_json()["wire"]["fee_status"], "waived")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            still_queued = self.client.get(
                "/releaseWire",
                json={"userid": "bob", "wire_id": wire_id},
            )
        self.assertEqual(still_queued.status_code, 200)
        self.assertEqual(still_queued.get_json()["wire"]["status"], "queued")
        customers_cls.return_value.debit_request.assert_not_called()
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.get(
                "/releaseWire",
                json={"userid": "bob", "wire_id": wire_id},
            )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        self.assertEqual(released.get_json()["wire"]["fee_status"], "waived")
        args, _kwargs = customers_cls.return_value.debit_request.call_args
        self.assertEqual(args[1], "33.00")
        self.assertEqual(customers_cls.return_value.debit_request.call_count, 1)

    def test_claimed_employee_get_complete_and_recall_another_customer(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            sent = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "40.00",
                    "trace_id": "complete-idor-1",
                },
            )
            other = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "15.00",
                    "trace_id": "recall-idor-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        complete_id = sent.get_json()["wire"]["wire_id"]
        recall_id = other.get_json()["wire"]["wire_id"]
        self._session("bob", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.credit_request.return_value = "Success"
            completed = self.client.get(
                "/completeWire",
                json={"userid": "bob", "wire_id": complete_id},
            )
            already = self.client.get(
                "/recallWire",
                json={"userid": "bob", "wire_id": complete_id},
            )
            recalled = self.client.get(
                "/recallWire",
                json={"userid": "bob", "wire_id": recall_id},
            )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")
        self.assertEqual(already.status_code, 409)
        self.assertEqual(already.get_json()["error"], "already_completed")
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()["wire"]["status"], "recalled")
        self.assertEqual(recalled.get_json()["wire"]["userid"], "alice")
        customers_cls.return_value.credit_request.assert_called()

    def test_claimed_employee_get_reject_queued_and_get_resume_after_pause(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary", json=self._bene_payload(nickname="Wells")
            )
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "33.00",
                    "trace_id": "reject-idor-1",
                },
            )
            paused = self.client.post(
                "/pauseWireBeneficiary",
                json={"userid": "alice", "beneficiary_id": bene_id},
            )
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.assertEqual(paused.get_json()["beneficiary"]["status"], "paused")
        wire_id = queued.get_json()["wire"]["wire_id"]
        self._session("bob", "employee")
        rejected = self.client.get(
            "/rejectWire",
            json={"userid": "bob", "wire_id": wire_id},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.get_json()["wire"]["status"], "rejected")
        resumed = self.client.get(
            "/resumeWireBeneficiary",
            json={"userid": "bob", "beneficiary_id": bene_id},
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["beneficiary"]["status"], "active")
        archived = self.client.get(
            "/archiveWireBeneficiary",
            json={"userid": "bob", "beneficiary_id": bene_id},
        )
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.get_json()["beneficiary"]["status"], "archived")
        self.assertEqual(archived.get_json()["beneficiary"]["userid"], "alice")


if __name__ == "__main__":
    unittest.main()
