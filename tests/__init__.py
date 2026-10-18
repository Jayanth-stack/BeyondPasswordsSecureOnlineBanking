"""Import helpers that avoid requiring a live MySQL server at import time."""
import os
import sys
import tempfile
from unittest.mock import MagicMock

# Receipt HMAC is read at import time in utility.crypto_receipt.
os.environ.setdefault("RECEIPT_SECRET", "test-receipt-secret-do-not-use-in-prod")
# Linked-account / Fedwire services are constructed at app import; keep them in-memory.
os.environ.setdefault("LINK_STORE", "memory")
os.environ.setdefault("LINK_CHALLENGE_SECRET", "test-link-challenge-secret")
os.environ.setdefault("WIRE_STORE", "memory")
os.environ.setdefault("BANK_LOG_FILE", os.path.join(tempfile.mkdtemp(), "bank.log"))
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-twilio-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-token")
os.environ.setdefault("TWILIO_VERIFY_SID", "test-verify-sid")

if "mysql" not in sys.modules:
    mysql_mod = MagicMock()
    sys.modules["mysql"] = mysql_mod
    sys.modules["mysql.connector"] = mysql_mod.connector

if "pymysql" not in sys.modules:
    sys.modules["pymysql"] = MagicMock()
