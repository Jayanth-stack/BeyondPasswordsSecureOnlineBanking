"""Outbound security-event notification capability.

Reusable emit path for login, device, password, profile, and high-value
money events. In-app inbox is the source of truth; email/SMS channels are
pluggable and fail-open. Device recognition is independent of the unmerged
session/device registry (PR #28).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

log = logging.getLogger(__name__)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
KNOWN_EVENTS = frozenset({
    'new_device',
    'login_success',
    'login_failure',
    'password_reset',
    'password_change',
    'profile_change',
    'high_value',
})
DEFAULT_EVENTS = frozenset({
    'new_device',
    'login_failure',
    'password_reset',
    'password_change',
    'profile_change',
    'high_value',
})
MONEY_QUANTUM = Decimal('0.01')
MAX_AMOUNT = Decimal('1000000000')

_UA_BROWSERS = (
    ('edg/', 'Edge'),
    ('edge/', 'Edge'),
    ('opr/', 'Opera'),
    ('chrome/', 'Chrome'),
    ('firefox/', 'Firefox'),
    ('msie', 'IE'),
    ('trident/', 'IE'),
    ('safari/', 'Safari'),
)
_UA_OS = (
    ('windows', 'Windows'),
    ('iphone', 'iOS'),
    ('ipad', 'iOS'),
    ('android', 'Android'),
    ('mac os', 'macOS'),
    ('macintosh', 'macOS'),
    ('linux', 'Linux'),
)


class AmountError(ValueError):
    pass


class NotifyError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return Decimal(str(raw).strip())


def parse_money(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AmountError('invalid_amount')
    text = str(value).strip()
    if not text or any(ch in text for ch in 'eE+'):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise AmountError('invalid_amount') from exc
    if amount.as_tuple().exponent < -2:
        raise AmountError('invalid_amount')
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized != amount:
        raise AmountError('invalid_amount')
    if quantized <= 0 or quantized > MAX_AMOUNT:
        raise AmountError('invalid_amount')
    return quantized


def parse_user_agent(user_agent: Optional[str]) -> Tuple[str, str]:
    text = (user_agent or '').strip()
    lowered = text.lower()
    family = 'Other'
    for needle, name in _UA_BROWSERS:
        if needle in lowered:
            family = name
            break
    os_name = 'Other'
    for needle, name in _UA_OS:
        if needle in lowered:
            os_name = name
            break
    return family, os_name


def client_ip(raw: Optional[str]) -> str:
    text = (raw or '').strip()
    if not text:
        return ''
    return text.split(',')[0].strip()


def ip_network(ip: Optional[str]) -> str:
    host = client_ip(ip)
    if not host:
        return 'unknown'
    if '.' in host and ':' not in host:
        parts = host.split('.')
        if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            return '.'.join(parts[:3]) + '.0'
        return 'unknown'
    if ':' in host:
        hextets = [part for part in host.split(':') if part]
        return ':'.join(hextets[:4]) + '::' if hextets else 'unknown'
    return 'unknown'


def mask_ip(ip: Optional[str]) -> str:
    host = client_ip(ip)
    if not host:
        return 'unknown'
    if '.' in host and ':' not in host:
        parts = host.split('.')
        if len(parts) == 4:
            return '.'.join(parts[:3]) + '.x'
    if ':' in host:
        return ip_network(host)
    return 'unknown'


def mask_account(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not text:
        return ''
    return text[-4:].rjust(4, '*')


def device_key(userid: str, user_agent: Optional[str], ip: Optional[str]) -> str:
    family, os_name = parse_user_agent(user_agent)
    raw = '|'.join((str(userid), family, os_name, ip_network(ip)))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def request_context() -> Dict[str, str]:
    forwarded = request.headers.get('X-Forwarded-For') if request else None
    remote = request.remote_addr if request else None
    agent = request.headers.get('User-Agent') if request else None
    return {
        'ip': client_ip(forwarded) or (remote or ''),
        'user_agent': agent or '',
    }


@dataclass(frozen=True)
class Contact:
    userid: str
    usertype: str = 'customer'
    email: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class DeliveryResult:
    ok: bool
    channel: str
    error: Optional[str] = None


class Channel:
    kind = 'log'
    name = 'log'

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        raise NotImplementedError


class LogChannel(Channel):
    kind = 'log'
    name = 'log'

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        log.info('security_notify event=%s user=%s subject=%s', event, contact.userid, subject)
        return DeliveryResult(ok=True, channel=self.name)


class RecordingChannel(Channel):
    def __init__(self, kind: str = 'email', name: Optional[str] = None):
        self.kind = kind
        self.name = name or kind
        self.sent: List[Dict[str, Any]] = []

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        self.sent.append({
            'userid': contact.userid,
            'email': contact.email,
            'phone': contact.phone,
            'subject': subject,
            'body': body,
            'event': event,
            'kind': self.kind,
        })
        return DeliveryResult(ok=True, channel=self.name)


class FailingChannel(Channel):
    def __init__(self, kind: str = 'email', error: str = 'boom'):
        self.kind = kind
        self.name = kind
        self.error = error

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        return DeliveryResult(ok=False, channel=self.name, error=self.error)


class SmtpEmailChannel(Channel):
    kind = 'email'
    name = 'email'

    def __init__(self, host: str, port: int, sender: str, username: str = '', password: str = '',
                 use_tls: bool = True):
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.use_tls = use_tls

    @classmethod
    def from_env(cls) -> Optional['SmtpEmailChannel']:
        host = (os.environ.get('SMTP_HOST') or '').strip()
        sender = (os.environ.get('SMTP_FROM') or '').strip()
        if not host or not sender:
            return None
        return cls(
            host=host,
            port=_env_int('SMTP_PORT', 587),
            sender=sender,
            username=(os.environ.get('SMTP_USER') or '').strip(),
            password=os.environ.get('SMTP_PASSWORD') or '',
            use_tls=_env_bool('SMTP_TLS', True),
        )

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        if not contact.email:
            return DeliveryResult(ok=False, channel=self.name, error='no_email')
        try:
            import smtplib
            from email.message import EmailMessage

            message = EmailMessage()
            message['Subject'] = subject
            message['From'] = self.sender
            message['To'] = contact.email
            message.set_content(body)
            with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
            return DeliveryResult(ok=True, channel=self.name)
        except Exception as exc:
            return DeliveryResult(ok=False, channel=self.name, error=str(exc))


class TwilioSmsChannel(Channel):
    kind = 'sms'
    name = 'sms'

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    @classmethod
    def from_env(cls) -> Optional['TwilioSmsChannel']:
        sid = (os.environ.get('TWILIO_ACCOUNT_SID') or '').strip()
        token = (os.environ.get('TWILIO_AUTH_TOKEN') or '').strip()
        from_number = (os.environ.get('TWILIO_FROM_NUMBER') or '').strip()
        if not sid or not token or not from_number:
            return None
        if sid.lower().startswith('your'):
            return None
        return cls(sid, token, from_number)

    def deliver(self, contact: Contact, subject: str, body: str, event: str) -> DeliveryResult:
        if not contact.phone:
            return DeliveryResult(ok=False, channel=self.name, error='no_phone')
        try:
            from twilio.rest import Client

            client = Client(self.account_sid, self.auth_token)
            client.messages.create(to=contact.phone, from_=self.from_number, body=f'{subject}: {body}')
            return DeliveryResult(ok=True, channel=self.name)
        except Exception as exc:
            return DeliveryResult(ok=False, channel=self.name, error=str(exc))


def default_channels() -> List[Channel]:
    channels: List[Channel] = [LogChannel()]
    email = SmtpEmailChannel.from_env()
    if email:
        channels.append(email)
    sms = TwilioSmsChannel.from_env()
    if sms:
        channels.append(sms)
    return channels


def default_contact_resolver(userid: str, usertype: str) -> Contact:
    try:
        if usertype in EMPLOYEE_ROLES:
            from employee import Employee

            details = Employee().get_employee_details(userid)
        else:
            from customer import Customers

            details = Customers().get_customer_details(userid)
        if not details or details == 'None':
            return Contact(userid=userid, usertype=usertype)
        return Contact(
            userid=userid,
            usertype=usertype,
            email=details.get('email_id') or details.get('email'),
            phone=details.get('contact_no') or details.get('phone'),
        )
    except Exception:
        return Contact(userid=userid, usertype=usertype)


@dataclass
class Notification:
    notification_id: str
    userid: str
    usertype: str
    event: str
    title: str
    body: str
    channels: List[str]
    details: Dict[str, Any]
    status: str
    created_at: float
    read_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'notification_id': self.notification_id,
            'userid': self.userid,
            'usertype': self.usertype,
            'event': self.event,
            'title': self.title,
            'body': self.body,
            'channels': list(self.channels),
            'details': dict(self.details),
            'status': self.status,
            'created_at': self.created_at,
            'read_at': self.read_at,
            'unread': self.read_at is None,
        }


@dataclass
class NotifyPrefs:
    userid: str
    email_enabled: bool = True
    sms_enabled: bool = True
    events: frozenset = field(default_factory=lambda: frozenset(DEFAULT_EVENTS))
    muted_until: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'userid': self.userid,
            'email_enabled': self.email_enabled,
            'sms_enabled': self.sms_enabled,
            'events': sorted(self.events),
            'muted_until': self.muted_until,
        }

    def allows(self, event: str, now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        if self.muted_until and clock < self.muted_until:
            return False
        return event in self.events


@dataclass(frozen=True)
class NotifyPolicy:
    enabled: bool = True
    customer_only: bool = True
    events: frozenset = field(default_factory=lambda: frozenset(DEFAULT_EVENTS))
    high_value_threshold: Decimal = Decimal('500')
    login_failure_threshold: int = 3
    login_failure_window: int = 900
    cooldown_seconds: int = 300
    inbox_limit: int = 50

    @classmethod
    def from_env(cls) -> 'NotifyPolicy':
        raw = os.environ.get('NOTIFY_EVENTS', ','.join(sorted(DEFAULT_EVENTS)))
        parsed = frozenset(part.strip() for part in raw.split(',') if part.strip() in KNOWN_EVENTS)
        return cls(
            enabled=_env_bool('NOTIFY_ENABLED', True),
            customer_only=_env_bool('NOTIFY_CUSTOMER_ONLY', True),
            events=parsed or frozenset(DEFAULT_EVENTS),
            high_value_threshold=_env_decimal('NOTIFY_HIGH_VALUE', Decimal('500')),
            login_failure_threshold=max(1, _env_int('NOTIFY_LOGIN_FAILURES', 3)),
            login_failure_window=max(30, _env_int('NOTIFY_FAILURE_WINDOW', 900)),
            cooldown_seconds=max(0, _env_int('NOTIFY_COOLDOWN', 300)),
            inbox_limit=max(1, _env_int('NOTIFY_INBOX_LIMIT', 50)),
        )

    def actor_allowed(self, usertype: str) -> bool:
        if not self.enabled:
            return False
        if self.customer_only and usertype in EMPLOYEE_ROLES:
            return False
        return True

    def event_allowed(self, event: str) -> bool:
        return event in self.events and event in KNOWN_EVENTS

    def to_dict(self) -> Dict[str, Any]:
        return {
            'enabled': self.enabled,
            'customer_only': self.customer_only,
            'events': sorted(self.events),
            'high_value_threshold': format(self.high_value_threshold, 'f'),
            'login_failure_threshold': self.login_failure_threshold,
            'login_failure_window': self.login_failure_window,
            'cooldown_seconds': self.cooldown_seconds,
        }


class NotifyStore:
    def remember_device(self, userid: str, key: str, ua_family: str, ua_os: str, ip_net: str,
                        now: Optional[float] = None) -> bool:
        raise NotImplementedError

    def is_known_device(self, userid: str, key: str) -> bool:
        raise NotImplementedError

    def record_failure(self, userid: str, ip: str, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def failure_count(self, userid: str, window: int, now: Optional[float] = None) -> int:
        raise NotImplementedError

    def clear_failures(self, userid: str) -> None:
        raise NotImplementedError

    def last_emit(self, userid: str, event: str, fingerprint: str) -> Optional[float]:
        raise NotImplementedError

    def set_last_emit(self, userid: str, event: str, fingerprint: str, now: Optional[float] = None) -> None:
        raise NotImplementedError

    def save_notification(self, item: Notification) -> None:
        raise NotImplementedError

    def list_notifications(self, userid: str, limit: int = 50) -> List[Notification]:
        raise NotImplementedError

    def get_notification(self, userid: str, notification_id: str) -> Optional[Notification]:
        raise NotImplementedError

    def mark_read(self, userid: str, notification_id: Optional[str] = None,
                  now: Optional[float] = None) -> int:
        raise NotImplementedError

    def unread_count(self, userid: str) -> int:
        raise NotImplementedError

    def get_prefs(self, userid: str) -> Optional[NotifyPrefs]:
        raise NotImplementedError

    def set_prefs(self, prefs: NotifyPrefs) -> None:
        raise NotImplementedError


def _notification_from_row(row: Sequence[Any]) -> Notification:
    return Notification(
        notification_id=row[0],
        userid=row[1],
        usertype=row[2],
        event=row[3],
        title=row[4],
        body=row[5],
        channels=json.loads(row[6] or '[]'),
        details=json.loads(row[7] or '{}'),
        status=row[8],
        created_at=float(row[9]),
        read_at=None if row[10] is None else float(row[10]),
    )


class MemoryNotifyStore(NotifyStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.devices: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.failures: Dict[str, List[Tuple[float, str]]] = {}
        self.cooldowns: Dict[Tuple[str, str, str], float] = {}
        self.notifications: Dict[str, Notification] = {}
        self.prefs: Dict[str, NotifyPrefs] = {}

    def remember_device(self, userid: str, key: str, ua_family: str, ua_os: str, ip_net: str,
                        now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        with self._lock:
            slot = self.devices.get((userid, key))
            if slot is None:
                self.devices[(userid, key)] = {
                    'ua_family': ua_family,
                    'ua_os': ua_os,
                    'ip_net': ip_net,
                    'first_seen': clock,
                    'last_seen': clock,
                }
                return True
            slot['last_seen'] = clock
            return False

    def is_known_device(self, userid: str, key: str) -> bool:
        with self._lock:
            return (userid, key) in self.devices

    def record_failure(self, userid: str, ip: str, now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        with self._lock:
            bucket = self.failures.setdefault(userid, [])
            bucket.append((clock, ip))
            return len(bucket)

    def failure_count(self, userid: str, window: int, now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        cutoff = clock - window
        with self._lock:
            bucket = [item for item in self.failures.get(userid, []) if item[0] >= cutoff]
            self.failures[userid] = bucket
            return len(bucket)

    def clear_failures(self, userid: str) -> None:
        with self._lock:
            self.failures.pop(userid, None)

    def last_emit(self, userid: str, event: str, fingerprint: str) -> Optional[float]:
        with self._lock:
            return self.cooldowns.get((userid, event, fingerprint))

    def set_last_emit(self, userid: str, event: str, fingerprint: str, now: Optional[float] = None) -> None:
        clock = time.time() if now is None else now
        with self._lock:
            self.cooldowns[(userid, event, fingerprint)] = clock

    def save_notification(self, item: Notification) -> None:
        with self._lock:
            self.notifications[item.notification_id] = item

    def list_notifications(self, userid: str, limit: int = 50) -> List[Notification]:
        with self._lock:
            items = [item for item in self.notifications.values() if item.userid == userid]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    def get_notification(self, userid: str, notification_id: str) -> Optional[Notification]:
        with self._lock:
            item = self.notifications.get(notification_id)
            if item is None or item.userid != userid:
                return None
            return item

    def mark_read(self, userid: str, notification_id: Optional[str] = None,
                  now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        changed = 0
        with self._lock:
            for item in self.notifications.values():
                if item.userid != userid or item.read_at is not None:
                    continue
                if notification_id and item.notification_id != notification_id:
                    continue
                item.read_at = clock
                changed += 1
        return changed

    def unread_count(self, userid: str) -> int:
        with self._lock:
            return sum(1 for item in self.notifications.values()
                       if item.userid == userid and item.read_at is None)

    def get_prefs(self, userid: str) -> Optional[NotifyPrefs]:
        with self._lock:
            return self.prefs.get(userid)

    def set_prefs(self, prefs: NotifyPrefs) -> None:
        with self._lock:
            self.prefs[prefs.userid] = prefs


class SqliteNotifyStore(NotifyStore):
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                '''
                CREATE TABLE IF NOT EXISTS devices (
                    userid TEXT NOT NULL,
                    device_key TEXT NOT NULL,
                    ua_family TEXT,
                    ua_os TEXT,
                    ip_net TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    PRIMARY KEY (userid, device_key)
                );
                CREATE TABLE IF NOT EXISTS failures (
                    userid TEXT NOT NULL,
                    ts REAL NOT NULL,
                    ip TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_notify_failures ON failures(userid, ts);
                CREATE TABLE IF NOT EXISTS cooldowns (
                    userid TEXT NOT NULL,
                    event TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    last_at REAL NOT NULL,
                    PRIMARY KEY (userid, event, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    usertype TEXT,
                    event TEXT NOT NULL,
                    title TEXT,
                    body TEXT,
                    channels TEXT,
                    details TEXT,
                    status TEXT,
                    created_at REAL,
                    read_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_notify_inbox ON notifications(userid, created_at);
                CREATE TABLE IF NOT EXISTS prefs (
                    userid TEXT PRIMARY KEY,
                    email_enabled INTEGER,
                    sms_enabled INTEGER,
                    events TEXT,
                    muted_until REAL
                );
                '''
            )

    def remember_device(self, userid: str, key: str, ua_family: str, ua_os: str, ip_net: str,
                        now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT 1 FROM devices WHERE userid=? AND device_key=?',
                (userid, key),
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO devices(userid, device_key, ua_family, ua_os, ip_net, first_seen, last_seen) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (userid, key, ua_family, ua_os, ip_net, clock, clock),
                )
                return True
            conn.execute(
                'UPDATE devices SET last_seen=?, ua_family=?, ua_os=?, ip_net=? '
                'WHERE userid=? AND device_key=?',
                (clock, ua_family, ua_os, ip_net, userid, key),
            )
            return False

    def is_known_device(self, userid: str, key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT 1 FROM devices WHERE userid=? AND device_key=?',
                (userid, key),
            ).fetchone()
            return row is not None

    def record_failure(self, userid: str, ip: str, now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            conn.execute('INSERT INTO failures(userid, ts, ip) VALUES (?,?,?)', (userid, clock, ip))
            row = conn.execute('SELECT COUNT(*) FROM failures WHERE userid=?', (userid,)).fetchone()
            return int(row[0]) if row else 0

    def failure_count(self, userid: str, window: int, now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        cutoff = clock - window
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM failures WHERE userid=? AND ts<?', (userid, cutoff))
            row = conn.execute(
                'SELECT COUNT(*) FROM failures WHERE userid=? AND ts>=?',
                (userid, cutoff),
            ).fetchone()
            return int(row[0]) if row else 0

    def clear_failures(self, userid: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM failures WHERE userid=?', (userid,))

    def last_emit(self, userid: str, event: str, fingerprint: str) -> Optional[float]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT last_at FROM cooldowns WHERE userid=? AND event=? AND fingerprint=?',
                (userid, event, fingerprint),
            ).fetchone()
            return None if row is None else float(row[0])

    def set_last_emit(self, userid: str, event: str, fingerprint: str, now: Optional[float] = None) -> None:
        clock = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO cooldowns(userid, event, fingerprint, last_at) VALUES (?,?,?,?)',
                (userid, event, fingerprint, clock),
            )

    def save_notification(self, item: Notification) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO notifications('
                'notification_id, userid, usertype, event, title, body, channels, details, status, created_at, read_at'
                ') VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (
                    item.notification_id, item.userid, item.usertype, item.event, item.title, item.body,
                    json.dumps(item.channels), json.dumps(item.details), item.status, item.created_at, item.read_at,
                ),
            )

    def list_notifications(self, userid: str, limit: int = 50) -> List[Notification]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT notification_id, userid, usertype, event, title, body, channels, details, status, '
                'created_at, read_at FROM notifications WHERE userid=? ORDER BY created_at DESC LIMIT ?',
                (userid, limit),
            ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def get_notification(self, userid: str, notification_id: str) -> Optional[Notification]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT notification_id, userid, usertype, event, title, body, channels, details, status, '
                'created_at, read_at FROM notifications WHERE userid=? AND notification_id=?',
                (userid, notification_id),
            ).fetchone()
        return None if row is None else _notification_from_row(row)

    def mark_read(self, userid: str, notification_id: Optional[str] = None,
                  now: Optional[float] = None) -> int:
        clock = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            if notification_id:
                cursor = conn.execute(
                    'UPDATE notifications SET read_at=? WHERE userid=? AND notification_id=? AND read_at IS NULL',
                    (clock, userid, notification_id),
                )
            else:
                cursor = conn.execute(
                    'UPDATE notifications SET read_at=? WHERE userid=? AND read_at IS NULL',
                    (clock, userid),
                )
            return cursor.rowcount

    def unread_count(self, userid: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) FROM notifications WHERE userid=? AND read_at IS NULL',
                (userid,),
            ).fetchone()
            return int(row[0]) if row else 0

    def get_prefs(self, userid: str) -> Optional[NotifyPrefs]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT userid, email_enabled, sms_enabled, events, muted_until FROM prefs WHERE userid=?',
                (userid,),
            ).fetchone()
        if row is None:
            return None
        events = frozenset(json.loads(row[3] or '[]')) or frozenset(DEFAULT_EVENTS)
        return NotifyPrefs(
            userid=row[0],
            email_enabled=bool(row[1]),
            sms_enabled=bool(row[2]),
            events=events,
            muted_until=None if row[4] is None else float(row[4]),
        )

    def set_prefs(self, prefs: NotifyPrefs) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO prefs(userid, email_enabled, sms_enabled, events, muted_until) '
                'VALUES (?,?,?,?,?)',
                (prefs.userid, int(prefs.email_enabled), int(prefs.sms_enabled),
                 json.dumps(sorted(prefs.events)), prefs.muted_until),
            )


TITLES = {
    'new_device': 'New device sign-in',
    'login_success': 'Successful sign-in',
    'login_failure': 'Repeated failed sign-in attempts',
    'password_reset': 'Password reset',
    'password_change': 'Password changed',
    'profile_change': 'Profile update requested',
    'high_value': 'High-value money movement',
}


def render_body(event: str, details: Dict[str, Any]) -> str:
    if event == 'new_device':
        return (
            f"A new {details.get('ua_family', 'device')} on {details.get('ua_os', 'unknown OS')} "
            f"signed in from {details.get('ip_masked', 'an unknown location')}."
        )
    if event == 'login_failure':
        return f"{details.get('count', 0)} failed sign-in attempts were recorded for your account."
    if event == 'password_reset':
        return 'Your password was reset. If this was not you, contact the bank immediately.'
    if event == 'password_change':
        return 'Your password was changed. If this was not you, contact the bank immediately.'
    if event == 'profile_change':
        return 'A request to update your email, phone, or address was submitted.'
    if event == 'high_value':
        return (
            f"A {details.get('operation', 'transfer')} of ${details.get('amount', '0')} "
            f"was submitted from account ending {details.get('from_account', '????')}."
        )
    if event == 'login_success':
        return f"Successful sign-in from {details.get('ip_masked', 'an unknown location')}."
    return 'A security event was recorded on your account.'


def _sanitize_details(event: str, details: Optional[Dict[str, Any]], ip: str, user_agent: str) -> Dict[str, Any]:
    family, os_name = parse_user_agent(user_agent)
    clean: Dict[str, Any] = {
        'ua_family': family,
        'ua_os': os_name,
        'ip_masked': mask_ip(ip),
    }
    source = details or {}
    blocked = {'password', 'newPassword', 'oldPassword', 'otp', 'otp_code', 'ssn', 'secret'}
    for key, value in source.items():
        if key in blocked:
            continue
        if key in {'from_account', 'to_account', 'account', 'fromAccount', 'toAccount'}:
            clean[key if 'from' in key.lower() or key == 'account' else key] = mask_account(value)
            if 'from' in key.lower() or key == 'account':
                clean['from_account'] = mask_account(value)
            if 'to' in key.lower():
                clean['to_account'] = mask_account(value)
            continue
        if key == 'amount':
            try:
                clean['amount'] = format(parse_money(value), 'f')
            except AmountError:
                clean['amount'] = str(value)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean


class NotifyService:
    def __init__(
        self,
        store: NotifyStore,
        policy: Optional[NotifyPolicy] = None,
        channels: Optional[Sequence[Channel]] = None,
        contact_resolver: Optional[Callable[[str, str], Contact]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store
        self.policy = policy or NotifyPolicy()
        self.channels = list(channels or default_channels())
        self.contact_resolver = contact_resolver or default_contact_resolver
        self.clock = clock or time.time

    def prefs_for(self, userid: str) -> NotifyPrefs:
        return self.store.get_prefs(userid) or NotifyPrefs(userid=userid)

    def update_prefs(self, userid: str, **changes: Any) -> NotifyPrefs:
        current = self.prefs_for(userid)
        events = current.events
        if 'events' in changes and changes['events'] is not None:
            raw = changes['events']
            if isinstance(raw, str):
                raw = [part.strip() for part in raw.split(',') if part.strip()]
            parsed = frozenset(item for item in raw if item in KNOWN_EVENTS)
            if not parsed:
                raise NotifyError('invalid_events', 'No valid notification events')
            events = parsed
        muted = current.muted_until
        if 'muted_until' in changes:
            muted = changes['muted_until']
        prefs = NotifyPrefs(
            userid=userid,
            email_enabled=current.email_enabled if changes.get('email_enabled') is None
            else bool(changes['email_enabled']),
            sms_enabled=current.sms_enabled if changes.get('sms_enabled') is None
            else bool(changes['sms_enabled']),
            events=events,
            muted_until=muted,
        )
        self.store.set_prefs(prefs)
        return prefs

    def recognize_device(self, userid: str, user_agent: Optional[str], ip: Optional[str]) -> Tuple[str, bool]:
        key = device_key(userid, user_agent, ip)
        family, os_name = parse_user_agent(user_agent)
        is_new = self.store.remember_device(userid, key, family, os_name, ip_network(ip), now=self.clock())
        return key, is_new

    def _on_cooldown(self, userid: str, event: str, fingerprint: str) -> bool:
        if self.policy.cooldown_seconds <= 0:
            return False
        last = self.store.last_emit(userid, event, fingerprint)
        if last is None:
            return False
        return (self.clock() - last) < self.policy.cooldown_seconds

    def emit(
        self,
        event: str,
        *,
        userid: str,
        usertype: str = 'customer',
        details: Optional[Dict[str, Any]] = None,
        ip: str = '',
        user_agent: str = '',
        fingerprint: Optional[str] = None,
        force: bool = False,
    ) -> Optional[Notification]:
        if event not in KNOWN_EVENTS:
            return None
        if not force and not self.policy.actor_allowed(usertype):
            return None
        if not force and not self.policy.event_allowed(event):
            return None
        prefs = self.prefs_for(userid)
        if not force and not prefs.allows(event, now=self.clock()):
            return None
        mark = fingerprint or event
        if not force and self._on_cooldown(userid, event, mark):
            return None

        clean = _sanitize_details(event, details, ip, user_agent)
        title = TITLES.get(event, 'Security alert')
        body = render_body(event, clean)
        contact = self.contact_resolver(userid, usertype)
        used: List[str] = ['inbox']
        delivered = False
        for channel in self.channels:
            if channel.kind == 'email' and not prefs.email_enabled:
                continue
            if channel.kind == 'sms' and not prefs.sms_enabled:
                continue
            try:
                result = channel.deliver(contact, title, body, event)
            except Exception as exc:
                result = DeliveryResult(ok=False, channel=getattr(channel, 'name', 'channel'), error=str(exc))
            if result.ok:
                used.append(result.channel)
                if channel.kind != 'log':
                    delivered = True
            elif result.channel:
                used.append(f'{result.channel}:failed')
        status = 'delivered' if delivered else 'inbox'
        item = Notification(
            notification_id=uuid.uuid4().hex,
            userid=userid,
            usertype=usertype,
            event=event,
            title=title,
            body=body,
            channels=used,
            details=clean,
            status=status,
            created_at=self.clock(),
        )
        self.store.save_notification(item)
        self.store.set_last_emit(userid, event, mark, now=item.created_at)
        return item

    def emit_quietly(self, event: str, **kwargs: Any) -> Optional[Notification]:
        try:
            return self.emit(event, **kwargs)
        except Exception:
            log.exception('security notify failed for %s', event)
            return None

    def note_login_failure(self, userid: str, usertype: str = 'customer', ip: str = '',
                           user_agent: str = '') -> Optional[Notification]:
        self.store.record_failure(userid, ip, now=self.clock())
        count = self.store.failure_count(userid, self.policy.login_failure_window, now=self.clock())
        if count < self.policy.login_failure_threshold:
            return None
        return self.emit_quietly(
            'login_failure',
            userid=userid,
            usertype=usertype,
            details={'count': count},
            ip=ip,
            user_agent=user_agent,
            fingerprint='login',
        )

    def note_login_success(self, userid: str, usertype: str = 'customer', ip: str = '',
                           user_agent: str = '') -> Optional[Notification]:
        self.store.clear_failures(userid)
        key, is_new = self.recognize_device(userid, user_agent, ip)
        if is_new:
            return self.emit_quietly(
                'new_device',
                userid=userid,
                usertype=usertype,
                details={'device_key': key},
                ip=ip,
                user_agent=user_agent,
                fingerprint=key,
            )
        if self.policy.event_allowed('login_success'):
            return self.emit_quietly(
                'login_success',
                userid=userid,
                usertype=usertype,
                ip=ip,
                user_agent=user_agent,
                fingerprint='login_success',
            )
        return None

    def emit_if_high_value(
        self,
        userid: str,
        usertype: str,
        operation: str,
        amount: Any,
        from_account: Any = None,
        to_account: Any = None,
        ip: str = '',
        user_agent: str = '',
    ) -> Optional[Notification]:
        try:
            parsed = parse_money(amount)
        except AmountError:
            return None
        if parsed < self.policy.high_value_threshold:
            return None
        return self.emit_quietly(
            'high_value',
            userid=userid,
            usertype=usertype,
            details={
                'operation': operation,
                'amount': format(parsed, 'f'),
                'from_account': from_account,
                'to_account': to_account,
            },
            ip=ip,
            user_agent=user_agent,
            fingerprint=f'{operation}:{format(parsed, "f")}',
        )

    def snapshot(self, userid: str) -> Dict[str, Any]:
        items = [item.to_dict() for item in self.store.list_notifications(userid, self.policy.inbox_limit)]
        return {
            'items': items,
            'unread': self.store.unread_count(userid),
            'prefs': self.prefs_for(userid).to_dict(),
            'policy': self.policy.to_dict(),
        }

    def mark_read(self, userid: str, notification_id: Optional[str] = None) -> int:
        if notification_id:
            item = self.store.get_notification(userid, notification_id)
            if item is None:
                raise NotifyError('notification_not_found', 'Notification not found')
        return self.store.mark_read(userid, notification_id, now=self.clock())


def build_store() -> NotifyStore:
    kind = (os.environ.get('NOTIFY_STORE') or 'sqlite').strip().lower()
    if kind == 'memory':
        return MemoryNotifyStore()
    path = os.environ.get('NOTIFY_DB') or os.path.join('SystemLogs', 'notify.sqlite')
    return SqliteNotifyStore(path)


def build_service(
    store: Optional[NotifyStore] = None,
    policy: Optional[NotifyPolicy] = None,
    channels: Optional[Sequence[Channel]] = None,
    contact_resolver: Optional[Callable[[str, str], Contact]] = None,
) -> NotifyService:
    return NotifyService(
        store=store or build_store(),
        policy=policy or NotifyPolicy.from_env(),
        channels=channels or default_channels(),
        contact_resolver=contact_resolver,
    )


def _require_session_user() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
    if 'userid' not in session:
        return None, (jsonify({'error': 'unauthorized'}), 401)
    values = request.get_json(silent=True) or {}
    req_id = values.get('userid') or session['userid']
    if req_id != session['userid']:
        return None, (jsonify({'error': 'userid_mismatch'}), 403)
    return values, None


def attach_notify_routes(app: Any, service: NotifyService) -> None:
    @app.route('/listNotifications', methods=['POST', 'GET'])
    def list_notifications():
        values, error = _require_session_user()
        if error:
            return error
        return jsonify(service.snapshot(session['userid'])), 200

    @app.route('/markNotificationRead', methods=['POST', 'GET'])
    def mark_notification_read():
        values, error = _require_session_user()
        if error:
            return error
        assert values is not None
        notification_id = values.get('notification_id')
        mark_all = bool(values.get('all'))
        try:
            changed = service.mark_read(session['userid'], None if mark_all else notification_id)
        except NotifyError as exc:
            status = 404 if exc.code == 'notification_not_found' else 400
            return jsonify({'error': exc.code, 'message': exc.message}), status
        return jsonify({'updated': changed, 'unread': service.store.unread_count(session['userid'])}), 200

    @app.route('/updateNotifyPrefs', methods=['POST', 'GET'])
    def update_notify_prefs():
        values, error = _require_session_user()
        if error:
            return error
        assert values is not None
        try:
            prefs = service.update_prefs(
                session['userid'],
                email_enabled=values.get('email_enabled'),
                sms_enabled=values.get('sms_enabled'),
                events=values.get('events'),
            )
        except NotifyError as exc:
            return jsonify({'error': exc.code, 'message': exc.message}), 400
        return jsonify(prefs.to_dict()), 200
