import unittest

from flask import Flask, jsonify, request, session

from utility.audit_trail import (
    AuditFilter,
    AuditTrail,
    MemoryAuditStore,
    attach_audit_routes,
    outcome_from_status,
    record_request,
)


def build_app(trail):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    attach_audit_routes(app, trail)

    def _emit(action, status, resource_type=None, resource_id=None, extra=None):
        values = request.get_json(silent=True) or {}
        record_request(
            action,
            outcome_from_status(status),
            session_data=dict(session),
            headers={key: value for key, value in request.headers.items()},
            remote_addr=request.remote_addr,
            trail=trail,
            resource_type=resource_type,
            resource_id=resource_id,
            details=extra or {"amount": values.get("amount")},
        )
        return jsonify({"message": "ok" if status < 300 else "no"}), status

    @app.route("/login", methods=["POST"])
    def login():
        values = request.get_json(silent=True) or {}
        if values.get("password") != "secret":
            record_request(
                "login",
                "denied",
                actor_id=values.get("userid"),
                actor_type=values.get("usertype"),
                remote_addr=request.remote_addr,
                trail=trail,
                details={"password": values.get("password")},
            )
            return jsonify({"message": "Invalid credentials"}), 401
        session["userid"] = values["userid"]
        session["usertype"] = values.get("usertype", "customer")
        record_request(
            "login",
            "success",
            session_data=dict(session),
            remote_addr=request.remote_addr,
            trail=trail,
            details={"password": values.get("password")},
        )
        return jsonify({"message": "ok"}), 200

    @app.route("/fundTransfer", methods=["POST"])
    def fund_transfer():
        if "userid" not in session:
            record_request("fund_transfer", "denied", trail=trail, details={"reason": "anonymous"})
            return jsonify({"message": "Unauthorized"}), 401
        values = request.get_json(silent=True) or {}
        if values.get("userid") != session["userid"]:
            return _emit("fund_transfer", 401, extra={"reason": "mismatch"})
        return _emit(
            "fund_transfer",
            200,
            resource_type="account",
            resource_id=values.get("fromAccount"),
            extra={"amount": values.get("amount"), "toAccount": values.get("toAccount")},
        )

    @app.route("/withdrawAmount", methods=["POST"])
    def withdraw():
        if "userid" not in session:
            return jsonify({"message": "Unauthorized"}), 401
        values = request.get_json(silent=True) or {}
        return _emit("withdraw", 200, resource_type="account", resource_id=values.get("account"))

    @app.route("/getSystemLogs", methods=["POST"])
    def system_logs():
        if session.get("usertype") != "admin":
            return jsonify({"message": "Unauthorized"}), 401
        return jsonify({"message": "file"}), 200

    return app


class AuditRouteTests(unittest.TestCase):
    def setUp(self):
        self.trail = AuditTrail(MemoryAuditStore())
        self.app = build_app(self.trail)
        self.client = self.app.test_client()

    def _login(self, userid="root", usertype="admin"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def test_login_failure_is_queryable_and_redacted(self):
        resp = self.client.post(
            "/login",
            json={"userid": "alice", "password": "wrong", "usertype": "customer"},
        )
        self.assertEqual(resp.status_code, 401)
        events = self.trail.query(AuditFilter(action="login", outcome="denied"))
        self.assertEqual(events["total"], 1)
        self.assertEqual(events["events"][0]["actor_id"], "alice")
        self.assertEqual(events["events"][0]["details"]["password"], "[REDACTED]")

    def test_login_success_then_money_movement(self):
        resp = self.client.post(
            "/login",
            json={"userid": "alice", "password": "secret", "usertype": "customer"},
        )
        self.assertEqual(resp.status_code, 200)
        moved = self.client.post(
            "/fundTransfer",
            json={"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "750"},
        )
        self.assertEqual(moved.status_code, 200)
        self.client.post("/withdrawAmount", json={"userid": "alice", "account": "10", "amount": "20"})
        transfers = self.trail.query(AuditFilter(action="fund_transfer", actor_id="alice"))
        self.assertEqual(transfers["total"], 1)
        self.assertEqual(transfers["events"][0]["resource_id"], "10")
        self.assertEqual(transfers["events"][0]["details"]["amount"], "750")

    def test_anonymous_transfer_denied_event(self):
        resp = self.client.post(
            "/fundTransfer",
            json={"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "5"},
        )
        self.assertEqual(resp.status_code, 401)
        events = self.trail.query(AuditFilter(action="fund_transfer", outcome="denied"))
        self.assertEqual(events["total"], 1)

    def test_admin_query_route_and_customer_blocked(self):
        self.trail.record("deposit", "success", actor_id="alice", resource_id="9")
        self._login("root", "admin")
        resp = self.client.post("/queryAudit", json={"userid": "root", "action": "deposit"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["events"][0]["resource_id"], "9")
        self.assertIn("login", body["actions"])

        self._login("alice", "customer")
        denied = self.client.post("/queryAudit", json={"userid": "alice"})
        self.assertEqual(denied.status_code, 401)

    def test_get_system_logs_still_admin_only(self):
        self._login("alice", "customer")
        self.assertEqual(self.client.post("/getSystemLogs", json={"userid": "alice"}).status_code, 401)
        self._login("root", "admin")
        self.assertEqual(self.client.post("/getSystemLogs", json={"userid": "root"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
