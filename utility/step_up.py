"""Reusable step-up confirmation for sensitive operations.

Login MFA (PR #8) is a different concern: this module issues a
purpose-bound, fingerprint-bound, single-use confirmation token after
a fresh OTP. Money-moving routes consume that token when the amount
meets the policy threshold.

Local HMAC OTP is used when Twilio credentials are placeholders so
tests and local runs work. Production should set real TWILIO_* values
(STEP_UP_PROVIDER=auto).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PURPOSE_TRANSFER = "transfer"
PURPOSE_WITHDRAW = "withdraw"
PURPOSE_CHEQUE = "cheque"
PURPOSE_APPROVE = "approve"
PURPOSE_PASSWORD_RESET = "password_reset"

MONEY_PURPOSES = (
    PURPOSE_TRANSFER,
    PURPOSE_WITHDRAW,
    PURPOSE_CHEQUE,
    PURPOSE_APPROVE,
)

_MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class InvalidAmount(ValueError):
    pass


def parse_money(raw: Any) -> Decimal:
    """Strict positive 2-dp money. Rejects junk, signs, and scientific notation."""
    if isinstance(raw, bool) or raw is None:
        raise InvalidAmount("invalid_amount")
    if isinstance(raw, int):
        text = str(raw)
    elif isinstance(raw, float):
        text = format(raw, "f").rstrip("0").rstrip(".") if "." in format(raw, "f") else format(raw, "f")
        if "." in text:
            whole, frac = text.split(".", 1)
            if len(frac) > 2:
                text = "{:.2f}".format(raw)
    else:
        text = str(raw).strip()
    if not text or not _MONEY_RE.fullmatch(text):
        raise InvalidAmount("invalid_amount")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidAmount("invalid_amount") from exc
    if amount <= 0:
        raise InvalidAmount("invalid_amount")
    return amount.quantize(Decimal("0.01"))


def try_parse_money(raw: Any) -> Optional[Decimal]:
    try:
        return parse_money(raw)
    except InvalidAmount:
        return None


def operation_fingerprint(purpose: str, **fields: Any) -> str:
    """Canonical fingerprint so a token cannot be replayed onto a different op."""
    payload = {"purpose": purpose}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key == "amount":
            parsed = try_parse_money(value)
            payload[key] = str(parsed) if parsed is not None else str(value)
        else:
            payload[key] = str(value).strip()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def fingerprint_from_values(purpose: str, values: Dict[str, Any], amount: Optional[Any] = None) -> str:
    values = values or {}
    if amount is None:
        amount = values.get("amount")
    if purpose == PURPOSE_TRANSFER:
        return operation_fingerprint(
            purpose,
            from_account=values.get("fromAccount") or values.get("from_account"),
            to_account=values.get("toAccount") or values.get("to_account"),
            amount=amount,
        )
    if purpose == PURPOSE_WITHDRAW:
        return operation_fingerprint(
            purpose,
            account=values.get("account"),
            amount=amount,
        )
    if purpose == PURPOSE_CHEQUE:
        return operation_fingerprint(
            purpose,
            from_account=values.get("from_account") or values.get("fromAccount"),
            to_account=values.get("to_account") or values.get("toAccount"),
            amount=amount,
        )
    if purpose == PURPOSE_APPROVE:
        return operation_fingerprint(
            purpose,
            transaction_no=values.get("transaction_no"),
        )
    return operation_fingerprint(purpose)


def infer_purpose(values: Optional[Dict[str, Any]]) -> str:
    values = values or {}
    raw = str(values.get("purpose") or "").strip().lower()
    if raw in MONEY_PURPOSES or raw == PURPOSE_PASSWORD_RESET:
        return raw
    if values.get("transaction_no") not in (None, ""):
        return PURPOSE_APPROVE
    if values.get("from_account") not in (None, "") or values.get("to_account") not in (None, ""):
        return PURPOSE_CHEQUE
    if values.get("fromAccount") not in (None, "") or values.get("toAccount") not in (None, ""):
        return PURPOSE_TRANSFER
    if values.get("account") not in (None, "") and values.get("amount") not in (None, ""):
        return PURPOSE_WITHDRAW
    return PURPOSE_PASSWORD_RESET


@dataclass
class StepUpPolicy:
    enabled: bool = True
    threshold: Decimal = Decimal("500.00")
    approve_always: bool = True
    customer_only: bool = True
    ttl_seconds: int = 300
    token_ttl_seconds: int = 120
    cooldown_seconds: int = 45
    max_attempts: int = 5

    @classmethod
    def from_env(cls) -> "StepUpPolicy":
        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            return int(raw)

        threshold = Decimal(os.getenv("STEP_UP_THRESHOLD", "500"))
        return cls(
            enabled=_bool("STEP_UP_ENABLED", True),
            threshold=threshold.quantize(Decimal("0.01")),
            approve_always=_bool("STEP_UP_APPROVE_ALWAYS", True),
            customer_only=_bool("STEP_UP_CUSTOMER_ONLY", True),
            ttl_seconds=_int("STEP_UP_TTL_SECONDS", 300),
            token_ttl_seconds=_int("STEP_UP_TOKEN_TTL_SECONDS", 120),
            cooldown_seconds=_int("STEP_UP_COOLDOWN_SECONDS", 45),
            max_attempts=_int("STEP_UP_MAX_ATTEMPTS", 5),
        )

    def requires(self, *, usertype: Optional[str], purpose: str, amount: Optional[Decimal]) -> bool:
        if not self.enabled:
            return False
        if self.customer_only and (usertype or "") != "customer":
            return False
        if purpose == PURPOSE_APPROVE:
            if self.approve_always:
                return True
            return amount is not None and amount >= self.threshold
        if purpose in MONEY_PURPOSES:
            return amount is not None and amount >= self.threshold
        return False

    def public_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "threshold": str(self.threshold),
            "approve_always": self.approve_always,
            "operations": list(MONEY_PURPOSES),
        }


@dataclass
class HttpResult:
    body: Dict[str, Any]
    status: int
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Challenge:
    challenge_id: str
    userid: str
    purpose: str
    fingerprint: str
    phone: str
    provider: str
    code_hash: Optional[str]
    salt: Optional[str]
    expires_at: float
    sent_at: float
    attempts: int
    max_attempts: int
    consumed: bool = False


@dataclass
class ConfirmationToken:
    token: str
    userid: str
    purpose: str
    fingerprint: str
    expires_at: float
    used: bool = False


class StepUpStore:
    def save_challenge(self, challenge: Challenge) -> None:
        raise NotImplementedError

    def latest_open_challenge(self, userid: str, purpose: str) -> Optional[Challenge]:
        raise NotImplementedError

    def get_challenge(self, challenge_id: str) -> Optional[Challenge]:
        raise NotImplementedError

    def update_challenge(self, challenge: Challenge) -> None:
        raise NotImplementedError

    def last_sent_at(self, userid: str, purpose: str) -> Optional[float]:
        raise NotImplementedError

    def save_token(self, token: ConfirmationToken) -> None:
        raise NotImplementedError

    def get_token(self, token: str) -> Optional[ConfirmationToken]:
        raise NotImplementedError

    def update_token(self, token: ConfirmationToken) -> None:
        raise NotImplementedError


class MemoryStepUpStore(StepUpStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.challenges: Dict[str, Challenge] = {}
        self.tokens: Dict[str, ConfirmationToken] = {}

    def save_challenge(self, challenge: Challenge) -> None:
        with self._lock:
            self.challenges[challenge.challenge_id] = challenge

    def latest_open_challenge(self, userid: str, purpose: str) -> Optional[Challenge]:
        with self._lock:
            open_ones = [
                c
                for c in self.challenges.values()
                if c.userid == userid and c.purpose == purpose and not c.consumed
            ]
        if not open_ones:
            return None
        return max(open_ones, key=lambda c: c.sent_at)

    def get_challenge(self, challenge_id: str) -> Optional[Challenge]:
        with self._lock:
            return self.challenges.get(challenge_id)

    def update_challenge(self, challenge: Challenge) -> None:
        with self._lock:
            self.challenges[challenge.challenge_id] = challenge

    def last_sent_at(self, userid: str, purpose: str) -> Optional[float]:
        with self._lock:
            times = [
                c.sent_at
                for c in self.challenges.values()
                if c.userid == userid and c.purpose == purpose
            ]
        return max(times) if times else None

    def save_token(self, token: ConfirmationToken) -> None:
        with self._lock:
            self.tokens[token.token] = token

    def get_token(self, token: str) -> Optional[ConfirmationToken]:
        with self._lock:
            return self.tokens.get(token)

    def update_token(self, token: ConfirmationToken) -> None:
        with self._lock:
            self.tokens[token.token] = token


class SqliteStepUpStore(StepUpStore):
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS challenges (
                    challenge_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    code_hash TEXT,
                    salt TEXT,
                    expires_at REAL NOT NULL,
                    sent_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    consumed INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_challenges_user_purpose ON challenges(userid, purpose, sent_at)"
            )

    def save_challenge(self, challenge: Challenge) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO challenges(
                    challenge_id, userid, purpose, fingerprint, phone, provider,
                    code_hash, salt, expires_at, sent_at, attempts, max_attempts, consumed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    challenge.challenge_id,
                    challenge.userid,
                    challenge.purpose,
                    challenge.fingerprint,
                    challenge.phone,
                    challenge.provider,
                    challenge.code_hash,
                    challenge.salt,
                    challenge.expires_at,
                    challenge.sent_at,
                    challenge.attempts,
                    challenge.max_attempts,
                    1 if challenge.consumed else 0,
                ),
            )

    def latest_open_challenge(self, userid: str, purpose: str) -> Optional[Challenge]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM challenges
                WHERE userid = ? AND purpose = ? AND consumed = 0
                ORDER BY sent_at DESC LIMIT 1
                """,
                (userid, purpose),
            ).fetchone()
        return self._challenge_from_row(row) if row else None

    def get_challenge(self, challenge_id: str) -> Optional[Challenge]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return self._challenge_from_row(row) if row else None

    def update_challenge(self, challenge: Challenge) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE challenges SET attempts = ?, consumed = ?
                WHERE challenge_id = ?
                """,
                (challenge.attempts, 1 if challenge.consumed else 0, challenge.challenge_id),
            )

    def last_sent_at(self, userid: str, purpose: str) -> Optional[float]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(sent_at) AS sent_at FROM challenges WHERE userid = ? AND purpose = ?",
                (userid, purpose),
            ).fetchone()
        if not row or row["sent_at"] is None:
            return None
        return float(row["sent_at"])

    def save_token(self, token: ConfirmationToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tokens(token, userid, purpose, fingerprint, expires_at, used)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token.token,
                    token.userid,
                    token.purpose,
                    token.fingerprint,
                    token.expires_at,
                    1 if token.used else 0,
                ),
            )

    def get_token(self, token: str) -> Optional[ConfirmationToken]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
        return self._token_from_row(row) if row else None

    def update_token(self, token: ConfirmationToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tokens SET used = ? WHERE token = ?",
                (1 if token.used else 0, token.token),
            )

    @staticmethod
    def _challenge_from_row(row: sqlite3.Row) -> Challenge:
        return Challenge(
            challenge_id=row["challenge_id"],
            userid=row["userid"],
            purpose=row["purpose"],
            fingerprint=row["fingerprint"],
            phone=row["phone"],
            provider=row["provider"],
            code_hash=row["code_hash"],
            salt=row["salt"],
            expires_at=float(row["expires_at"]),
            sent_at=float(row["sent_at"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            consumed=bool(row["consumed"]),
        )

    @staticmethod
    def _token_from_row(row: sqlite3.Row) -> ConfirmationToken:
        return ConfirmationToken(
            token=row["token"],
            userid=row["userid"],
            purpose=row["purpose"],
            fingerprint=row["fingerprint"],
            expires_at=float(row["expires_at"]),
            used=bool(row["used"]),
        )


class OtpProvider:
    name = "base"

    def send(self, phone: str, purpose: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (code_plain_or_None, salt_or_None). Local stores a hash of the code."""
        raise NotImplementedError

    def check(self, challenge: Challenge, code: str) -> bool:
        raise NotImplementedError


class LocalOtpProvider(OtpProvider):
    name = "local"

    def __init__(self, echo: bool = False) -> None:
        self.echo = echo
        self.last_code: Optional[str] = None

    def send(self, phone: str, purpose: str) -> Tuple[Optional[str], Optional[str]]:
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        self.last_code = code if self.echo else None
        return code, salt

    def check(self, challenge: Challenge, code: str) -> bool:
        if not challenge.code_hash or not challenge.salt:
            return False
        digest = _hash_code(code, challenge.salt)
        return hmac.compare_digest(digest, challenge.code_hash)


class TwilioOtpProvider(OtpProvider):
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, verify_sid: str) -> None:
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self._verify_sid = verify_sid

    def send(self, phone: str, purpose: str) -> Tuple[Optional[str], Optional[str]]:
        self._client.verify.v2.services(self._verify_sid).verifications.create(
            to=phone, channel="sms"
        )
        return None, None

    def check(self, challenge: Challenge, code: str) -> bool:
        result = self._client.verify.v2.services(self._verify_sid).verification_checks.create(
            to=challenge.phone, code=code
        )
        return getattr(result, "status", "") == "approved"


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256((salt + code).encode("utf-8")).hexdigest()


def twilio_creds_are_placeholders(sid: Optional[str], token: Optional[str], verify: Optional[str]) -> bool:
    if not sid or not token or not verify:
        return True
    blob = f"{sid} {token} {verify}".lower()
    return any(marker in blob for marker in ("your_", "your ", "placeholder", "changeme", "xxx"))


def build_otp_provider(echo_local: bool = False) -> OtpProvider:
    mode = (os.getenv("STEP_UP_PROVIDER") or "auto").strip().lower()
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    verify = os.getenv("TWILIO_VERIFY_SID")
    use_local = mode == "local" or (
        mode == "auto" and twilio_creds_are_placeholders(sid, token, verify)
    )
    if use_local:
        return LocalOtpProvider(echo=echo_local)
    return TwilioOtpProvider(sid, token, verify)


class StepUpService:
    def __init__(
        self,
        store: StepUpStore,
        policy: Optional[StepUpPolicy] = None,
        provider: Optional[OtpProvider] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store
        self.policy = policy or StepUpPolicy()
        self.provider = provider or LocalOtpProvider(echo=True)
        self._now = now or time.time

    @classmethod
    def from_env(cls) -> "StepUpService":
        store_kind = (os.getenv("STEP_UP_STORE") or "sqlite").strip().lower()
        if store_kind == "memory":
            store: StepUpStore = MemoryStepUpStore()
        else:
            path = os.getenv("STEP_UP_DB", os.path.join("SystemLogs", "step_up.sqlite"))
            store = SqliteStepUpStore(path)
        echo = (os.getenv("STEP_UP_ECHO_OTP") or "").strip().lower() in ("1", "true", "yes")
        return cls(store=store, policy=StepUpPolicy.from_env(), provider=build_otp_provider(echo_local=echo))

    def start_challenge(
        self,
        *,
        userid: str,
        purpose: str,
        phone: str,
        fingerprint: str,
    ) -> HttpResult:
        if purpose not in MONEY_PURPOSES:
            return HttpResult({"message": "Unsupported purpose", "error": "invalid_purpose"}, 400)
        if not phone:
            return HttpResult({"message": "No phone number available", "error": "no_phone"}, 400)

        now = self._now()
        last = self.store.last_sent_at(userid, purpose)
        if last is not None:
            wait = self.policy.cooldown_seconds - (now - last)
            if wait > 0:
                retry = int(wait) + 1
                return HttpResult(
                    {
                        "message": "Please wait before requesting another OTP",
                        "error": "step_up_cooldown",
                        "retry_after": retry,
                    },
                    429,
                    {"Retry-After": str(retry)},
                )

        code, salt = self.provider.send(phone, purpose)
        code_hash = _hash_code(code, salt) if code and salt else None
        challenge = Challenge(
            challenge_id=str(uuid.uuid4()),
            userid=userid,
            purpose=purpose,
            fingerprint=fingerprint,
            phone=phone,
            provider=self.provider.name,
            code_hash=code_hash,
            salt=salt,
            expires_at=now + self.policy.ttl_seconds,
            sent_at=now,
            attempts=0,
            max_attempts=self.policy.max_attempts,
        )
        self.store.save_challenge(challenge)
        logger.info("step-up challenge started user=%s purpose=%s", userid, purpose)
        body: Dict[str, Any] = {"message": "OTP Sent"}
        if isinstance(self.provider, LocalOtpProvider) and self.provider.echo and code:
            body["debug_otp"] = code
        return HttpResult(body, 200)

    def verify_challenge(
        self,
        *,
        userid: str,
        purpose: str,
        code: str,
    ) -> HttpResult:
        if not code or not str(code).strip():
            return HttpResult({"message": "OTP code is required", "error": "otp_required"}, 400)
        challenge = self.store.latest_open_challenge(userid, purpose)
        if challenge is None:
            return HttpResult({"message": "OTP mismatched!", "error": "step_up_no_challenge"}, 401)
        now = self._now()
        if now > challenge.expires_at:
            challenge.consumed = True
            self.store.update_challenge(challenge)
            return HttpResult({"message": "OTP expired", "error": "step_up_expired"}, 401)
        if challenge.attempts >= challenge.max_attempts:
            return HttpResult({"message": "Too many OTP attempts", "error": "step_up_locked"}, 403)

        challenge.attempts += 1
        ok = self.provider.check(challenge, str(code).strip())
        if not ok:
            self.store.update_challenge(challenge)
            logger.info("step-up OTP rejected user=%s purpose=%s", userid, purpose)
            return HttpResult({"message": "OTP mismatched!", "error": "step_up_invalid"}, 401)

        challenge.consumed = True
        self.store.update_challenge(challenge)
        token = ConfirmationToken(
            token=secrets.token_urlsafe(32),
            userid=userid,
            purpose=purpose,
            fingerprint=challenge.fingerprint,
            expires_at=now + self.policy.token_ttl_seconds,
        )
        self.store.save_token(token)
        logger.info("step-up OTP verified user=%s purpose=%s", userid, purpose)
        return HttpResult(
            {"message": "verified", "confirmation_token": token.token},
            200,
        )

    def consume_token(
        self,
        token_value: str,
        *,
        userid: str,
        purpose: str,
        fingerprint: str,
    ) -> HttpResult:
        record = self.store.get_token(token_value)
        if record is None:
            return HttpResult(
                {"message": "OTP confirmation is invalid", "error": "step_up_invalid"},
                403,
            )
        now = self._now()
        if record.used:
            return HttpResult(
                {"message": "OTP confirmation already used", "error": "step_up_used"},
                403,
            )
        if now > record.expires_at:
            record.used = True
            self.store.update_token(record)
            return HttpResult(
                {"message": "OTP confirmation expired", "error": "step_up_expired"},
                403,
            )
        if record.userid != userid or record.purpose != purpose:
            return HttpResult(
                {"message": "OTP confirmation does not match this request", "error": "step_up_mismatch"},
                403,
            )
        if not hmac.compare_digest(record.fingerprint, fingerprint):
            return HttpResult(
                {"message": "OTP confirmation does not match this request", "error": "step_up_mismatch"},
                403,
            )
        record.used = True
        self.store.update_token(record)
        return HttpResult({"message": "ok"}, 200)


def enforce_step_up(
    values: Optional[Dict[str, Any]],
    *,
    userid: str,
    usertype: Optional[str],
    purpose: str,
    amount_raw: Any = None,
    service: Optional[StepUpService] = None,
) -> Optional[HttpResult]:
    """Return an error result if this call needs a valid confirmation token."""
    service = service or get_step_up_service()
    amount = try_parse_money(amount_raw if amount_raw is not None else (values or {}).get("amount"))
    if not service.policy.requires(usertype=usertype, purpose=purpose, amount=amount):
        return None
    token = (values or {}).get("confirmation_token") or (values or {}).get("step_up_token")
    if not token:
        return HttpResult(
            {
                "message": "OTP confirmation required for this amount",
                "error": "step_up_required",
                "threshold": str(service.policy.threshold),
            },
            403,
        )
    fingerprint = fingerprint_from_values(purpose, values or {}, amount=amount)
    result = service.consume_token(str(token), userid=userid, purpose=purpose, fingerprint=fingerprint)
    if result.status == 200:
        return None
    result.body.setdefault("threshold", str(service.policy.threshold))
    return result


_SERVICE: Optional[StepUpService] = None


def get_step_up_service() -> StepUpService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = StepUpService.from_env()
    return _SERVICE


def reset_step_up_service(service: Optional[StepUpService] = None) -> None:
    global _SERVICE
    _SERVICE = service


def policy_public_dict(service: Optional[StepUpService] = None) -> Dict[str, Any]:
    service = service or get_step_up_service()
    return service.policy.public_dict()
