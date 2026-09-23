import importlib
import unittest
from unittest.mock import patch

import tests  # noqa: F401 - installs mysql mocks before app imports


class ApproveRequestEmpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('twilio.rest.Client'):
            import app as app_module
            importlib.reload(app_module)
            cls.app = app_module.app
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

    def test_customer_cannot_approve_employee_transfer(self):
        with patch('app.Employee') as employee_cls, patch('app.Customers') as customers_cls:
            employee_cls.return_value.get_amount_of_transaction.return_value = 500
            customers_cls.return_value.fund_transfers.return_value = 'done'

            with self.client.session_transaction() as sess:
                sess['userid'] = 'cust1'
                sess['usertype'] = 'customer'

            response = self.client.post(
                '/approveRequestEmp',
                json={'userid': 'cust1', 'transaction_no': 99},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['message'], 'Unauthorized access')
        customers_cls.return_value.fund_transfers.assert_not_called()

    def test_employee_can_approve_transfer(self):
        with patch('app.Employee') as employee_cls, patch('app.Customers') as customers_cls:
            emp = employee_cls.return_value
            emp.get_employee_tier.return_value = 1
            emp.get_amount_of_transaction.return_value = 500
            emp.get_fromAccount_of_transaction.return_value = 1
            emp.get_toAccount_of_transaction.return_value = 2
            emp.get_transaction_status.return_value = 1
            customers_cls.return_value.fund_transfers.return_value = 'done'

            with self.client.session_transaction() as sess:
                sess['userid'] = 'emp1'
                sess['usertype'] = 'tier1'
                sess['emp_tier'] = 1

            response = self.client.post(
                '/approveRequestEmp',
                json={'userid': 'emp1', 'transaction_no': 99},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['message'], 'done')
        customers_cls.return_value.fund_transfers.assert_called_once()


if __name__ == '__main__':
    unittest.main()
