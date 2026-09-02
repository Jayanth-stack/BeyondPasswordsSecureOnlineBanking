import os
import tempfile
import unittest

from utility.audit_trail import (
    ACTIONS,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    REDACT_VALUE,
    AuditFilter,
    AuditTrail,
    MemoryAuditStore,
    SqliteAuditStore,
    can_query_audit,
    handle_query_request,
    outcome_from_status,
    redact,
    record_request,
)


class RedactionTests(unittest.TestCase):
    def test_redacts_password_otp_ssn_and_nested_secrets(self):
        payload = {
            "userid": "alice",
            "password": "hunter2",
            "otp_code": "123456",
            "ssn": "111-22-3333",
            "nested": {"newPassword": "secret", "amount": "50"},
            "note": "password=leaked",
        }
        cleaned = redact(payload)
        self.assertEqual(cleaned["userid"], "alice")
        self.assertEqual(cleaned["password"], REDACT_VALUE)
        self.assertEqual(cleaned["otp_code"], REDACT_VALUE)
        self.assertEqual(cleaned["ssn"], REDACT_VALUE)
        self.assertEqual(cleaned["nested"]["newPassword"], REDACT_VALUE)
        self.assertEqual(cleaned["nested"]["amount"], "50")
        self.assertEqual(cleaned["note"], REDACT_VALUE)

    def test_redacts_phone_and_email_pii(self):
        cleaned = redact({"phone": "+15550001", "email_id": "a@b.c", "fromAccount": 10})
        self.assertEqual(cleaned["phone"], REDACT_VALUE)
        self.assertEqual(cleaned["email_id"], REDACT_VALUE)
        self.assertEqual(cleaned["fromAccount"], 10)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.trail = AuditTrail(MemoryAuditStore())

    def test_record_and_filter_by_actor_action_outcome(self):
        self.trail.record("login", "denied", actor_id="alice", actor_type="customer")
        self.trail.record(
            "fund_transfer",
            "success",
            actor_id="alice",
            actor_type="customer",
            resource_type="account",
            resource_id=10,
            details={"amount": "750", "password": "nope"},
        )
        self.trail.record("login", "success", actor_id="bob", actor_type="admin")

        by_actor = self.trail.query(AuditFilter(actor_id="alice"))
        self.assertEqual(by_actor["total"], 2)
        transfer = [event for event in by_actor["events"] if event["action"] == "fund_transfer"][0]
        self.assertEqual(transfer["details"]["password"], REDACT_VALUE)
        self.assertEqual(transfer["details"]["amount"], "750")
        self.assertEqual(transfer["resource_id"], "10")

        denied = self.trail.query(AuditFilter(outcome="denied"))
        self.assertEqual(denied["total"], 1)
        self.assertEqual(denied["events"][0]["actor_id"], "alice")

        transfers = self.trail.query(AuditFilter(action="fund_transfer"))
        self.assertEqual(transfers["total"], 1)

    def test_newest_first_and_pagination(self):
        for index in range(5):
            self.trail.record("login", "success", actor_id=str(index), details={"n": index})
        page = self.trail.query(AuditFilter(limit=2, offset=0))
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(page["events"][0]["details"]["n"], 4)
        second = self.trail.query(AuditFilter(limit=2, offset=2))
        self.assertEqual(second["events"][0]["details"]["n"], 2)

    def test_text_search_and_time_window(self):
        first = self.trail.record("withdraw", "success", actor_id="alice", details={"ref": "abc123"})
        self.trail.record("deposit", "success", actor_id="bob", details={"ref": "zzz"})
        found = self.trail.query(AuditFilter(q="abc123"))
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["events"][0]["actor_id"], "alice")
        before = self.trail.query(AuditFilter(until=first["ts"]))
        self.assertGreaterEqual(before["total"], 1)
        none = self.trail.query(AuditFilter(since="2099-01-01T00:00:00Z"))
        self.assertEqual(none["total"], 0)

    def test_record_swallows_store_errors(self):
        class Boom:
            def append(self, event):
                raise RuntimeError("disk full")

            def query(self, filt):
                return {"events": [], "total": 0}

        trail = AuditTrail(Boom())
        self.assertIsNone(trail.record("login", "success", actor_id="alice"))

    def test_filter_limit_clamped(self):
        filt = AuditFilter.from_mapping({"limit": 9999, "offset": -4, "action": "login"})
        self.assertEqual(filt.limit, MAX_LIMIT)
        self.assertEqual(filt.offset, 0)
        self.assertEqual(filt.action, "login")
        empty = AuditFilter.from_mapping({})
        self.assertEqual(empty.limit, DEFAULT_LIMIT)


class SqliteStoreTests(unittest.TestCase):
    def test_survives_process_restart(self):
        handle, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        first = AuditTrail(SqliteAuditStore(path))
        first.record(
            "deactivate_account",
            "success",
            actor_id="tier2",
            actor_type="tier2",
            resource_type="account",
            resource_id="42",
            ip="10.0.0.8",
        )
        second = AuditTrail(SqliteAuditStore(path))
        result = second.query(AuditFilter(action="deactivate_account", resource_id="42"))
        self.assertEqual(result["total"], 1)
        event = result["events"][0]
        self.assertEqual(event["actor_id"], "tier2")
        self.assertEqual(event["ip"], "10.0.0.8")
        self.assertIn("deactivate_account", result["actions"])
        self.assertEqual(result["actions"], list(ACTIONS))


class QueryAuthTests(unittest.TestCase):
    def setUp(self):
        self.trail = AuditTrail(MemoryAuditStore())
        self.trail.record("login", "success", actor_id="alice")
        self.trail.record("fund_transfer", "success", actor_id="alice", resource_id="10")

    def test_admin_can_query(self):
        result = handle_query_request(
            {"userid": "root", "action": "fund_transfer"},
            {"userid": "root", "usertype": "admin"},
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
            trail=self.trail,
        )
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["total"], 1)
        self.assertEqual(result["body"]["events"][0]["action"], "fund_transfer")
        queries = self.trail.query(AuditFilter(action="audit_query"))
        self.assertEqual(queries["events"][0]["outcome"], "success")
        self.assertEqual(queries["events"][0]["ip"], "203.0.113.9")

    def test_customer_cannot_query(self):
        result = handle_query_request(
            {"userid": "alice"},
            {"userid": "alice", "usertype": "customer"},
            trail=self.trail,
        )
        self.assertEqual(result["status"], 401)
        self.assertFalse(can_query_audit({"userid": "alice", "usertype": "customer"}))

    def test_userid_mismatch_denied(self):
        result = handle_query_request(
            {"userid": "other"},
            {"userid": "root", "usertype": "admin"},
            trail=self.trail,
        )
        self.assertEqual(result["status"], 401)
        self.assertIn("mismatch", result["body"]["message"].lower())

    def test_missing_userid_invalid(self):
        result = handle_query_request(
            {},
            {"userid": "root", "usertype": "admin"},
            trail=self.trail,
        )
        self.assertEqual(result["status"], 400)

    def test_anonymous_denied(self):
        result = handle_query_request({"userid": "root"}, {}, trail=self.trail)
        self.assertEqual(result["status"], 401)


class HelperTests(unittest.TestCase):
    def test_outcome_from_status(self):
        self.assertEqual(outcome_from_status(200), "success")
        self.assertEqual(outcome_from_status(302), "invalid")
        self.assertEqual(outcome_from_status(401), "denied")
        self.assertEqual(outcome_from_status(403), "denied")
        self.assertEqual(outcome_from_status(400), "invalid")
        self.assertEqual(outcome_from_status(500), "failure")

    def test_record_request_uses_session_and_redacts(self):
        trail = AuditTrail(MemoryAuditStore())
        record_request(
            "login",
            "denied",
            session_data={"userid": "alice", "usertype": "customer"},
            remote_addr="127.0.0.1",
            trail=trail,
            details={"password": "hunter2", "userid": "alice"},
        )
        event = trail.query(AuditFilter())["events"][0]
        self.assertEqual(event["actor_id"], "alice")
        self.assertEqual(event["ip"], "127.0.0.1")
        self.assertEqual(event["details"]["password"], REDACT_VALUE)
        self.assertEqual(event["details"]["userid"], "alice")


if __name__ == "__main__":
    unittest.main()
