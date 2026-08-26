import unittest

from tests.fakes import FakeConn
from utility.db import Database, QueryError, bind_params, quote_ident


class BindParamsTests(unittest.TestCase):
    def test_rejects_interpolated_string_params(self):
        with self.assertRaises(QueryError):
            bind_params("SELECT * FROM t WHERE id=%s", "1 OR 1=1")

    def test_rejects_named_params(self):
        with self.assertRaises(QueryError):
            bind_params("SELECT * FROM t WHERE id=%s", {'id': 1})

    def test_mismatch_is_an_error(self):
        with self.assertRaises(QueryError):
            bind_params("SELECT * FROM t WHERE id=%s AND x=%s", (1,))

    def test_static_sql_allows_empty_params(self):
        sql, params = bind_params("SELECT emp_id FROM Employees WHERE tier = 1")
        self.assertEqual(params, ())
        self.assertIn('Employees', sql)


class QuoteIdentTests(unittest.TestCase):
    def test_quotes_simple_names(self):
        self.assertEqual(quote_ident('AccountLedger'), '`AccountLedger`')

    def test_rejects_injection(self):
        with self.assertRaises(QueryError):
            quote_ident('Accounts; DROP TABLE Customers')
        with self.assertRaises(QueryError):
            quote_ident('account`no')


class DatabaseQueryTests(unittest.TestCase):
    def test_values_stay_in_bind_params(self):
        conn = FakeConn()
        db = Database(conn)
        payload = "alice' OR '1'='1"
        db.fetch_one("SELECT customer_id FROM Customers WHERE customer_id=%s", (payload,))
        sql, params = conn.executed[0]
        self.assertNotIn(payload, sql)
        self.assertEqual(params, (payload,))

    def test_execute_commits_outside_transaction(self):
        conn = FakeConn()
        db = Database(conn)
        db.execute("UPDATE Customers SET address=%s WHERE customer_id=%s", ('x', 'alice'))
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.started, 0)

    def test_transaction_commits_once(self):
        conn = FakeConn()
        db = Database(conn)
        with db.transaction():
            db.execute("UPDATE Accounts SET balance = balance + %s WHERE account_no=%s", (10, 1))
            db.execute("UPDATE Accounts SET balance = balance - %s WHERE account_no=%s", (10, 2))
        self.assertEqual(conn.started, 1)
        self.assertEqual(conn.commits, 1)

    def test_transaction_rolls_back(self):
        conn = FakeConn()
        db = Database(conn)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with db.transaction():
                db.execute("UPDATE Accounts SET balance = balance + %s WHERE account_no=%s", (10, 1))
                raise Boom()
        self.assertEqual(conn.rollbacks, 1)


if __name__ == '__main__':
    unittest.main()
