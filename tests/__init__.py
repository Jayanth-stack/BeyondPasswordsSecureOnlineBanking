import os

os.environ.setdefault('RECEIPT_SECRET', 'unit-test-receipt-secret')
os.environ.setdefault('DB_HOST', 'localhost')
os.environ.setdefault('DB_USER', 'root')
os.environ.setdefault('DB_PASSWORD', 'root')
os.environ.setdefault('DB_PORT', '3306')
os.environ.setdefault('DB_NAME', 'bankingapplication')
os.environ.setdefault('TWILIO_ACCOUNT_SID', 'ACtest')
os.environ.setdefault('TWILIO_AUTH_TOKEN', 'token')
os.environ.setdefault('TWILIO_VERIFY_SID', 'VAtest')
