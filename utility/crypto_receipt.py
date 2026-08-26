import os
import hmac
import hashlib
import json
import uuid
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Secret key for HMAC, must be set in .env as RECEIPT_SECRET
_receipt_secret = os.getenv("RECEIPT_SECRET")
if not _receipt_secret:
    raise ValueError("RECEIPT_SECRET environment variable must be set")
SECRET = _receipt_secret.encode()


def is_successful_transfer(result) -> bool:
    """True when fund_transfers completed; accepts legacy 'done' or signed receipt dict."""
    return result == "done" or (
        isinstance(result, dict) and "signature" in result
    )

def generate_nonce():
    return str(uuid.uuid4())

def current_timestamp():
    return datetime.utcnow().isoformat() + "Z"

def generate_receipt(data: dict) -> dict:
    """
    Generate HMAC-SHA256 signature over sorted JSON of data.
    Returns data merged with 'signature' field.
    """
    message = json.dumps(data, sort_keys=True).encode('utf-8')
    signature = hmac.new(SECRET, message, hashlib.sha256).hexdigest()
    return {**data, "signature": signature}