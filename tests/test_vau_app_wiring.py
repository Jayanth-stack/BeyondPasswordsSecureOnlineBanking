"""Real app.py wiring for account updater — isolated Flask tests miss this.

Routes close over the import-time service; loadCustomer/getCustomer read
get_vau_service(); session usertype is trusted as the updater actor role.
"""
import importlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('VAU_STORE', 'memory')

import tests  # noqa: F401

from utility.vau import MemoryVauStore, set_service


VISA = '4111111111111111'


def _load_app():
    with patch('twilio.rest.Client'):
        import app as app_module

        importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        return app_module


def _accounts(_userid='alice'):
    return {
        'checkin': {'Account': 1001, 'Balance': 50},
        'savings': {'Account': 1002, 'Balance': 10},
        'credit': 'None',
    }


class VauAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['VAU_STORE'] = 'memory'
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.vau_service
        self.service.store = MemoryVauStore()
        set_service(self.service)

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def _add_payload(self, **overrides):
        body = {
            'userid': 'alice',
            'nickname': 'Travel Visa',
            'legal_name': 'Ada Lovelace',
            'pan': VISA,
            'expiry': '1227',
            'default_account': '1001',
        }
        body.update(overrides)
        return body

    def test_load_customer_includes_updater_without_pan(self):
        self._session()
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            customers_cls.return_value.get_funds_requests.return_value = 'None'
            added = self.client.post('/addCardUpdaterKey', json=self._add_payload())
            self.assertEqual(added.status_code, 201)
            self.assertNotIn('pan', added.get_json()['key'])
            self.assertNotIn('fingerprint', added.get_json()['key'])
            response = self.client.post('/loadCustomer')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('CardUpdater', payload)
        self.assertIn('Wires', payload)
        self.assertIn('LinkedAccounts', payload)
        updater = payload['CardUpdater']
        self.assertEqual(updater['keys'][0]['nickname'], 'Travel Visa')
        self.assertNotIn('pan', updater['keys'][0])
        self.assertNotIn(VISA, str(updater))

    def test_get_customer_staff_snapshot_and_money_routes_untouched(self):
        self._session()
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            customers_cls.return_value.get_funds_requests.return_value = 'None'
            added = self.client.post('/addCardUpdaterKey', json=self._add_payload())
            self.assertEqual(added.status_code, 201)
        self._session('teller', 'tier1')
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            lookup = self.client.post('/getCustomer', json={'userid': 'teller', 'customer_id': 'alice'})
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.get_json()['CardUpdater']['keys'][0]['last4'], '1111')

    def test_unauthenticated_load_still_401(self):
        response = self.client.post('/loadCustomer')
        self.assertEqual(response.status_code, 401)
