"""Import helpers that avoid requiring a live MySQL server at import time."""
import os
import sys
import tempfile
from unittest.mock import MagicMock

# Receipt HMAC is read at import time in utility.crypto_receipt.
os.environ.setdefault("RECEIPT_SECRET", "test-receipt-secret-do-not-use-in-prod")
# Linked-account / Fedwire services are constructed at app import; keep them
# in-memory and secret-stable so tests do not write sqlite or secret files.
os.environ.setdefault("LINK_STORE", "memory")
os.environ.setdefault("LINK_CHALLENGE_SECRET", "test-link-challenge-secret")
os.environ.setdefault("WIRE_STORE", "memory")
# PR #75 honors BANK_LOG_FILE; keep unittest imports off the tracked production log.
os.environ.setdefault(
    "BANK_LOG_FILE",
    os.path.join(tempfile.mkdtemp(), "bank.log"),
)

if "mysql" not in sys.modules:
    mysql_mod = MagicMock()
    sys.modules["mysql"] = mysql_mod
    sys.modules["mysql.connector"] = mysql_mod.connector

if "pymysql" not in sys.modules:
    sys.modules["pymysql"] = MagicMock()
