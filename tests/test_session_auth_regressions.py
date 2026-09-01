import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401 - installs mysql mocks before app imports


class SessionAuthRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('twilio.rest.Client'):
            import app as app_module
            importlib.reload(app_module)
            cls.app = app_module.app
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

    def test_deny_request_rejects_session_key_spoof(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'cust1'
            sess['usertype'] = 'customer'

        response = self.client.post(
            '/denyRequest',
            json={'userid': 'userid', 'transaction_no': 42},
        )
        self.assertIn(response.status_code, (302, 401))

    def test_deny_request_allows_matching_customer(self):
        with patch('app.Customers') as customers_cls:
            customers_cls.return_value.deny_funds_requested.return_value = 'Request Cancelled'
            with self.client.session_transaction() as sess:
                sess['userid'] = 'cust1'
                sess['usertype'] = 'customer'

            response = self.client.post(
                '/denyRequest',
                json={'userid': 'cust1', 'transaction_no': 42},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['message'], 'Request Cancelled')

    def test_approve_request_uses_session_userid(self):
        with patch('app.Employee') as employee_cls, patch('app.Customers') as customers_cls:
            emp = employee_cls.return_value
            emp.get_amount_of_transaction.return_value = 50
            emp.get_fromAccount_of_transaction.return_value = 1
            emp.get_toAccount_of_transaction.return_value = 2
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = 'done'

            with self.client.session_transaction() as sess:
                sess['userid'] = 'cust1'
                sess['usertype'] = 'customer'

            response = self.client.post(
                '/approveRequest',
                json={'customer_id': 'cust1', 'transaction_no': 7},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['message'], 'done')

    def test_logout_clears_session(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'cust1'
            sess['usertype'] = 'customer'

        self.client.post('/logout', json={'userid': 'cust1'})

        with self.client.session_transaction() as sess:
            self.assertNotIn('userid', sess)
            self.assertNotIn('usertype', sess)

    def test_approve_request_emp_rejects_customer(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'cust1'
            sess['usertype'] = 'customer'

        response = self.client.post(
            '/approveRequestEmp',
            json={'userid': 'cust1', 'transaction_no': 7},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
