"""Helpers to import the Flask app without a live MySQL/Twilio stack."""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault('RECEIPT_SECRET', 'test-receipt-secret')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-sessions')
os.environ.setdefault('MFA_PROVIDER', 'local')
os.environ.setdefault('MFA_LOCAL_SECRET', 'test-mfa-local-secret')
os.environ.setdefault('TWILIO_ACCOUNT_SID', 'your_account_sid')
os.environ.setdefault('TWILIO_AUTH_TOKEN', 'your_auth_token')
os.environ.setdefault('TWILIO_VERIFY_SID', 'your_verify_sid')
os.environ.setdefault('DB_HOST', 'localhost')
os.environ.setdefault('DB_USER', 'root')
os.environ.setdefault('DB_PASSWORD', 'root')
os.environ.setdefault('DB_PORT', '3306')
os.environ.setdefault('DB_NAME', 'bankingapplication')

if 'mysql' not in sys.modules:
    mysql_mod = MagicMock()
    sys.modules['mysql'] = mysql_mod
    sys.modules['mysql.connector'] = mysql_mod.connector

if 'pymysql' not in sys.modules:
    sys.modules['pymysql'] = MagicMock()
