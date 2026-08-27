"""Load the Flask app with MySQL stubbed so tests do not need a live database."""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock

os.environ.setdefault('RECEIPT_SECRET', 'test-receipt-secret')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('CSRF_ENABLED', 'true')
os.environ.setdefault('TWILIO_ACCOUNT_SID', 'ACtest')
os.environ.setdefault('TWILIO_AUTH_TOKEN', 'token')
os.environ.setdefault('TWILIO_VERIFY_SID', 'VAtest')

_STORE_DIR = tempfile.mkdtemp(prefix='rate-limit-tests-')
os.environ['RATE_LIMIT_STORE'] = os.path.join(_STORE_DIR, 'rate_limit.sqlite')


def _install_mysql_stub():
    fake_connector = MagicMock()
    fake_db = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = None
    fake_cursor.fetchall.return_value = []
    fake_db.cursor.return_value = fake_cursor
    fake_connector.connect.return_value = fake_db
    fake_connector.connector = fake_connector

    fake_mysql = MagicMock()
    fake_mysql.connector = fake_connector

    sys.modules['mysql'] = fake_mysql
    sys.modules['mysql.connector'] = fake_connector


_install_mysql_stub()

import app as bank  # noqa: E402

bank.app.config['TESTING'] = True
bank.app.config['WTF_CSRF_ENABLED'] = False
bank.app.config['LOGIN_USER_LIMIT'] = 5
bank.app.config['LOGIN_USER_WINDOW'] = 900
bank.app.config['LOGIN_IP_LIMIT'] = 20
bank.app.config['LOGIN_IP_WINDOW'] = 900


def csrf_header(client):
    response = client.get('/csrf-token')
    token = response.get_json()['csrf_token']
    return {'X-CSRF-Token': token}
