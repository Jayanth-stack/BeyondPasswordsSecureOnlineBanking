import unittest
from decimal import Decimal

from flask import Flask, jsonify, request, session

from utility.step_up import (
    PURPOSE_APPROVE,
    PURPOSE_CHEQUE,
    PURPOSE_TRANSFER,
    PURPOSE_WITHDRAW,
    LocalOtpProvider,
    MemoryStepUpStore,
    StepUpPolicy,
    StepUpService,
    enforce_step_up,
    fingerprint_from_values,
    infer_purpose,
    policy_public_dict,
)


def _json(result):
    resp = jsonify(result.body)
    resp.status_code = result.status
    for key, value in result.headers.items():
        resp.headers[key] = value
    return resp


def build_app(service, phones):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True

    @app.route("/loadCustomer", methods=["POST"])
    def load_customer():
        if "userid" not in session or session.get("usertype") != "customer":
            return jsonify({"message": "Unauthorized access or session expired"}), 401
        return jsonify({"Accounts": {}, "Info": {}, "FundsRequests": "None", "StepUp": policy_public_dict(service)}), 200

    @app.route("/sendOTP", methods=["POST"])
    def send_otp():
        values = request.get_json() or {}
        purpose = infer_purpose(values)
        if purpose in (PURPOSE_TRANSFER, PURPOSE_WITHDRAW, PURPOSE_CHEQUE, PURPOSE_APPROVE):
            if "userid" not in session or session.get("userid") != values.get("userid"):
                return jsonify({"message": "Unauthorized access or session expired"}), 401
            phone = phones.get(values["userid"])
            fingerprint = fingerprint_from_values(purpose, values)
            return _json(
                service.start_challenge(
                    userid=values["userid"],
                    purpose=purpose,
                    phone=phone or "",
                    fingerprint=fingerprint,
                )
            )
        return jsonify({"message": "OTP Sent"}), 200

    @app.route("/verifyOTP", methods=["POST"])
    def verify_otp():
        values = request.get_json() or {}
        if "userid" not in session or session.get("userid") != values.get("userid"):
            return jsonify({"message": "Unauthorized access or session expired"}), 401
        purpose = infer_purpose(values)
        return _json(
            service.verify_challenge(
                userid=values["userid"],
                purpose=purpose,
                code=values.get("otp") or "",
            )
        )

    def _money_route(purpose, success_message):
        if "userid" not in session:
            return jsonify({"message": "Unauthorized"}), 401
        values = request.get_json() or {}
        if values.get("userid") != session["userid"]:
            return jsonify({"message": "User ID mismatch"}), 401
        blocked = enforce_step_up(
            values,
            userid=session["userid"],
            usertype=session.get("usertype"),
            purpose=purpose,
            amount_raw=values.get("amount"),
            service=service,
        )
        if blocked:
            return _json(blocked)
        return jsonify({"message": success_message}), 200

    @app.route("/fundTransfer", methods=["POST"])
    def fund_transfer():
        return _money_route(PURPOSE_TRANSFER, "Request to be approved by tier1 employee")

    @app.route("/withdrawAmount", methods=["POST"])
    def withdraw():
        return _money_route(PURPOSE_WITHDRAW, "Amount Debited")

    @app.route("/getCashierCheque", methods=["POST"])
    def cheque():
        if "userid" not in session:
            return jsonify({"message": "Unauthorized"}), 401
        values = request.get_json() or {}
        if values.get("userid") != session["userid"]:
            return jsonify({"message": "User ID mismatch"}), 401
        blocked = enforce_step_up(
            values,
            userid=session["userid"],
            usertype=session.get("usertype"),
            purpose=PURPOSE_CHEQUE,
            amount_raw=values.get("amount"),
            service=service,
        )
        if blocked:
            return _json(blocked)
        return jsonify({"message": "Success"}), 200

    @app.route("/approveRequest", methods=["POST"])
    def approve():
        if "userid" not in session:
            return jsonify({"message": "Unauthorized"}), 401
        values = request.get_json() or {}
        blocked = enforce_step_up(
            values,
            userid=session["userid"],
            usertype=session.get("usertype"),
            purpose=PURPOSE_APPROVE,
            amount_raw=values.get("amount"),
            service=service,
        )
        if blocked:
            return _json(blocked)
        return jsonify({"message": "done"}), 200

    @app.route("/depositAmount", methods=["POST"])
    def deposit():
        if "userid" not in session:
            return jsonify({"message": "Unauthorized"}), 401
        return jsonify({"message": "Success"}), 200

    return app


class StepUpRouteTests(unittest.TestCase):
    def setUp(self):
        self.service = StepUpService(
            store=MemoryStepUpStore(),
            policy=StepUpPolicy(threshold=Decimal("500.00"), cooldown_seconds=45),
            provider=LocalOtpProvider(echo=True),
        )
        self.app = build_app(self.service, {"alice": "+15550001"})
        self.client = self.app.test_client()

    def _login(self, userid="alice", usertype="customer"):
        with self.client.session_transaction() as sess:
            sess["userid"] = userid
            sess["usertype"] = usertype

    def _challenge(self, extra=None, userid="alice"):
        payload = {
            "userid": userid,
            "requester": "Customer",
            "purpose": "transfer",
            "fromAccount": "10",
            "toAccount": "20",
            "amount": "750",
        }
        if extra:
            payload.update(extra)
        return self.client.post("/sendOTP", json=payload)

    def test_load_customer_includes_policy(self):
        self._login()
        resp = self.client.post("/loadCustomer", json={"customer_id": "alice"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["StepUp"]["threshold"], "500.00")
        self.assertTrue(body["StepUp"]["enabled"])

    def test_low_value_transfer_no_token(self):
        self._login()
        resp = self.client.post(
            "/fundTransfer",
            json={"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "100"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_high_value_transfer_requires_then_accepts_token(self):
        self._login()
        payload = {"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "750"}
        denied = self.client.post("/fundTransfer", json=payload)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()["error"], "step_up_required")

        sent = self._challenge()
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.get_json()["message"], "OTP Sent")
        code = sent.get_json()["debug_otp"]
        verified = self.client.post(
            "/verifyOTP",
            json={"userid": "alice", "otp": code, "purpose": "transfer", "fromAccount": "10", "toAccount": "20", "amount": "750"},
        )
        self.assertEqual(verified.get_json()["message"], "verified")
        token = verified.get_json()["confirmation_token"]
        payload["confirmation_token"] = token
        ok = self.client.post("/fundTransfer", json=payload)
        self.assertEqual(ok.status_code, 200)

        replay = self.client.post("/fundTransfer", json=payload)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.get_json()["error"], "step_up_used")

    def test_token_does_not_cover_different_amount(self):
        self._login()
        sent = self._challenge()
        verified = self.client.post(
            "/verifyOTP",
            json={
                "userid": "alice",
                "otp": sent.get_json()["debug_otp"],
                "purpose": "transfer",
                "fromAccount": "10",
                "toAccount": "20",
                "amount": "750",
            },
        )
        token = verified.get_json()["confirmation_token"]
        resp = self.client.post(
            "/fundTransfer",
            json={
                "userid": "alice",
                "fromAccount": "10",
                "toAccount": "20",
                "amount": "900",
                "confirmation_token": token,
            },
        )
        self.assertEqual(resp.get_json()["error"], "step_up_mismatch")

    def test_wrong_otp(self):
        self._login()
        self._challenge()
        resp = self.client.post(
            "/verifyOTP",
            json={"userid": "alice", "otp": "000000", "purpose": "transfer", "amount": "750", "fromAccount": "10", "toAccount": "20"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_cooldown(self):
        self._login()
        self.assertEqual(self._challenge().status_code, 200)
        again = self._challenge()
        self.assertEqual(again.status_code, 429)
        self.assertEqual(again.get_json()["error"], "step_up_cooldown")

    def test_send_otp_requires_session_for_transfer(self):
        resp = self.client.post(
            "/sendOTP",
            json={"userid": "alice", "purpose": "transfer", "fromAccount": "10", "toAccount": "20", "amount": "750"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_password_reset_send_otp_no_session(self):
        resp = self.client.post("/sendOTP", json={"userid": "alice", "requester": "Customer"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["message"], "OTP Sent")

    def test_employee_high_value_skipped(self):
        self._login(usertype="tier1")
        resp = self.client.post(
            "/fundTransfer",
            json={"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "7500"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_withdraw_and_cheque_gated(self):
        self._login()
        withdraw = self.client.post(
            "/withdrawAmount", json={"userid": "alice", "account": "10", "amount": "750"}
        )
        self.assertEqual(withdraw.status_code, 403)
        cheque = self.client.post(
            "/getCashierCheque",
            json={"userid": "alice", "from_account": "10", "to_account": "20", "amount": "750"},
        )
        self.assertEqual(cheque.status_code, 403)

    def test_deposit_ungated(self):
        self._login()
        resp = self.client.post(
            "/depositAmount", json={"userid": "alice", "account": "10", "amount": "7500"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_approve_always_requires_token(self):
        self._login()
        denied = self.client.post(
            "/approveRequest", json={"customer_id": "alice", "transaction_no": "44"}
        )
        self.assertEqual(denied.status_code, 403)
        sent = self.client.post(
            "/sendOTP",
            json={"userid": "alice", "purpose": "approve", "transaction_no": "44"},
        )
        verified = self.client.post(
            "/verifyOTP",
            json={
                "userid": "alice",
                "otp": sent.get_json()["debug_otp"],
                "purpose": "approve",
                "transaction_no": "44",
            },
        )
        token = verified.get_json()["confirmation_token"]
        ok = self.client.post(
            "/approveRequest",
            json={"customer_id": "alice", "transaction_no": "44", "confirmation_token": token},
        )
        self.assertEqual(ok.status_code, 200)

    def test_unauthenticated_transfer(self):
        resp = self.client.post(
            "/fundTransfer",
            json={"userid": "alice", "fromAccount": "10", "toAccount": "20", "amount": "10"},
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
