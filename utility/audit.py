import json
import logging
import os
import re
from datetime import datetime, timezone

AUDIT_LOGGER_NAME = 'bank.audit'
REDACT_KEYS = {
    'password', 'oldpassword', 'newpassword', 'otp', 'otp_code', 'code',
    'ssn', 'token', 'secret', 'auth_token', 'account_sid', 'hashed',
    'authorization', 'cookie',
}
REDACT_VALUE = '[REDACTED]'

_audit_logger = None


def _iso_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def redact(value):
    """Recursively strip secrets from structures that may be logged."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in REDACT_KEYS:
                cleaned[key] = REDACT_VALUE
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str) and re.search(r'(password|otp|ssn|secret|token)\s*[:=]', value, re.I):
        return REDACT_VALUE
    return value


def get_audit_logger():
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    os.makedirs('SystemLogs', exist_ok=True)
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler('SystemLogs/audit.log', mode='a', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    _audit_logger = logger
    return logger


def audit(action, outcome, actor=None, usertype=None, resource=None, ip=None, **details):
    """
    Append one structured security/audit event.

    This is the shared logging capability for login, MFA, password reset,
    and later financial actions. Never pass raw secrets in details.
    """
    event = {
        'ts': _iso_now(),
        'action': action,
        'outcome': outcome,
        'actor': actor,
        'usertype': usertype,
        'resource': resource,
        'ip': ip,
        'details': redact(details) if details else {},
    }
    get_audit_logger().info(json.dumps(event, default=str))
    return event
