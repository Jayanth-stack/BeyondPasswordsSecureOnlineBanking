"""Server-side sessions, device registry, idle/absolute timeout, revocation.

Flask's default session is a signed cookie. Combined with
``app.secret_key = os.urandom(24)`` that means gunicorn workers cannot share
sessions, a process restart logs everyone out, logout cannot kill another
device, and idle timeout is not enforceable on the server.

This module is the reusable foundation those features need: a SessionStore
(memory or SQLite), a SessionPolicy, a Flask SessionInterface that puts only a
signed sid in the cookie, and a device registry keyed by (userid, device_key).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

_SERVICE: Optional['SessionService'] = None


class SessionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def resolve_secret_key(path: Optional[str] = None) -> str:
    """Stable signing key shared across gunicorn workers.

    Order: SECRET_KEY env, then a 0600 file (created once), so a missing .env
    still survives restart. Distinct from PR #12 which only read SECRET_KEY.
    """
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    path = path or os.environ.get(
        'SESSION_SECRET_FILE', os.path.join('SystemLogs', '.session_secret')
    )
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        key = os.urandom(24).hex()
        with os.fdopen(fd, 'w') as fh:
            fh.write(key)
        return key
    except FileExistsError:
        with open(path, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
        if not key:
            raise RuntimeError('session secret file is empty: %s' % path)
        return key


def client_ip(request: Any) -> str:
    """Direct peer address only. X-Forwarded-For is ignored (clients can spoof it)."""
    if request is None:
        return ''
    return (getattr(request, 'remote_addr', None) or '')[:64]


def device_label(user_agent: str) -> str:
    ua = user_agent or ''
    low = ua.lower()
    if 'edg/' in low or 'edge/' in low:
        browser = 'Edge'
    elif 'opr/' in low or 'opera' in low:
        browser = 'Opera'
    elif 'chrome' in low and 'chromium' not in low:
        browser = 'Chrome'
    elif 'firefox' in low:
        browser = 'Firefox'
    elif 'safari' in low:
        browser = 'Safari'
    elif ua:
        browser = 'Browser'
    else:
        browser = 'Unknown browser'
    if 'android' in low:
        os_name = 'Android'
    elif 'iphone' in low or 'ipad' in low or 'ios' in low:
        os_name = 'iOS'
    elif 'mac os' in low or 'macintosh' in low:
        os_name = 'macOS'
    elif 'windows' in low:
        os_name = 'Windows'
    elif 'linux' in low:
        os_name = 'Linux'
    else:
        os_name = 'unknown OS'
    return '%s on %s' % (browser, os_name)


def device_key(user_agent: str) -> str:
    """Stable-enough device id from UA family+OS. IP is not included (roaming)."""
    label = device_label(user_agent)
    digest = hashlib.sha256(label.encode('utf-8')).hexdigest()
    return digest[:16]


def new_sid() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class SessionPolicy:
    idle_seconds: int = 1800
    absolute_seconds: int = 43200
    max_concurrent: int = 3
    max_concurrent_employee: int = 5

    @classmethod
    def from_env(cls) -> 'SessionPolicy':
        return cls(
            idle_seconds=_env_int('SESSION_IDLE_SECONDS', 1800),
            absolute_seconds=_env_int('SESSION_ABSOLUTE_SECONDS', 43200),
            max_concurrent=_env_int('SESSION_MAX_CONCURRENT', 3),
            max_concurrent_employee=_env_int('SESSION_MAX_CONCURRENT_EMPLOYEE', 5),
        )

    def max_for(self, usertype: Optional[str]) -> int:
        if usertype and usertype != 'customer':
            return self.max_concurrent_employee
        return self.max_concurrent


@dataclass
class Device:
    userid: str
    device_key: str
    device_label: str
    first_seen: float
    last_seen: float
    last_ip: str = ''


@dataclass
class SessionRecord:
    sid: str
    data: Dict[str, Any] = field(default_factory=dict)
    userid: Optional[str] = None
    usertype: Optional[str] = None
    created_at: float = 0.0
    last_seen: float = 0.0
    idle_expires_at: float = 0.0
    absolute_expires_at: float = 0.0
    ip: str = ''
    user_agent: str = ''
    device_label: str = ''
    device_key: str = ''
    revoked: bool = False
    new_device: bool = False

    def as_public(self, current_sid: Optional[str] = None) -> Dict[str, Any]:
        return {
            'sid': self.sid,
            'device_label': self.device_label or 'Unknown device',
            'ip': self.ip or 'unknown',
            'created_at': _iso(self.created_at) if self.created_at else '',
            'last_seen': _iso(self.last_seen) if self.last_seen else '',
            'current': self.sid == current_sid,
            'new_device': bool(self.new_device),
        }


class SessionStore(Protocol):
    def get(self, sid: str) -> Optional[SessionRecord]: ...
    def save(self, record: SessionRecord) -> None: ...
    def delete(self, sid: str) -> None: ...
    def list_active(self, userid: str, now: float) -> List[SessionRecord]: ...
    def revoke(self, sid: str) -> Optional[SessionRecord]: ...
    def revoke_all_except(self, userid: str, keep_sid: str) -> int: ...
    def get_device(self, userid: str, key: str) -> Optional[Device]: ...
    def upsert_device(self, device: Device) -> None: ...


def _copy_record(record: SessionRecord) -> SessionRecord:
    return SessionRecord(
        sid=record.sid,
        data=dict(record.data),
        userid=record.userid,
        usertype=record.usertype,
        created_at=record.created_at,
        last_seen=record.last_seen,
        idle_expires_at=record.idle_expires_at,
        absolute_expires_at=record.absolute_expires_at,
        ip=record.ip,
        user_agent=record.user_agent,
        device_label=record.device_label,
        device_key=record.device_key,
        revoked=record.revoked,
        new_device=record.new_device,
    )


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, SessionRecord] = {}
        self._devices: Dict[Tuple[str, str], Device] = {}
        self._lock = threading.Lock()

    def get(self, sid: str) -> Optional[SessionRecord]:
        with self._lock:
            record = self._sessions.get(sid)
            return _copy_record(record) if record else None

    def save(self, record: SessionRecord) -> None:
        with self._lock:
            self._sessions[record.sid] = _copy_record(record)

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def list_active(self, userid: str, now: float) -> List[SessionRecord]:
        with self._lock:
            out = [
                _copy_record(r)
                for r in self._sessions.values()
                if r.userid == userid
                and not r.revoked
                and r.idle_expires_at > now
                and r.absolute_expires_at > now
            ]
        out.sort(key=lambda r: r.last_seen, reverse=True)
        return out

    def revoke(self, sid: str) -> Optional[SessionRecord]:
        with self._lock:
            record = self._sessions.get(sid)
            if record is None:
                return None
            record.revoked = True
            return _copy_record(record)

    def revoke_all_except(self, userid: str, keep_sid: str) -> int:
        n = 0
        with self._lock:
            for record in self._sessions.values():
                if record.userid == userid and record.sid != keep_sid and not record.revoked:
                    record.revoked = True
                    n += 1
        return n

    def get_device(self, userid: str, key: str) -> Optional[Device]:
        with self._lock:
            device = self._devices.get((userid, key))
            if device is None:
                return None
            return Device(**device.__dict__)

    def upsert_device(self, device: Device) -> None:
        with self._lock:
            self._devices[(device.userid, device.device_key)] = Device(**device.__dict__)


class SqliteSessionStore:
    """WAL sqlite under SystemLogs/. Independent of the banking MySQL schema."""

    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                userid TEXT,
                usertype TEXT,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                idle_expires_at REAL NOT NULL,
                absolute_expires_at REAL NOT NULL,
                ip TEXT,
                user_agent TEXT,
                device_label TEXT,
                device_key TEXT,
                revoked INTEGER NOT NULL DEFAULT 0,
                new_device INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_userid ON sessions(userid);
            CREATE TABLE IF NOT EXISTS devices (
                userid TEXT NOT NULL,
                device_key TEXT NOT NULL,
                device_label TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_ip TEXT,
                PRIMARY KEY (userid, device_key)
            );
            """
        )

    def _row_to_record(self, row: sqlite3.Row) -> SessionRecord:
        try:
            data = json.loads(row['data'] or '{}')
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return SessionRecord(
            sid=row['sid'],
            data=data,
            userid=row['userid'],
            usertype=row['usertype'],
            created_at=row['created_at'],
            last_seen=row['last_seen'],
            idle_expires_at=row['idle_expires_at'],
            absolute_expires_at=row['absolute_expires_at'],
            ip=row['ip'] or '',
            user_agent=row['user_agent'] or '',
            device_label=row['device_label'] or '',
            device_key=row['device_key'] or '',
            revoked=bool(row['revoked']),
            new_device=bool(row['new_device']),
        )

    def get(self, sid: str) -> Optional[SessionRecord]:
        with self._lock:
            cur = self._conn.execute('SELECT * FROM sessions WHERE sid = ?', (sid,))
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def save(self, record: SessionRecord) -> None:
        payload = json.dumps(record.data, separators=(',', ':'), default=str)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    sid, data, userid, usertype, created_at, last_seen,
                    idle_expires_at, absolute_expires_at, ip, user_agent,
                    device_label, device_key, revoked, new_device
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sid) DO UPDATE SET
                    data=excluded.data,
                    userid=excluded.userid,
                    usertype=excluded.usertype,
                    created_at=excluded.created_at,
                    last_seen=excluded.last_seen,
                    idle_expires_at=excluded.idle_expires_at,
                    absolute_expires_at=excluded.absolute_expires_at,
                    ip=excluded.ip,
                    user_agent=excluded.user_agent,
                    device_label=excluded.device_label,
                    device_key=excluded.device_key,
                    revoked=excluded.revoked,
                    new_device=excluded.new_device
                """,
                (
                    record.sid,
                    payload,
                    record.userid,
                    record.usertype,
                    record.created_at,
                    record.last_seen,
                    record.idle_expires_at,
                    record.absolute_expires_at,
                    record.ip,
                    record.user_agent,
                    record.device_label,
                    record.device_key,
                    1 if record.revoked else 0,
                    1 if record.new_device else 0,
                ),
            )

    def delete(self, sid: str) -> None:
        with self._lock:
            self._conn.execute('DELETE FROM sessions WHERE sid = ?', (sid,))

    def list_active(self, userid: str, now: float) -> List[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE userid = ? AND revoked = 0
                  AND idle_expires_at > ? AND absolute_expires_at > ?
                ORDER BY last_seen DESC
                """,
                (userid, now, now),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def revoke(self, sid: str) -> Optional[SessionRecord]:
        with self._lock:
            cur = self._conn.execute('SELECT * FROM sessions WHERE sid = ?', (sid,))
            row = cur.fetchone()
            if row is None:
                return None
            self._conn.execute('UPDATE sessions SET revoked = 1 WHERE sid = ?', (sid,))
        record = self._row_to_record(row)
        record.revoked = True
        return record

    def revoke_all_except(self, userid: str, keep_sid: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE sessions SET revoked = 1
                WHERE userid = ? AND sid != ? AND revoked = 0
                """,
                (userid, keep_sid),
            )
            return cur.rowcount or 0

    def get_device(self, userid: str, key: str) -> Optional[Device]:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM devices WHERE userid = ? AND device_key = ?',
                (userid, key),
            ).fetchone()
        if row is None:
            return None
        return Device(
            userid=row['userid'],
            device_key=row['device_key'],
            device_label=row['device_label'] or '',
            first_seen=row['first_seen'],
            last_seen=row['last_seen'],
            last_ip=row['last_ip'] or '',
        )

    def upsert_device(self, device: Device) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO devices (userid, device_key, device_label, first_seen, last_seen, last_ip)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(userid, device_key) DO UPDATE SET
                    device_label=excluded.device_label,
                    last_seen=excluded.last_seen,
                    last_ip=excluded.last_ip
                """,
                (
                    device.userid,
                    device.device_key,
                    device.device_label,
                    device.first_seen,
                    device.last_seen,
                    device.last_ip,
                ),
            )


def build_store(path: Optional[str] = None) -> SessionStore:
    path = path if path is not None else os.environ.get(
        'SESSION_STORE', os.path.join('SystemLogs', 'sessions.sqlite')
    )
    if path in ('memory', ':memory:'):
        return MemorySessionStore()
    return SqliteSessionStore(path)


class SessionService:
    def __init__(
        self,
        store: Optional[SessionStore] = None,
        policy: Optional[SessionPolicy] = None,
        clock: Callable[[], float] = _now,
    ) -> None:
        self.store = store or build_store()
        self.policy = policy or SessionPolicy.from_env()
        self.clock = clock

    def load(self, sid: str) -> Optional[SessionRecord]:
        record = self.store.get(sid)
        if record is None:
            return None
        now = self.clock()
        if record.revoked or now >= record.idle_expires_at or now >= record.absolute_expires_at:
            return None
        return record

    def create(self, sid: Optional[str] = None) -> SessionRecord:
        now = self.clock()
        return SessionRecord(
            sid=sid or new_sid(),
            created_at=now,
            last_seen=now,
            idle_expires_at=now + self.policy.idle_seconds,
            absolute_expires_at=now + self.policy.absolute_seconds,
        )

    def persist(
        self,
        record: SessionRecord,
        data: Dict[str, Any],
        request: Any = None,
    ) -> SessionRecord:
        now = self.clock()
        previous_userid = record.userid
        record.data = dict(data)
        record.last_seen = now
        record.idle_expires_at = now + self.policy.idle_seconds
        userid = data.get('userid')
        usertype = data.get('usertype')
        record.userid = userid
        record.usertype = usertype
        if request is not None:
            record.ip = client_ip(request) or record.ip
            ua = (request.headers.get('User-Agent') if hasattr(request, 'headers') else '') or record.user_agent
            record.user_agent = ua[:256]
        if userid:
            self._bind_device(record, previous_userid)
            self._enforce_concurrent(record)
        self.store.save(record)
        return record

    def _bind_device(self, record: SessionRecord, previous_userid: Optional[str]) -> None:
        userid = record.userid
        if not userid:
            return
        key = device_key(record.user_agent)
        record.device_key = key
        record.device_label = device_label(record.user_agent)
        known = self.store.get_device(userid, key)
        first_bind = previous_userid != userid
        if known is None:
            now = self.clock()
            self.store.upsert_device(
                Device(
                    userid=userid,
                    device_key=key,
                    device_label=record.device_label,
                    first_seen=now,
                    last_seen=now,
                    last_ip=record.ip,
                )
            )
            if first_bind:
                record.new_device = True
                record.data['_new_device'] = True
        else:
            known.last_seen = self.clock()
            known.last_ip = record.ip or known.last_ip
            known.device_label = record.device_label or known.device_label
            self.store.upsert_device(known)
            if first_bind and not record.data.get('_new_device'):
                record.new_device = False

    def _enforce_concurrent(self, current: SessionRecord) -> None:
        userid = current.userid
        if not userid:
            return
        limit = self.policy.max_for(current.usertype)
        if limit <= 0:
            return
        active = [
            r for r in self.store.list_active(userid, self.clock()) if r.sid != current.sid
        ]
        overflow = len(active) + 1 - limit
        if overflow <= 0:
            return
        active.sort(key=lambda r: r.last_seen)
        for record in active[:overflow]:
            self.store.revoke(record.sid)

    def destroy(self, sid: Optional[str]) -> None:
        if sid:
            self.store.delete(sid)

    def list_for(self, userid: str) -> List[SessionRecord]:
        return self.store.list_active(userid, self.clock())

    def revoke(self, sid: str, userid: str) -> SessionRecord:
        record = self.store.get(sid)
        if record is None or record.userid != userid:
            raise SessionError('session_not_found', 'Session not found', 404)
        if record.revoked:
            raise SessionError('session_not_found', 'Session not found', 404)
        revoked = self.store.revoke(sid)
        if revoked is None:
            raise SessionError('session_not_found', 'Session not found', 404)
        return revoked

    def revoke_others(self, userid: str, keep_sid: str) -> int:
        return self.store.revoke_all_except(userid, keep_sid)

    def snapshot(self, sess: Any) -> Dict[str, Any]:
        userid = sess.get('userid') if sess is not None else None
        current_sid = getattr(sess, 'sid', None)
        usertype = sess.get('usertype') if sess is not None else None
        sessions = self.list_for(userid) if userid else []
        return {
            'idle_seconds': self.policy.idle_seconds,
            'absolute_seconds': self.policy.absolute_seconds,
            'max_concurrent': self.policy.max_for(usertype),
            'new_device': bool(sess.get('_new_device')) if sess is not None else False,
            'current_sid': current_sid,
            'sessions': [r.as_public(current_sid) for r in sessions],
        }


def get_service() -> SessionService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SessionService()
    return _SERVICE


def set_service(service: Optional[SessionService]) -> None:
    global _SERVICE
    _SERVICE = service


def session_snapshot(sess: Any) -> Dict[str, Any]:
    return get_service().snapshot(sess)


def _require_user(sess: Any, values: Optional[Dict[str, Any]] = None) -> str:
    values = values or {}
    userid = sess.get('userid') if sess is not None else None
    if not userid:
        raise SessionError('unauthorized', 'Unauthorized access or session expired', 401)
    requested = values.get('userid')
    if requested and requested != userid:
        raise SessionError('forbidden', 'Session user mismatch', 403)
    return userid


def handle_list_request(sess: Any, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        _require_user(sess, values)
    except SessionError as exc:
        return {'status': exc.status, 'body': {'error': exc.code, 'code': exc.code, 'message': exc.message}}
    return {'status': 200, 'body': get_service().snapshot(sess)}


def handle_revoke_request(sess: Any, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    values = values or {}
    try:
        userid = _require_user(sess, values)
        target = values.get('sid')
        if not target:
            raise SessionError('invalid_sid', 'sid is required', 400)
        revoked = get_service().revoke(target, userid)
    except SessionError as exc:
        return {'status': exc.status, 'body': {'error': exc.code, 'code': exc.code, 'message': exc.message}}
    current = getattr(sess, 'sid', None) == revoked.sid
    if current:
        sess.clear()
    return {
        'status': 200,
        'body': {'message': 'revoked', 'sid': revoked.sid, 'current': current},
    }


def handle_revoke_others_request(sess: Any, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    values = values or {}
    try:
        userid = _require_user(sess, values)
        keep = getattr(sess, 'sid', None)
        if not keep:
            raise SessionError('invalid_sid', 'Current session is missing', 400)
        n = get_service().revoke_others(userid, keep)
    except SessionError as exc:
        return {'status': exc.status, 'body': {'error': exc.code, 'code': exc.code, 'message': exc.message}}
    return {'status': 200, 'body': {'message': 'revoked', 'count': n}}


def attach_session_routes(app: Any, service: Optional[SessionService] = None) -> Any:
    from flask import jsonify, request, session

    if service is not None:
        set_service(service)

    @app.route('/listSessions', methods=['POST', 'GET'])
    def list_sessions():
        values = request.get_json(silent=True) or {}
        result = handle_list_request(session, values)
        return jsonify(result['body']), result['status']

    @app.route('/revokeSession', methods=['POST', 'GET'])
    def revoke_session():
        values = request.get_json(silent=True) or {}
        result = handle_revoke_request(session, values)
        return jsonify(result['body']), result['status']

    @app.route('/revokeOtherSessions', methods=['POST', 'GET'])
    def revoke_other_sessions():
        values = request.get_json(silent=True) or {}
        result = handle_revoke_others_request(session, values)
        return jsonify(result['body']), result['status']

    return app


def _signing_serializer(app: Any):
    from itsdangerous import URLSafeTimedSerializer

    secret = app.secret_key
    if not secret:
        return None
    return URLSafeTimedSerializer(secret, salt='kb-server-session')


try:
    from flask.sessions import SessionInterface, SessionMixin
    from werkzeug.datastructures import CallbackDict

    class ServerSession(CallbackDict, SessionMixin):
        def __init__(self, initial: Optional[Dict[str, Any]] = None, sid: Optional[str] = None, new: bool = False):
            def on_update(self: 'ServerSession') -> None:
                self.modified = True

            CallbackDict.__init__(self, initial, on_update)
            self.sid = sid or new_sid()
            self.new = new
            self.modified = False
            self.accessed = True
            self.permanent = True
            self._invalid_cookie = False
            self._cleared = False

        def clear(self) -> None:  # type: ignore[override]
            CallbackDict.clear(self)
            self.modified = True
            self._cleared = True

    class ServerSessionInterface(SessionInterface):
        pickle_based = False

        def __init__(self, service: SessionService):
            self.service = service

        def should_set_cookie(self, app, session) -> bool:  # type: ignore[override]
            if not session:
                return False
            return True

        def open_session(self, app, request):  # type: ignore[override]
            serializer = _signing_serializer(app)
            if serializer is None:
                return None
            cookie_name = self.get_cookie_name(app)
            raw = request.cookies.get(cookie_name)
            sid = None
            if raw:
                try:
                    sid = serializer.loads(raw)
                except Exception:
                    sid = None
            if isinstance(sid, str) and sid:
                record = self.service.load(sid)
                if record is not None:
                    sess = ServerSession(record.data, sid=record.sid, new=False)
                    return sess
                sess = ServerSession(sid=new_sid(), new=True)
                sess._invalid_cookie = True
                return sess
            return ServerSession(sid=new_sid(), new=True)

        def save_session(self, app, session, response) -> None:  # type: ignore[override]
            from flask import request as flask_request

            name = self.get_cookie_name(app)
            domain = self.get_cookie_domain(app)
            path = self.get_cookie_path(app)
            secure = self.get_cookie_secure(app)
            samesite = self.get_cookie_samesite(app)
            httponly = self.get_cookie_httponly(app)
            serializer = _signing_serializer(app)
            if serializer is None:
                return

            if session.accessed:
                response.vary.add('Cookie')

            if not session:
                if getattr(session, 'sid', None) and (not session.new or session._cleared):
                    self.service.destroy(session.sid)
                if session.modified or getattr(session, '_invalid_cookie', False) or not session.new:
                    response.delete_cookie(
                        name,
                        domain=domain,
                        path=path,
                        secure=secure,
                        samesite=samesite,
                        httponly=httponly,
                    )
                    response.vary.add('Cookie')
                return

            if session._cleared:
                # Identity changed after clear() (login). Rotate the sid.
                if session.sid:
                    self.service.destroy(session.sid)
                session.sid = new_sid()
                session.new = True
                session._cleared = False

            record = self.service.load(session.sid) if not session.new else None
            if record is None:
                record = self.service.create(session.sid)
            self.service.persist(record, dict(session), request=flask_request)
            session.new = False

            expires = self.get_expiration_time(app, session)
            token = serializer.dumps(session.sid)
            response.set_cookie(
                name,
                token,
                expires=expires,
                httponly=httponly,
                domain=domain,
                path=path,
                secure=secure,
                samesite=samesite,
            )
            response.vary.add('Cookie')

except ImportError:  # pragma: no cover - Flask not installed in some test envs
    ServerSession = None  # type: ignore
    ServerSessionInterface = None  # type: ignore


def init_sessions(
    app: Any,
    store: Optional[SessionStore] = None,
    policy: Optional[SessionPolicy] = None,
    service: Optional[SessionService] = None,
) -> SessionService:
    """Install the server-side session interface and /listSessions routes."""
    from datetime import timedelta

    if not app.secret_key:
        app.secret_key = resolve_secret_key()
    service = service or SessionService(store=store, policy=policy)
    set_service(service)
    app.config.setdefault('SESSION_REFRESH_EACH_REQUEST', True)
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(seconds=service.policy.absolute_seconds)
    if ServerSessionInterface is not None:
        app.session_interface = ServerSessionInterface(service)
    attach_session_routes(app, service)
    return service
