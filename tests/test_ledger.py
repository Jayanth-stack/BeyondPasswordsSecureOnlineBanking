import unittest
from datetime import datetime

from tests.fakes import FakeConn
from utility.db import Database, QueryError
from utility import ledger
from utility.ledger import (
    record_entry,
    list_entries,
    entries_as_legacy_html,
    ensure_ledger_table,
    serialize_entry,
)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        ledger._schema_ready = False

    def test_ensure_creates_table(self):
        conn = FakeConn()
        db = Database(conn)
        ensure_ledger_table(db)
        sql, params = conn.executed[0]
        self.assertIn('CREATE TABLE IF NOT EXISTS AccountLedger', sql)
        self.assertEqual(params, ())

    def test_record_and_list(self):
        stored = []

        def handler(sql, params):
            if sql.strip().startswith('CREATE TABLE'):
                return []
            if 'INSERT INTO AccountLedger' in sql:
                stored.append(params)
                return {'rows': [], 'lastrowid': 7}
            if 'FROM AccountLedger' in sql:
                row = {
                    'ledger_id': 7,
                    'account_no': params[0],
                    'counterpart_account': None,
                    'amount': 250.0,
                    'direction': 'credit',
                    'kind': 'bonus',
                    'description': 'Bonus amount credited',
                    'transaction_no': None,
                    'balance_after': 250.0,
                    'created_at': datetime(2026, 8, 26, 13, 0, 0),
                }
                return [row]
            return []

        conn = FakeConn(handler=handler)
        db = Database(conn)
        ledger._schema_ready = True
        rowid = record_entry(
            db,
            account_no=42,
            amount=250,
            direction='credit',
            kind='bonus',
            description='Bonus amount credited',
            balance_after=250,
        )
        self.assertEqual(rowid, 7)
        self.assertEqual(stored[0][0], 42)
        self.assertEqual(stored[0][3], 'credit')
        entries = list_entries(db, 42)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'bonus')
        self.assertEqual(entries[0]['created_at'], '2026-08-26 13:00:00')

    def test_rejects_unknown_kind(self):
        db = Database(FakeConn())
        with self.assertRaises(QueryError):
            record_entry(db, 1, 10, 'credit', 'launder', 'nope')

    def test_html_escapes_description(self):
        html = entries_as_legacy_html([
            {'description': '<script>alert(1)</script>'},
        ])
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_serialize_tuple_rows(self):
        entry = serialize_entry((
            1, 9, 8, 12.5, 'debit', 'transfer', 'sent', 3, 87.5,
            datetime(2026, 1, 1, 0, 0, 0),
        ))
        self.assertEqual(entry['account_no'], 9)
        self.assertEqual(entry['direction'], 'debit')


if __name__ == '__main__':
    unittest.main()
