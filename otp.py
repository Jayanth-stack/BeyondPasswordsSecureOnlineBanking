import pyotp
import pyotp as pyotp
import time
import datetime
from customer import Customers

class OtpInterface:
    def __init__(self):
        self.totp = pyotp.TOTP(pyotp.random_base32(), interval =300)



    def getObj(self):
        return self.totp()

    def send_otp(self, phone):
        phone = "+1" + phone



    def verify(self, otp):
        if self.totp is not None:
            if (self.totp.verify(otp)):
                self.totp = None
                return 'Verified'
            else:
                return 'Otp not verified'
        else:
            return 'Must send OTP first before Verification '