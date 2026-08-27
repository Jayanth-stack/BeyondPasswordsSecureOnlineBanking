"""Flask wiring for CSRF + persistent rate limits.

Keeps identity/IP extraction and 429 payload shape in one place so login,
OTP, password reset, and registration share the same policy.
"""
from __future__ import annotations

import os
from typing import Optional

from flask import current_app, jsonify, request, session

from utility.csrf import init_csrf
from utility.rate_limit import RateLimitExceeded, RateLimiter


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def identity_key(userid) -> str:
    return str(userid or '').strip().lower()[:64]


def client_ip() -> str:
    # Ignore X-Forwarded-For: it is attacker-controlled without a trusted proxy.
    return (request.remote_addr or 'unknown').strip()[:64] or 'unknown'


def get_limiter() -> RateLimiter:
    return current_app.extensions['rate_limiter']


def rate_limit_response(exc: RateLimitExceeded):
    body = jsonify({
        'message': 'Too many attempts. Try again later.',
        'code': 'rate_limited',
        'retry_after': exc.retry_after,
    })
    body.status_code = 429
    body.headers['Retry-After'] = str(exc.retry_after)
    return body


def consume_or_429(key: str, limit: int, window_seconds: int):
    try:
        get_limiter().consume(key, limit=limit, window_seconds=window_seconds)
    except RateLimitExceeded as exc:
        return rate_limit_response(exc)
    return None


def reject_if_limited(key: str, limit: int, window_seconds: int):
    if get_limiter().over_limit(key, limit=limit, window_seconds=window_seconds):
        retry = window_seconds
        oldest = get_limiter().store.oldest_since(key, get_limiter().clock() - window_seconds)
        if oldest is not None:
            retry = int(window_seconds - (get_limiter().clock() - oldest)) + 1
        return rate_limit_response(RateLimitExceeded(retry, key))
    return None


def record_failure(key: str, limit: int, window_seconds: int):
    """Count a failed attempt; ignore the exception if this hit itself trips the cap."""
    try:
        get_limiter().consume(key, limit=limit, window_seconds=window_seconds)
    except RateLimitExceeded as exc:
        return rate_limit_response(exc)
    return None


def clear_limit(key: str) -> None:
    get_limiter().reset(key)


def rotate_csrf() -> Optional[str]:
    csrf = current_app.extensions.get('csrf')
    if csrf is None:
        return None
    return csrf.rotate(session)


def init_request_guards(app, limiter: Optional[RateLimiter] = None):
    app.config.setdefault('CSRF_ENABLED', _env_flag('CSRF_ENABLED', True))
    app.config.setdefault(
        'RATE_LIMIT_STORE',
        os.getenv('RATE_LIMIT_STORE', os.path.join('SystemLogs', 'rate_limit.sqlite')),
    )
    app.config.setdefault('LOGIN_USER_LIMIT', _env_int('LOGIN_USER_LIMIT', 5))
    app.config.setdefault('LOGIN_USER_WINDOW', _env_int('LOGIN_USER_WINDOW', 900))
    app.config.setdefault('LOGIN_IP_LIMIT', _env_int('LOGIN_IP_LIMIT', 20))
    app.config.setdefault('LOGIN_IP_WINDOW', _env_int('LOGIN_IP_WINDOW', 900))
    app.config.setdefault('OTP_VERIFY_LIMIT', _env_int('OTP_VERIFY_LIMIT', 5))
    app.config.setdefault('OTP_VERIFY_WINDOW', _env_int('OTP_VERIFY_WINDOW', 600))
    app.config.setdefault('OTP_SEND_LIMIT', _env_int('OTP_SEND_LIMIT', 3))
    app.config.setdefault('OTP_SEND_WINDOW', _env_int('OTP_SEND_WINDOW', 600))
    app.config.setdefault('RESET_LIMIT', _env_int('RESET_LIMIT', 5))
    app.config.setdefault('RESET_WINDOW', _env_int('RESET_WINDOW', 900))
    app.config.setdefault('REGISTER_IP_LIMIT', _env_int('REGISTER_IP_LIMIT', 10))
    app.config.setdefault('REGISTER_IP_WINDOW', _env_int('REGISTER_IP_WINDOW', 3600))

    if limiter is None:
        limiter = RateLimiter.from_path(app.config['RATE_LIMIT_STORE'])
    app.extensions['rate_limiter'] = limiter
    init_csrf(app)
    return limiter
