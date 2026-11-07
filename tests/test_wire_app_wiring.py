"""Real app.py wiring for Fedwire — isolated Flask tests miss this.

Routes close over the import-time wire_service; loadCustomer/getCustomer read
get_wire_service(); session usertype is trusted as the wire actor role.
GET is honored on every money-moving wire endpoint.
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
        self.service.policy.enabled = True
        self.service.policy.customer_manage = True
        self.service.policy.customer_send = True
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

    def test_load_customer_includes_wires_without_account_number(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn("account_number", added.get_json()["beneficiary"])
            response = self.client.post("/loadCustomer")
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]
        self.assertEqual(wires["beneficiaries"][0]["nickname"], "Chase")
        self.assertEqual(wires["beneficiaries"][0]["account_last4"], "1234")
        self.assertNotIn("account_number", wires["beneficiaries"][0])
        self.assertTrue(wires["enabled"])

    def test_get_customer_includes_wires_for_staff(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
        self.assertEqual(added.status_code, 201)
        self._session("teller", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            response = self.client.post(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["Wires"]["beneficiaries"][0]["nickname"], "Chase")
        self.assertNotIn("account_number", response.get_json()["Wires"]["beneficiaries"][0])

    def test_get_send_still_originates(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.get("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "get-send-1",
                },
            )
        self.assertEqual(sent.status_code, 201)
        wire = sent.get_json()["wire"]
        self.assertEqual(wire["status"], "sent")
        self.assertTrue(wire["imad"].startswith("20240614KONOHA01"))
        self.assertNotIn("account_number", wire)
        debit = customers_cls.return_value.debit_request
        self.assertGreaterEqual(debit.call_count, 1)
        self.assertEqual(debit.call_args_list[0].args[0], "1001")
        self.assertEqual(debit.call_args_list[0].args[1], "40.00")
        self.assertIn("wire to Chase", debit.call_args_list[0].kwargs.get("remark") or debit.call_args_list[0].args[2])

    def test_claimed_employee_get_send_originates_another_customer(self):
        # /login stores client-supplied usertype; wire routes trust it as staff.
        self._session("alice", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = {
                "checkin": {"Account": 2001, "Balance": 5000},
                "savings": "None",
                "credit": "None",
            }
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(
                    userid="alice",
                    customer_id="bob",
                    nickname="BobsChase",
                    default_account="2001",
                ),
            )
            self.assertEqual(added.status_code, 201)
            self.assertEqual(added.get_json()["beneficiary"]["userid"], "bob")
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            sent = self.client.get(
                "/sendWire",
                json={
                    "userid": "alice",
                    "customer_id": "bob",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "claimed-emp-send",
                },
            )
        self.assertEqual(sent.status_code, 201)
        wire = sent.get_json()["wire"]
        self.assertEqual(wire["userid"], "bob")
        self.assertEqual(wire["status"], "sent")
        self.assertTrue(wire["imad"])
        debit = customers_cls.return_value.debit_request
        self.assertEqual(debit.call_args_list[0].args[0], "2001")
        self.assertEqual(debit.call_args_list[0].args[1], "40.00")

    def test_claimed_employee_get_complete_and_recall_another_customer(self):
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            customers_cls.return_value.credit_request.return_value = "Success"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            first = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "40.00",
                    "trace_id": "complete-me",
                },
            )
            second = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "15.00",
                    "trace_id": "recall-me",
                },
            )
        complete_id = first.get_json()["wire"]["wire_id"]
        recall_id = second.get_json()["wire"]["wire_id"]

        self._session("alice", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.credit_request.return_value = "Success"
            completed = self.client.get(
                "/completeWire",
                json={"userid": "alice", "wire_id": complete_id},
            )
            recalled = self.client.get(
                "/recallWire",
                json={"userid": "alice", "wire_id": recall_id},
            )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["wire"]["status"], "completed")
        self.assertTrue(completed.get_json()["wire"]["omad"])
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.get_json()["wire"]["status"], "recalled")
        credit = customers_cls.return_value.credit_request
        self.assertEqual(credit.call_count, 2)
        self.assertEqual(credit.call_args_list[0].args[1], "15.00")
        self.assertEqual(credit.call_args_list[1].args[1], "25.00")

    def test_claimed_employee_get_waive_fee_skips_fee_debit(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            bene_id = added.get_json()["beneficiary"]["beneficiary_id"]
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": bene_id,
                    "amount": "33.00",
                    "trace_id": "waive-me",
                },
            )
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        wire_id = queued.get_json()["wire"]["wire_id"]

        self._session("alice", "employee")
        waived = self.client.get(
            "/waiveWireFee",
            json={"userid": "alice", "wire_id": wire_id},
        )
        self.assertEqual(waived.status_code, 200)
        self.assertEqual(waived.get_json()["wire"]["fee_status"], "waived")

        self.now[0] = ts(2024, 6, 17, 10, 0)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            due = self.client.get(
                "/runDueWires",
                json={"userid": "alice", "customer_id": "alice"},
            )
        self.assertEqual(due.status_code, 200)
        sent = [row for row in due.get_json()["Wires"]["wires"] if row["wire_id"] == wire_id][0]
        self.assertEqual(sent["status"], "sent")
        debit = customers_cls.return_value.debit_request
        self.assertEqual(debit.call_count, 1)
        self.assertEqual(debit.call_args.args[1], "33.00")

    def test_get_customer_snapshot_transmits_queued_wires(self):
        # snapshot() always run_due()s, so a staff lookup posts queued wires.
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "50.00",
                    "trace_id": "lookup-due",
                },
            )
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.now[0] = ts(2024, 6, 17, 10, 0)
        self._session("teller", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            response = self.client.get(
                "/getCustomer",
                json={"userid": "teller", "customer_id": "alice"},
            )
        self.assertEqual(response.status_code, 200)
        wires = response.get_json()["Wires"]["wires"]
        self.assertEqual(wires[0]["status"], "sent")
        self.assertTrue(wires[0]["imad"])
        debit = customers_cls.return_value.debit_request
        self.assertGreaterEqual(debit.call_count, 1)
        self.assertEqual(debit.call_args_list[0].args[1], "50.00")

    def test_claimed_employee_get_list_wires_transmits_another_customer(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        self._session()
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            queued = self.client.post(
                "/sendWire",
                json={
                    "userid": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "44.00",
                    "trace_id": "list-due",
                },
            )
        self.assertEqual(queued.get_json()["wire"]["status"], "queued")
        self.now[0] = ts(2024, 6, 17, 10, 0)
        self._session("eve", "employee")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            listed = self.client.get(
                "/listWires",
                json={"userid": "eve", "customer_id": "alice"},
            )
        self.assertEqual(listed.status_code, 200)
        row = listed.get_json()["Wires"]["wires"][0]
        self.assertEqual(row["userid"], "alice")
        self.assertEqual(row["status"], "sent")
        self.assertTrue(row["imad"])
        debit = customers_cls.return_value.debit_request
        self.assertEqual(debit.call_args_list[0].args[1], "44.00")

    def test_release_same_approver_is_403_then_other_staff_sends(self):
        self._session("maker", "tier1")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            added = self.client.post(
                "/addWireBeneficiary",
                json=self._bene_payload(userid="maker", customer_id="alice"),
            )
            self.assertEqual(added.status_code, 201)
            pending = self.client.post(
                "/sendWire",
                json={
                    "userid": "maker",
                    "customer_id": "alice",
                    "beneficiary_id": added.get_json()["beneficiary"]["beneficiary_id"],
                    "amount": "10000.00",
                    "trace_id": "dual-1",
                },
            )
        self.assertEqual(pending.status_code, 201)
        self.assertEqual(pending.get_json()["wire"]["status"], "pending_release")
        wire_id = pending.get_json()["wire"]["wire_id"]

        denied = self.client.post(
            "/releaseWire",
            json={"userid": "maker", "wire_id": wire_id},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()["error"], "same_approver")

        self._session("checker", "tier2")
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.debit_request.return_value = "Amount Debited"
            released = self.client.post(
                "/releaseWire",
                json={"userid": "checker", "wire_id": wire_id},
            )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()["wire"]["status"], "sent")
        self.assertEqual(released.get_json()["wire"]["releaser"], "checker")

    def test_staff_missing_customer_id_on_send_is_400(self):
        self._session("teller", "tier1")
        response = self.client.post(
            "/sendWire",
            json={"userid": "teller", "beneficiary_id": "missing", "amount": "40.00"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "missing_customer_id")

    def test_snapshot_none_service_does_not_disable_closed_over_routes(self):
        self._session()
        set_service(None)
        with patch("app.Customers") as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {"first_name": "Ada"}
            customers_cls.return_value.get_funds_requests.return_value = "None"
            added = self.client.post("/addWireBeneficiary", json=self._bene_payload())
            self.assertEqual(added.status_code, 201)
            dash = self.client.post("/loadCustomer")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.get_json()["Wires"]["enabled"], False)
        self.assertEqual(dash.get_json()["Wires"]["beneficiaries"], [])

    def test_customer_cannot_waive_or_complete(self):
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
                    "trace_id": "cust-staff",
                },
            )
        wire_id = sent.get_json()["wire"]["wire_id"]
        waived = self.client.post("/waiveWireFee", json={"userid": "alice", "wire_id": wire_id})
        completed = self.client.post("/completeWire", json={"userid": "alice", "wire_id": wire_id})
        self.assertEqual(waived.status_code, 403)
        self.assertEqual(waived.get_json()["error"], "wire_forbidden")
        self.assertEqual(completed.status_code, 403)
        self.assertEqual(completed.get_json()["error"], "wire_forbidden")


if __name__ == "__main__":
    unittest.main()
