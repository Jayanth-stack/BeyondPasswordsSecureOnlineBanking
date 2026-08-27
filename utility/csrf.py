"""Synchronizer CSRF tokens bound to the Flask session.

Tokens are issued over GET /csrf-token and required on unsafe methods via
the X-CSRF-Token header (JSON body `csrf_token` is accepted as a fallback).
"""
from __future__ import annotations

import hmac
import secrets
from typing import Any, Iterable, Optional

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})
SESSION_KEY = '_csrf_token'
HEADER_NAMES = ('X-CSRF-Token', 'X-CSRFToken')


class CsrfProtection:
    def __init__(self, session_key: str = SESSION_KEY):
        self.session_key = session_key

    def issue(self, session: Any) -> str:
        token = session.get(self.session_key)
        if not token:
            token = secrets.token_urlsafe(32)
            session[self.session_key] = token
        return token

    def rotate(self, session: Any) -> str:
        token = secrets.token_urlsafe(32)
        session[self.session_key] = token
        return token

    def expected(self, session: Any) -> Optional[str]:
        token = session.get(self.session_key)
        return token if token else None

    def extract(self, request: Any) -> Optional[str]:
        headers = getattr(request, 'headers', None) or {}
        for name in HEADER_NAMES:
            value = headers.get(name) if hasattr(headers, 'get') else None
            if value:
                return value
        json_body = None
        get_json = getattr(request, 'get_json', None)
        if callable(get_json):
            try:
                json_body = get_json(silent=True)
            except TypeError:
                json_body = get_json()
        if isinstance(json_body, dict):
            body_token = json_body.get('csrf_token')
            if body_token:
                return body_token
        form = getattr(request, 'form', None)
        if form is not None:
            form_token = form.get('csrf_token') if hasattr(form, 'get') else None
            if form_token:
                return form_token
        args = getattr(request, 'args', None)
        if args is not None:
            arg_token = args.get('csrf_token') if hasattr(args, 'get') else None
            if arg_token:
                return arg_token
        return None

    def validate(self, session: Any, request: Any) -> bool:
        expected = self.expected(session)
        provided = self.extract(request)
        if not expected or not provided:
            return False
        if not isinstance(expected, str) or not isinstance(provided, str):
            return False
        if len(expected) != len(provided):
            return False
        return hmac.compare_digest(expected, provided)


def init_csrf(app, protection: Optional[CsrfProtection] = None,
              exempt_paths: Optional[Iterable[str]] = None):
    from flask import jsonify, request, session

    protection = protection or CsrfProtection()
    app.extensions['csrf'] = protection
    exempt = set(exempt_paths or ())
    exempt.add('/csrf-token')

    @app.route('/csrf-token', methods=['GET'])
    def csrf_token():
        token = protection.issue(session)
        return jsonify({'csrf_token': token})

    @app.before_request
    def enforce_csrf():
        if not app.config.get('CSRF_ENABLED', True):
            return None
        if request.method in SAFE_METHODS:
            return None
        path = request.path
        if path in exempt or path.startswith('/static'):
            return None
        if protection.validate(session, request):
            return None
        return jsonify({
            'message': 'CSRF token missing or invalid',
            'code': 'csrf_failed',
        }), 403

    return protection
