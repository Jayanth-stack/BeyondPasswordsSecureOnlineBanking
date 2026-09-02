"""Queryable, append-only audit trail for security-relevant banking events.

This is the reusable logging capability the rest of the app should emit into
and that admins query. It is intentionally not a MySQL table: create_database.py
drops the whole schema, and PR #8 already claimed a file-based SystemLogs/audit.log.

Stores:
- MemoryAuditStore — tests
- SqliteAuditStore — restart-safe default (SystemLogs/audit_trail.sqlite)

Recording never raises into the banking path. Querying surfaces store errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

LOGGER = logging.getLogger("bank.audit_trail")

REDACT_VALUE = "[REDACTED]"
SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "otp",
    "ssn",
    "token",
    "secret",
    "cookie",
    "authorization",
    "hashed",
    "auth_token",
    "account_sid",
)
PII_KEYS = {
    "phone",
    "contact",
    "contact_no",
    "email",
    "email_id",
    "address",
    "dob",
    "newpassword",
    "oldpassword",
}

ACTIONS = (
    "login",
    "otp_verify",
    "logout",
    "password_reset",
    "send_otp",
    "fund_transfer",
    "withdraw",
    "deposit",
    "cheque_issue",
    "approve_transfer",
    "deny_transfer",
    "deactivate_account",
    "deactivate_customer",
    "deactivate_employee",
    "open_account",
    "audit_query",
)
OUTCOMES = ("success", "denied", "invalid", "failure")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_DB_PATH = os.path.join("SystemLogs", "audit_trail.sqlite")

_SENSITIVE_VALUE_RE = re.compile(
    r"(password|otp|ssn|secret|token)\s*[:=]",
    re.IGNORECASE,
)

_default_trail = None
_default_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_event_id() -> str:
    return uuid.uuid4().hex


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_secret_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return any(marker.replace("_", "") in normalized for marker in SECRET_KEY_MARKERS)


def redact(value: Any) -> Any:
    """Recursively strip secrets and obvious PII from structures that may be stored."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "").replace("_", "")
            if _is_secret_key(key) or lowered in PII_KEYS:
                cleaned[key] = REDACT_VALUE
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        return REDACT_VALUE
    return value


def outcome_from_status(status_code: int) -> str:
    if status_code < 300:
        return "success"
    if status_code in (401, 403):
        return "denied"
    if status_code < 500:
        return "invalid"
    return "failure"


def client_ip(headers: Optional[Dict[str, str]] = None, remote_addr: Optional[str] = None) -> Optional[str]:
    if headers:
        forwarded = headers.get("X-Forwarded-For") or headers.get("X-Forwarded-For".lower())
        if not forwarded:
            # Flask header map is case-insensitive; plain dicts are not.
            for key, value in headers.items():
                if str(key).lower() == "x-forwarded-for":
                    forwarded = value
                    break
        if forwarded:
            return str(forwarded).split(",")[0].strip()[:64]
    if remote_addr:
        return str(remote_addr)[:64]
    return None


@dataclass
class AuditEvent:
    event_id: str
    ts: str
    action: str
    outcome: str
    actor_id: Optional[str] = None
    actor_type: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    ip: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["details"] = dict(self.details or {})
        return payload

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AuditEvent":
        details = row.get("details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (TypeError, ValueError):
                details = {"raw": details}
        return cls(
            event_id=row["event_id"],
            ts=row["ts"],
            action=row["action"],
            outcome=row["outcome"],
            actor_id=row.get("actor_id"),
            actor_type=row.get("actor_type"),
            resource_type=row.get("resource_type"),
            resource_id=row.get("resource_id"),
            ip=row.get("ip"),
            details=details if isinstance(details, dict) else {},
        )


@dataclass
class AuditFilter:
    actor_id: Optional[str] = None
    actor_type: Optional[str] = None
    action: Optional[str] = None
    outcome: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    q: Optional[str] = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    @classmethod
    def from_mapping(cls, values: Optional[Dict[str, Any]]) -> "AuditFilter":
        values = values or {}

        def _text(key: str) -> Optional[str]:
            raw = values.get(key)
            if raw is None:
                return None
            text = str(raw).strip()
            return text or None

        try:
            limit = int(values.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        try:
            offset = int(values.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        return cls(
            actor_id=_text("actor_id"),
            actor_type=_text("actor_type"),
            action=_text("action"),
            outcome=_text("outcome"),
            resource_type=_text("resource_type"),
            resource_id=_text("resource_id"),
            since=_text("since"),
            until=_text("until"),
            q=_text("q"),
            limit=limit,
            offset=offset,
        )


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None:
        ...

    def query(self, filt: AuditFilter) -> Dict[str, Any]:
        ...


class MemoryAuditStore:
    def __init__(self) -> None:
        self._events: List[AuditEvent] = []
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(self, filt: AuditFilter) -> Dict[str, Any]:
        with self._lock:
            matched = [event for event in self._events if _matches(event, filt)]
        matched.sort(key=lambda event: event.ts, reverse=True)
        total = len(matched)
        page = matched[filt.offset : filt.offset + filt.limit]
        return {"events": [event.to_dict() for event in page], "total": total}


def _contains_text(event: AuditEvent, needle: str) -> bool:
    blob = " ".join(
        [
            event.event_id,
            event.ts,
            event.action,
            event.outcome,
            event.actor_id or "",
            event.actor_type or "",
            event.resource_type or "",
            event.resource_id or "",
            event.ip or "",
            json.dumps(event.details, default=str),
        ]
    ).lower()
    return needle.lower() in blob


def _matches(event: AuditEvent, filt: AuditFilter) -> bool:
    if filt.actor_id and event.actor_id != filt.actor_id:
        return False
    if filt.actor_type and event.actor_type != filt.actor_type:
        return False
    if filt.action and event.action != filt.action:
        return False
    if filt.outcome and event.outcome != filt.outcome:
        return False
    if filt.resource_type and event.resource_type != filt.resource_type:
        return False
    if filt.resource_id and str(event.resource_id) != str(filt.resource_id):
        return False
    if filt.since and (event.ts or "") < filt.since:
        return False
    if filt.until and (event.ts or "") > filt.until:
        return False
    if filt.q and not _contains_text(event, filt.q):
        return False
    return True


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor_id TEXT,
    actor_type TEXT,
    resource_type TEXT,
    resource_id TEXT,
    ip TEXT,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
CREATE INDEX IF NOT EXISTS idx_audit_outcome ON audit_events(outcome);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_events(resource_type, resource_id);
"""


class SqliteAuditStore:
    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SQLITE_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def append(self, event: AuditEvent) -> None:
        payload = event.to_dict()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, ts, action, outcome, actor_id, actor_type,
                        resource_type, resource_id, ip, details
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["event_id"],
                        payload["ts"],
                        payload["action"],
                        payload["outcome"],
                        payload["actor_id"],
                        payload["actor_type"],
                        payload["resource_type"],
                        payload["resource_id"],
                        payload["ip"],
                        json.dumps(payload["details"], default=str),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def query(self, filt: AuditFilter) -> Dict[str, Any]:
        clauses: List[str] = []
        params: List[Any] = []

        def _eq(column: str, value: Optional[str]) -> None:
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)

        _eq("actor_id", filt.actor_id)
        _eq("actor_type", filt.actor_type)
        _eq("action", filt.action)
        _eq("outcome", filt.outcome)
        _eq("resource_type", filt.resource_type)
        _eq("resource_id", filt.resource_id)
        if filt.since:
            clauses.append("ts >= ?")
            params.append(filt.since)
        if filt.until:
            clauses.append("ts <= ?")
            params.append(filt.until)
        if filt.q:
            like = "%" + filt.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "("
                "actor_id LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' "
                "OR resource_id LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\' "
                "OR outcome LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\'"
                ")"
            )
            params.extend([like, like, like, like, like, like])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                total_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM audit_events {where}",
                    params,
                ).fetchone()
                total = int(total_row["n"]) if total_row else 0
                rows = conn.execute(
                    f"""
                    SELECT event_id, ts, action, outcome, actor_id, actor_type,
                           resource_type, resource_id, ip, details
                    FROM audit_events
                    {where}
                    ORDER BY ts DESC, event_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [filt.limit, filt.offset],
                ).fetchall()
            finally:
                conn.close()
        events = [AuditEvent.from_row(dict(row)).to_dict() for row in rows]
        return {"events": events, "total": total}


class AuditTrail:
    def __init__(self, store: Optional[AuditStore] = None) -> None:
        self.store = store or MemoryAuditStore()

    @classmethod
    def from_env(cls) -> "AuditTrail":
        kind = (os.getenv("AUDIT_STORE") or "sqlite").strip().lower()
        if kind == "memory":
            return cls(MemoryAuditStore())
        path = os.getenv("AUDIT_DB") or DEFAULT_DB_PATH
        return cls(SqliteAuditStore(path))

    def record(
        self,
        action: str,
        outcome: str,
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[Any] = None,
        ip: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Optional[Dict[str, Any]]:
        merged = dict(details or {})
        merged.update(extra)
        event = AuditEvent(
            event_id=new_event_id(),
            ts=utc_now_iso(),
            action=str(action),
            outcome=str(outcome),
            actor_id=None if actor_id is None else str(actor_id),
            actor_type=None if actor_type is None else str(actor_type),
            resource_type=None if resource_type is None else str(resource_type),
            resource_id=None if resource_id is None else str(resource_id),
            ip=None if ip is None else str(ip)[:64],
            details=redact(merged) if merged else {},
        )
        try:
            self.store.append(event)
        except Exception:
            LOGGER.exception("failed to append audit event action=%s outcome=%s", action, outcome)
            return None
        return event.to_dict()

    def query(self, filt: Optional[AuditFilter] = None) -> Dict[str, Any]:
        filt = filt or AuditFilter()
        result = self.store.query(filt)
        result["limit"] = filt.limit
        result["offset"] = filt.offset
        result["actions"] = list(ACTIONS)
        result["outcomes"] = list(OUTCOMES)
        return result


def default_trail() -> AuditTrail:
    global _default_trail
    with _default_lock:
        if _default_trail is None:
            _default_trail = AuditTrail.from_env()
        return _default_trail


def reset_default_trail(trail: Optional[AuditTrail] = None) -> None:
    global _default_trail
    with _default_lock:
        _default_trail = trail


def record_request(
    action: str,
    outcome: str,
    session_data: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    remote_addr: Optional[str] = None,
    trail: Optional[AuditTrail] = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Record an event using optional Flask-like session/request context."""
    trail = trail or default_trail()
    actor_id = kwargs.pop("actor_id", None)
    actor_type = kwargs.pop("actor_type", None)
    if session_data:
        actor_id = actor_id or session_data.get("userid")
        actor_type = actor_type or session_data.get("usertype")
    ip = kwargs.pop("ip", None) or client_ip(headers, remote_addr)
    return trail.record(
        action,
        outcome,
        actor_id=actor_id,
        actor_type=actor_type,
        ip=ip,
        **kwargs,
    )


def can_query_audit(session_data: Optional[Dict[str, Any]]) -> bool:
    session_data = session_data or {}
    return session_data.get("usertype") == "admin" and bool(session_data.get("userid"))


def handle_query_request(
    values: Optional[Dict[str, Any]],
    session_data: Optional[Dict[str, Any]],
    headers: Optional[Dict[str, str]] = None,
    remote_addr: Optional[str] = None,
    trail: Optional[AuditTrail] = None,
) -> Dict[str, Any]:
    """Authorize and run an audit query. Returns {status, body}."""
    trail = trail or default_trail()
    session_data = session_data or {}
    values = values or {}
    context = {
        "session_data": session_data,
        "headers": headers,
        "remote_addr": remote_addr,
        "trail": trail,
        "resource_type": "audit",
    }

    if not can_query_audit(session_data):
        record_request("audit_query", "denied", **context)
        return {"status": 401, "body": {"message": "Unauthorized access or session expired"}}

    if "userid" not in values:
        record_request("audit_query", "invalid", **context, details={"reason": "missing_userid"})
        return {"status": 400, "body": {"message": "Some data missing"}}

    if values.get("userid") != session_data.get("userid"):
        record_request(
            "audit_query",
            "denied",
            **context,
            details={"reason": "userid_mismatch"},
        )
        return {"status": 401, "body": {"message": "Unauthorized operation: User ID mismatch"}}

    filt = AuditFilter.from_mapping(values)
    try:
        result = trail.query(filt)
    except Exception as exc:
        LOGGER.exception("audit query failed")
        record_request("audit_query", "failure", **context, details={"error": str(exc)})
        return {"status": 500, "body": {"message": "Failed to query audit events"}}

    record_request(
        "audit_query",
        "success",
        **context,
        details={"total": result.get("total"), "action": filt.action, "limit": filt.limit},
    )
    return {"status": 200, "body": result}


def attach_audit_routes(app: Any, trail: Optional[AuditTrail] = None) -> Any:
    """Mount POST /queryAudit on a Flask app. Used by tests and app.py."""
    from flask import jsonify, request, session

    bound = trail or default_trail()

    @app.route("/queryAudit", methods=["POST", "GET"])
    def query_audit():
        values = request.get_json(silent=True) or {}
        result = handle_query_request(
            values,
            dict(session),
            headers={key: value for key, value in request.headers.items()},
            remote_addr=request.remote_addr,
            trail=bound,
        )
        return jsonify(result["body"]), result["status"]

    return app
