import os
import tempfile
import time
import unittest
from decimal import Decimal

from utility.step_up import (
    PURPOSE_APPROVE,
    PURPOSE_CHEQUE,
    PURPOSE_TRANSFER,
    PURPOSE_WITHDRAW,
    InvalidAmount,
    LocalOtpProvider,
    MemoryStepUpStore,
    SqliteStepUpStore,
    StepUpPolicy,
    StepUpService,
    fingerprint_from_values,
    infer_purpose,
    operation_fingerprint,
    parse_money,
    try_parse_money,
    twilio_creds_are_placeholders,
)


class ParseMoneyTests(unittest.TestCase):
    def test_accepts_int_and_two_dp(self):
        self.assertEqual(parse_money(500), Decimal("500.00"))
        self.assertEqual(parse_money("500.5"), Decimal("500.50"))
        self.assertEqual(parse_money("12.34"), Decimal("12.34"))

    def test_rejects_junk(self):
        for raw in (None, "", "abc", "-1", "0", "+5", "1e3", "10.123", "  ", True, "00.001"):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidAmount):
                    parse_money(raw)
                self.assertIsNone(try_parse_money(raw))


class FingerprintTests(unittest.TestCase):
    def test_stable_and_amount_normalized(self):
        a = operation_fingerprint(PURPOSE_TRANSFER, from_account="1", to_account="2", amount="10")
        b = operation_fingerprint(PURPOSE_TRANSFER, to_account="2", from_account="1", amount="10.00")
        self.assertEqual(a, b)

    def test_different_ops_differ(self):
        transfer = fingerprint_from_values(
            PURPOSE_TRANSFER, {"fromAccount": "1", "toAccount": "2", "amount": "500"}
        )
        cheque = fingerprint_from_values(
            PURPOSE_CHEQUE, {"from_account": "1", "to_account": "2", "amount": "500"}
        )
        self.assertNotEqual(transfer, cheque)

    def test_infer_purpose(self):
        self.assertEqual(infer_purpose({"fromAccount": "1", "amount": "2"}), PURPOSE_TRANSFER)
        self.assertEqual(infer_purpose({"transaction_no": 9}), PURPOSE_APPROVE)
        self.assertEqual(infer_purpose({"userid": "c1"}), "password_reset")
        self.assertEqual(infer_purpose({"purpose": "withdraw", "account": "1"}), PURPOSE_WITHDRAW)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = StepUpPolicy(threshold=Decimal("500.00"))

    def test_customer_at_and_above_threshold(self):
        self.assertFalse(self.policy.requires(usertype="customer", purpose=PURPOSE_TRANSFER, amount=Decimal("499.99")))
        self.assertTrue(self.policy.requires(usertype="customer", purpose=PURPOSE_TRANSFER, amount=Decimal("500.00")))
        self.assertTrue(self.policy.requires(usertype="customer", purpose=PURPOSE_TRANSFER, amount=Decimal("5000")))

    def test_employee_skipped(self):
        self.assertFalse(self.policy.requires(usertype="tier1", purpose=PURPOSE_TRANSFER, amount=Decimal("5000")))
        self.assertFalse(self.policy.requires(usertype="admin", purpose=PURPOSE_WITHDRAW, amount=Decimal("5000")))

    def test_approve_always(self):
        self.assertTrue(self.policy.requires(usertype="customer", purpose=PURPOSE_APPROVE, amount=Decimal("1.00")))
        self.policy.approve_always = False
        self.assertFalse(self.policy.requires(usertype="customer", purpose=PURPOSE_APPROVE, amount=Decimal("1.00")))

    def test_disabled(self):
        self.policy.enabled = False
        self.assertFalse(self.policy.requires(usertype="customer", purpose=PURPOSE_TRANSFER, amount=Decimal("9000")))

    def test_unparseable_amount_does_not_require(self):
        self.assertFalse(self.policy.requires(usertype="customer", purpose=PURPOSE_TRANSFER, amount=None))


class StepUpServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1_000.0]
        self.provider = LocalOtpProvider(echo=True)
        self.service = StepUpService(
            store=MemoryStepUpStore(),
            policy=StepUpPolicy(threshold=Decimal("500.00"), cooldown_seconds=45, ttl_seconds=300, token_ttl_seconds=120),
            provider=self.provider,
            now=lambda: self.clock[0],
        )
        self.fp = operation_fingerprint(PURPOSE_TRANSFER, from_account="10", to_account="20", amount="750")

    def _send(self):
        return self.service.start_challenge(
            userid="alice", purpose=PURPOSE_TRANSFER, phone="+15551212", fingerprint=self.fp
        )

    def test_happy_path_token_then_consume(self):
        sent = self._send()
        self.assertEqual(sent.status, 200)
        self.assertEqual(sent.body["message"], "OTP Sent")
        code = sent.body["debug_otp"]
        verified = self.service.verify_challenge(userid="alice", purpose=PURPOSE_TRANSFER, code=code)
        self.assertEqual(verified.status, 200)
        self.assertEqual(verified.body["message"], "verified")
        token = verified.body["confirmation_token"]
        consumed = self.service.consume_token(
            token, userid="alice", purpose=PURPOSE_TRANSFER, fingerprint=self.fp
        )
        self.assertEqual(consumed.status, 200)
        replay = self.service.consume_token(
            token, userid="alice", purpose=PURPOSE_TRANSFER, fingerprint=self.fp
        )
        self.assertEqual(replay.status, 403)
        self.assertEqual(replay.body["error"], "step_up_used")

    def test_wrong_code_then_lock(self):
        self._send()
        for _ in range(5):
            result = self.service.verify_challenge(userid="alice", purpose=PURPOSE_TRANSFER, code="000000")
            self.assertIn(result.status, (401, 403))
        locked = self.service.verify_challenge(userid="alice", purpose=PURPOSE_TRANSFER, code="000000")
        self.assertEqual(locked.status, 403)
        self.assertEqual(locked.body["error"], "step_up_locked")

    def test_purpose_isolation(self):
        sent = self._send()
        code = sent.body["debug_otp"]
        other = self.service.verify_challenge(userid="alice", purpose=PURPOSE_WITHDRAW, code=code)
        self.assertEqual(other.status, 401)
        ok = self.service.verify_challenge(userid="alice", purpose=PURPOSE_TRANSFER, code=code)
        self.assertEqual(ok.status, 200)

    def test_fingerprint_mismatch(self):
        sent = self._send()
        verified = self.service.verify_challenge(
            userid="alice", purpose=PURPOSE_TRANSFER, code=sent.body["debug_otp"]
        )
        token = verified.body["confirmation_token"]
        other_fp = operation_fingerprint(PURPOSE_TRANSFER, from_account="10", to_account="99", amount="750")
        mismatch = self.service.consume_token(
            token, userid="alice", purpose=PURPOSE_TRANSFER, fingerprint=other_fp
        )
        self.assertEqual(mismatch.body["error"], "step_up_mismatch")

    def test_cooldown(self):
        self.assertEqual(self._send().status, 200)
        again = self._send()
        self.assertEqual(again.status, 429)
        self.assertEqual(again.body["error"], "step_up_cooldown")
        self.assertIn("Retry-After", again.headers)
        self.clock[0] += 46
        self.assertEqual(self._send().status, 200)

    def test_expired_challenge(self):
        sent = self._send()
        self.clock[0] += 301
        expired = self.service.verify_challenge(
            userid="alice", purpose=PURPOSE_TRANSFER, code=sent.body["debug_otp"]
        )
        self.assertEqual(expired.body["error"], "step_up_expired")

    def test_expired_token(self):
        sent = self._send()
        verified = self.service.verify_challenge(
            userid="alice", purpose=PURPOSE_TRANSFER, code=sent.body["debug_otp"]
        )
        self.clock[0] += 121
        expired = self.service.consume_token(
            verified.body["confirmation_token"],
            userid="alice",
            purpose=PURPOSE_TRANSFER,
            fingerprint=self.fp,
        )
        self.assertEqual(expired.body["error"], "step_up_expired")

    def test_user_isolation(self):
        sent = self._send()
        other = self.service.verify_challenge(
            userid="bob", purpose=PURPOSE_TRANSFER, code=sent.body["debug_otp"]
        )
        self.assertEqual(other.status, 401)


class SqlitePersistenceTests(unittest.TestCase):
    def test_survives_new_service_instance(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.remove(path)
        try:
            provider = LocalOtpProvider(echo=True)
            first = StepUpService(
                store=SqliteStepUpStore(path),
                policy=StepUpPolicy(),
                provider=provider,
            )
            fp = operation_fingerprint(PURPOSE_TRANSFER, from_account="1", to_account="2", amount="900")
            sent = first.start_challenge(userid="alice", purpose=PURPOSE_TRANSFER, phone="+1", fingerprint=fp)
            code = sent.body["debug_otp"]
            second = StepUpService(
                store=SqliteStepUpStore(path),
                policy=StepUpPolicy(),
                provider=provider,
            )
            verified = second.verify_challenge(userid="alice", purpose=PURPOSE_TRANSFER, code=code)
            self.assertEqual(verified.status, 200)
            third = StepUpService(
                store=SqliteStepUpStore(path),
                policy=StepUpPolicy(),
                provider=LocalOtpProvider(echo=True),
            )
            consumed = third.consume_token(
                verified.body["confirmation_token"],
                userid="alice",
                purpose=PURPOSE_TRANSFER,
                fingerprint=fp,
            )
            self.assertEqual(consumed.status, 200)
        finally:
            if os.path.exists(path):
                os.remove(path)


class PlaceholderCredsTests(unittest.TestCase):
    def test_detects_placeholders(self):
        self.assertTrue(twilio_creds_are_placeholders("your_account_sid", "your_auth_token", "your_verify_sid"))
        self.assertFalse(twilio_creds_are_placeholders("ACabc123", "secret-token", "VA123"))


if __name__ == "__main__":
    unittest.main()
