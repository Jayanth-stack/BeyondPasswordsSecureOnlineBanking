from functools import wraps
import time

from flask import jsonify, redirect, request, session, url_for

AUTH_STAGE_PENDING_MFA = 'pending_mfa'
AUTH_STAGE_AUTHENTICATED = 'authenticated'
RESET_TTL_SECONDS = 600


def start_mfa_login(sess, userid, usertype, emp_tier=None):
    """Password succeeded. Do not grant an authenticated session until MFA completes."""
    sess.clear()
    sess['pending_userid'] = userid
    sess['pending_usertype'] = usertype
    sess['auth_stage'] = AUTH_STAGE_PENDING_MFA
    sess['mfa_verified'] = False
    if emp_tier is not None:
        sess['pending_emp_tier'] = emp_tier


def complete_mfa_login(sess):
    """Promote a pending MFA session to a fully authenticated session."""
    userid = sess.get('pending_userid')
    usertype = sess.get('pending_usertype')
    if not userid or not usertype:
        return False
    emp_tier = sess.get('pending_emp_tier')
    sess['userid'] = userid
    sess['usertype'] = usertype
    sess['mfa_verified'] = True
    sess['auth_stage'] = AUTH_STAGE_AUTHENTICATED
    if emp_tier is not None:
        sess['emp_tier'] = emp_tier
    sess.pop('pending_userid', None)
    sess.pop('pending_usertype', None)
    sess.pop('pending_emp_tier', None)
    return True


def is_pending_mfa(sess):
    return (
        sess.get('auth_stage') == AUTH_STAGE_PENDING_MFA
        and bool(sess.get('pending_userid'))
        and not sess.get('mfa_verified')
    )


def is_fully_authenticated(sess):
    return bool(sess.get('mfa_verified')) and bool(sess.get('userid')) and sess.get('auth_stage') == AUTH_STAGE_AUTHENTICATED


def pending_identity(sess):
    return {
        'userid': sess.get('pending_userid'),
        'usertype': sess.get('pending_usertype'),
        'emp_tier': sess.get('pending_emp_tier'),
    }


def current_identity(sess):
    return {
        'userid': sess.get('userid'),
        'usertype': sess.get('usertype'),
        'emp_tier': sess.get('emp_tier'),
    }


def clear_session(sess):
    sess.clear()


def mark_password_reset_verified(sess, userid, usertype):
    sess['reset_userid'] = userid
    sess['reset_usertype'] = usertype
    sess['reset_verified_at'] = time.time()


def password_reset_verified(sess, userid, max_age=RESET_TTL_SECONDS):
    if sess.get('reset_userid') != userid:
        return False
    verified_at = sess.get('reset_verified_at') or 0
    return (time.time() - float(verified_at)) <= max_age


def clear_password_reset(sess):
    sess.pop('reset_userid', None)
    sess.pop('reset_usertype', None)
    sess.pop('reset_verified_at', None)


def dashboard_endpoint(usertype, emp_tier=None):
    if usertype == 'customer':
        return 'get_customer_dash_ui'
    if usertype == 'admin':
        return 'get_admin_dashboard_ui'
    try:
        tier = int(emp_tier or 1)
    except (TypeError, ValueError):
        tier = 1
    if tier >= 2:
        return 'get_tier2_dashboard_ui'
    return 'get_tier1_dashboard_ui'


def _wants_json():
    if request.is_json:
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept


def _reject_unauthenticated(pending_ok=False):
    if pending_ok and is_pending_mfa(session):
        return None
    if is_fully_authenticated(session):
        return None
    if is_pending_mfa(session):
        if _wants_json():
            return jsonify({'message': 'MFA verification required', 'auth_stage': AUTH_STAGE_PENDING_MFA}), 401
        return redirect(url_for('get_verifyotp_dashboard_ui'))
    if _wants_json():
        return jsonify({'message': 'Unauthorized access or session expired'}), 401
    return redirect(url_for('get_login_page_ui'))


def require_authenticated(*roles):
    """Decorator: require a post-MFA session, optionally constrained to usertypes."""
    role_set = set(roles)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            rejected = _reject_unauthenticated(pending_ok=False)
            if rejected is not None:
                return rejected
            if role_set and session.get('usertype') not in role_set:
                return jsonify({'message': 'Insufficient permissions'}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_pending_mfa(fn):
    """Decorator: OTP page / verify / resend only."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if is_fully_authenticated(session):
            endpoint = dashboard_endpoint(session.get('usertype'), session.get('emp_tier'))
            return redirect(url_for(endpoint))
        if not is_pending_mfa(session):
            if _wants_json() or request.method == 'POST':
                return jsonify({'error': 'Session expired or invalid'}), 401
            return redirect(url_for('get_login_page_ui'))
        return fn(*args, **kwargs)
    return wrapper
