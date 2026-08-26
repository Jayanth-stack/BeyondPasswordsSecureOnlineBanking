import hashlib
import hmac
import os
import time
import logging

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

LOGIN_PURPOSE = 'login'
PASSWORD_RESET_PURPOSE = 'password_reset'
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_SEND_COOLDOWN_SECONDS = 60
PLACEHOLDER_TWILIO_MARKERS = (
    'your_account_sid',
    'your account_sid',
    'your_auth_token',
    'your auth_token',
    'your_verify_sid',
    'your verify_sid',
    'placeholder',
    'changeme',
)


class MfaError(Exception):
    pass


class MfaDeliveryError(MfaError):
    pass


class MfaCooldownError(MfaError):
    def __init__(self, retry_after):
        super().__init__('OTP was recently sent. Please wait before requesting another.')
        self.retry_after = int(retry_after)


def _is_placeholder(value):
    if not value:
        return True
    return value.strip().lower() in PLACEHOLDER_TWILIO_MARKERS or value.strip().lower().startswith('your_')


def normalize_phone(phone):
    if not phone:
        return None
    digits = ''.join(ch for ch in str(phone) if ch.isdigit() or ch == '+')
    if digits.startswith('+'):
        return digits
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    return '+' + digits if digits else None


def _window_index(ts=None, window_seconds=DEFAULT_WINDOW_SECONDS):
    return int((ts if ts is not None else time.time()) // window_seconds)


def compute_local_otp(destination, purpose, window=None, secret=None):
    """Deterministic 6-digit OTP for local/dev/tests. Not used when Twilio is configured."""
    secret = (secret or os.getenv('MFA_LOCAL_SECRET') or os.getenv('SECRET_KEY') or 'local-mfa-dev-only').encode()
    if window is None:
        window = _window_index()
    message = ('%s|%s|%s' % (destination, purpose, window)).encode('utf-8')
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return str(int(digest[:8], 16) % 1000000).zfill(6)


class SendCooldown:
    def __init__(self):
        self._last_sent = {}

    def check(self, key, cooldown_seconds=DEFAULT_SEND_COOLDOWN_SECONDS):
        now = time.time()
        last = self._last_sent.get(key, 0)
        wait = cooldown_seconds - (now - last)
        if wait > 0:
            raise MfaCooldownError(wait)

    def mark(self, key):
        self._last_sent[key] = time.time()

    def reset(self, key=None):
        if key is None:
            self._last_sent.clear()
        else:
            self._last_sent.pop(key, None)


class MfaProvider:
    def send(self, destination, purpose):
        raise NotImplementedError

    def verify(self, destination, purpose, code):
        raise NotImplementedError


class LocalOtpProvider(MfaProvider):
    """HMAC time-window OTP used when Twilio is not configured (dev, tests, local)."""

    def __init__(self, secret=None, window_seconds=DEFAULT_WINDOW_SECONDS):
        self.secret = secret
        self.window_seconds = window_seconds

    def send(self, destination, purpose):
        phone = normalize_phone(destination) or destination
        logger.info('Local MFA challenge issued for purpose=%s dest=%s', purpose, _mask_phone(phone))
        return {'status': 'pending', 'provider': 'local', 'destination': _mask_phone(phone)}

    def verify(self, destination, purpose, code):
        phone = normalize_phone(destination) or destination
        offered = str(code or '').strip()
        if not offered:
            return False
        now_window = _window_index(window_seconds=self.window_seconds)
        for window in (now_window, now_window - 1):
            expected = compute_local_otp(phone, purpose, window=window, secret=self.secret)
            if hmac.compare_digest(offered, expected):
                return True
        return False


class TwilioVerifyProvider(MfaProvider):
    def __init__(self, account_sid, auth_token, verify_sid):
        from twilio.rest import Client
        self.client = Client(account_sid, auth_token)
        self.verify_sid = verify_sid

    def send(self, destination, purpose):
        from twilio.base.exceptions import TwilioRestException
        phone = normalize_phone(destination)
        if not phone:
            raise MfaDeliveryError('No phone number available')
        try:
            self.client.verify.v2.services(self.verify_sid).verifications.create(
                to=phone, channel='sms'
            )
        except TwilioRestException as exc:
            raise MfaDeliveryError('Failed to send OTP') from exc
        return {'status': 'pending', 'provider': 'twilio', 'destination': _mask_phone(phone)}

    def verify(self, destination, purpose, code):
        from twilio.base.exceptions import TwilioRestException
        phone = normalize_phone(destination)
        if not phone or not code:
            return False
        try:
            result = self.client.verify.v2.services(self.verify_sid).verification_checks.create(
                to=phone, code=str(code).strip()
            )
            return result.status == 'approved'
        except TwilioRestException:
            return False


class MfaService:
    """
    Reusable MFA challenge API for login, password reset, and step-up checks.

    `purpose` isolates codes so a login OTP cannot be reused to reset a password.
    """

    def __init__(self, provider=None, cooldown=None):
        self.provider = provider or build_provider()
        self.cooldown = cooldown or SendCooldown()

    def send(self, destination, purpose, cooldown_seconds=DEFAULT_SEND_COOLDOWN_SECONDS):
        phone = normalize_phone(destination)
        if not phone:
            raise MfaDeliveryError('No phone number available')
        key = '%s:%s' % (purpose, phone)
        self.cooldown.check(key, cooldown_seconds)
        result = self.provider.send(phone, purpose)
        self.cooldown.mark(key)
        return result

    def verify(self, destination, purpose, code):
        phone = normalize_phone(destination)
        if not phone:
            return False
        return bool(self.provider.verify(phone, purpose, code))


_provider = None
_service = None


def twilio_configured():
    sid = os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    verify = os.getenv('TWILIO_VERIFY_SID')
    return not (_is_placeholder(sid) or _is_placeholder(token) or _is_placeholder(verify))


def build_provider():
    mode = (os.getenv('MFA_PROVIDER') or 'auto').strip().lower()
    if mode == 'local':
        return LocalOtpProvider()
    if mode == 'twilio' or (mode == 'auto' and twilio_configured()):
        if not twilio_configured():
            logger.warning('MFA_PROVIDER=twilio but credentials are missing; using local OTP provider')
            return LocalOtpProvider()
        return TwilioVerifyProvider(
            os.getenv('TWILIO_ACCOUNT_SID'),
            os.getenv('TWILIO_AUTH_TOKEN'),
            os.getenv('TWILIO_VERIFY_SID'),
        )
    if twilio_configured():
        return TwilioVerifyProvider(
            os.getenv('TWILIO_ACCOUNT_SID'),
            os.getenv('TWILIO_AUTH_TOKEN'),
            os.getenv('TWILIO_VERIFY_SID'),
        )
    logger.warning('Twilio Verify is not configured; using local OTP provider')
    return LocalOtpProvider()


def get_mfa_service(force_reload=False):
    global _provider, _service
    if _service is None or force_reload:
        _provider = build_provider()
        _service = MfaService(provider=_provider)
    return _service


def reset_mfa_service():
    global _provider, _service
    _provider = None
    _service = None


def _mask_phone(phone):
    if not phone:
        return None
    digits = ''.join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return '***'
    return '***' + digits[-4:]
