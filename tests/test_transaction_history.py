import unittest
from unittest.mock import patch

from tests.fakes import FakeConn
from utility.db import Database
from customer import Customers


class TransactionHistoryCapabilityTests(unittest.TestCase):
    def test_open_account_records_bonus_ledger(self):
        inserts = []

        def handler(sql, params):
            if sql.strip().startswith('CREATE TABLE'):
                return []
            if 'SELECT account_type FROM Accounts' in sql:
                return []
            if 'INSERT INTO Accounts' in sql:
                return {'rows': [], 'lastrowid': 15}
            if 'INSERT INTO AccountLedger' in sql:
                inserts.append(params)
                return {'rows': [], 'lastrowid': 1}
            return []

        c = Customers(Database(FakeConn(handler=handler)))
        self.assertEqual(c.open_account('alice', 'savings'), 'Done')
        self.assertTrue(inserts)
        self.assertEqual(inserts[0][0], 15)
        self.assertEqual(inserts[0][4], 'bonus')
        self.assertEqual(inserts[0][3], 'credit')

    def test_ownership_blocks_other_customers(self):
        def handler(sql, params):
            if 'FROM Accounts WHERE account_no' in sql and 'customer_id' in sql:
                return []
            return []

        c = Customers(Database(FakeConn(handler=handler)))
        self.assertIsNone(c.get_transaction_history(99, customer_id='attacker'))

    def test_owned_account_returns_ledger_entries(self):
        def handler(sql, params):
            if 'FROM Accounts WHERE account_no' in sql and 'customer_id' in sql:
                return [(1,)]
            if sql.strip().startswith('CREATE TABLE'):
                return []
            if 'FROM AccountLedger' in sql:
                return [{
                    'ledger_id': 1,
                    'account_no': 10,
                    'counterpart_account': None,
                    'amount': 250.0,
                    'direction': 'credit',
                    'kind': 'bonus',
                    'description': 'Bonus amount credited',
                    'transaction_no': None,
                    'balance_after': 250.0,
                    'created_at': '2026-08-26 13:00:00',
                }]
            return []

        c = Customers(Database(FakeConn(handler=handler)))
        history = c.get_transaction_history(10, customer_id='alice')
        self.assertEqual(history['source'], 'ledger')
        self.assertEqual(history['entries'][0]['kind'], 'bonus')
        self.assertIn('Bonus amount credited', history['html'])

    def test_lookup_queries_are_parameterized(self):
        conn = FakeConn()
        c = Customers(Database(conn))
        c.check_user_id("alice' OR '1'='1")
        sql, params = conn.executed[0]
        self.assertIn('%s', sql)
        self.assertNotIn("OR '1'='1", sql)
        self.assertEqual(params[0], "alice' OR '1'='1")


class HistoryRouteTests(unittest.TestCase):
    def setUp(self):
        import app as bank
        bank.app.config['TESTING'] = True
        self.bank = bank
        self.client = bank.app.test_client()

    def test_rejects_anonymous(self):
        resp = self.client.post(
            '/getTransactionHistory',
            json={'userid': 'alice', 'account_no': 1},
        )
        self.assertEqual(resp.status_code, 403)

    def test_rejects_unowned_account(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
        with patch.object(self.bank, 'Customers') as cls:
            cls.return_value.get_transaction_history.return_value = None
            resp = self.client.post(
                '/getTransactionHistory',
                json={'userid': 'alice', 'account_no': 99},
            )
        self.assertEqual(resp.status_code, 404)

    def test_returns_structured_ledger(self):
        with self.client.session_transaction() as sess:
            sess['userid'] = 'alice'
            sess['usertype'] = 'customer'
        payload = {
            'entries': [{
                'ledger_id': 1,
                'account_no': 10,
                'amount': 250.0,
                'direction': 'credit',
                'kind': 'bonus',
                'description': 'Bonus amount credited',
                'created_at': '2026-08-26 13:00:00',
                'balance_after': 250.0,
            }],
            'html': 'Bonus amount credited,<br>',
            'source': 'ledger',
        }
        with patch.object(self.bank, 'Customers') as cls:
            cls.return_value.get_transaction_history.return_value = payload
            resp = self.client.post(
                '/getTransactionHistory',
                json={'userid': 'alice', 'account_no': 10},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['source'], 'ledger')
        self.assertEqual(body['transactions'][0]['kind'], 'bonus')
        self.assertEqual(body['message'][0][0], 'Bonus amount credited,<br>')


if __name__ == '__main__':
    unittest.main()
