import os
import hmac
import hashlib
import json
import uuid
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Secret key for HMAC, must be set in .env as RECEIPT_SECRET
SECRET = os.getenv("RECEIPT_SECRET").encode()

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