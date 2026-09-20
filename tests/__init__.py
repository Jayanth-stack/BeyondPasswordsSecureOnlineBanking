"""Import helpers that avoid requiring a live MySQL server at import time."""
import os
import sys
from unittest.mock import MagicMock

# Receipt HMAC is read at import time in utility.crypto_receipt.
os.environ.setdefault("RECEIPT_SECRET", "test-receipt-secret-do-not-use-in-prod")
# Linked-account service is constructed at app import; keep it in-memory and secret-stable.
os.environ.setdefault("LINK_STORE", "memory")
os.environ.setdefault("LINK_CHALLENGE_SECRET", "test-link-challenge-secret")

if "mysql" not in sys.modules:
    mysql_mod = MagicMock()
    sys.modules["mysql"] = mysql_mod
    sys.modules["mysql.connector"] = mysql_mod.connector

if "pymysql" not in sys.modules:
    sys.modules["pymysql"] = MagicMock()
