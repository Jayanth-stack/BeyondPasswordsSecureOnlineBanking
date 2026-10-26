"""Real app.py wiring for card 3DS ACS."""
import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from utility.acs import MemoryAcsStore, set_service

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


class AcsAppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = _load_app()
        cls.app = cls.app_module.app
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        self.service = self.app_module.acs_service
        self.service.store = MemoryAcsStore()
        set_service(self.service)

    def _session(self, userid='alice', usertype='customer'):
        with self.client.session_transaction() as sess:
            sess['userid'] = userid
            sess['usertype'] = usertype

    def test_load_customer_includes_card_3ds_without_pan(self):
        self._session()
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            customers_cls.return_value.get_funds_requests.return_value = 'None'
            added = self.client.post('/addCard3dsKey', json={
                'userid': 'alice',
                'nickname': 'Travel',
                'pan': VISA,
                'default_account': '1001',
            })
            self.assertEqual(added.status_code, 201)
            self.assertNotIn('fingerprint', added.get_json()['key'])
            response = self.client.post('/loadCustomer')
        self.assertEqual(response.status_code, 200)
        snap = response.get_json()['Card3ds']
        self.assertEqual(snap['keys'][0]['nickname'], 'Travel')
        self.assertNotIn(VISA, str(response.get_json()))

    def test_get_customer_includes_card_3ds_for_staff(self):
        self._session()
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            customers_cls.return_value.get_funds_requests.return_value = 'None'
            self.client.post('/addCard3dsKey', json={
                'userid': 'alice',
                'nickname': 'Travel',
                'pan': VISA,
                'default_account': '1001',
            })
        self._session('teller', 'tier1')
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            response = self.client.post('/getCustomer', json={
                'userid': 'teller',
                'customer_id': 'alice',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['Card3ds']['keys'][0]['last4'], '1111')

    def test_disabled_snapshot_is_fail_open(self):
        set_service(None)
        self._session()
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.get_all_account.return_value = _accounts()
            customers_cls.return_value.get_customer_details.return_value = {'first_name': 'Ada'}
            customers_cls.return_value.get_funds_requests.return_value = 'None'
            response = self.client.post('/loadCustomer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['Card3ds']['enabled'], False)
        set_service(self.service)


if __name__ == '__main__':
    unittest.main()
