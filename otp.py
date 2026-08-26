"""Compatibility wrapper around the shared MFA service."""
from utility.mfa import LOGIN_PURPOSE, get_mfa_service


class OtpInterface:
    """Legacy name kept so existing imports continue to work."""

    def __init__(self, service=None):
        self.service = service or get_mfa_service()

    def send_otp(self, phone, purpose=LOGIN_PURPOSE):
        return self.service.send(phone, purpose)

    def verify(self, phone, otp, purpose=LOGIN_PURPOSE):
        if self.service.verify(phone, purpose, otp):
            return 'Verified'
        return 'Otp not verified'
