"""Append-only account ledger used by money-movement paths and history APIs."""

from datetime import datetime, timezone
from html import escape

from utility.db import Database, QueryError

LEDGER_KINDS = frozenset({
    'open',
    'bonus',
    'transfer',
    'deposit',
    'withdrawal',
    'credit',
    'cheque',
})

LEDGER_DIRECTIONS = frozenset({'debit', 'credit'})

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS AccountLedger (
    ledger_id INT NOT NULL AUTO_INCREMENT,
    account_no INT NOT NULL,
    counterpart_account INT NULL,
    amount FLOAT NOT NULL,
    direction ENUM('debit', 'credit') NOT NULL,
    kind VARCHAR(32) NOT NULL,
    description VARCHAR(255) NOT NULL,
    transaction_no INT NULL,
    balance_after FLOAT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (ledger_id),
    INDEX idx_ledger_account_created (account_no, created_at)
)
"""

_schema_ready = False


def ledger_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_ledger_table(db=None):
    """Idempotent schema helper so existing databases pick up the ledger."""
    global _schema_ready
    db = db or Database()
    db.execute(LEDGER_DDL)
    _schema_ready = True
    return db


def record_entry(
    db,
    account_no,
    amount,
    direction,
    kind,
    description,
    counterpart_account=None,
    transaction_no=None,
    balance_after=None,
    created_at=None,
):
    """Insert one parameterized ledger row. Returns lastrowid."""
    global _schema_ready
    if kind not in LEDGER_KINDS:
        raise QueryError('Unknown ledger kind: %r' % (kind,))
    if direction not in LEDGER_DIRECTIONS:
        raise QueryError('Unknown ledger direction: %r' % (direction,))
    if not _schema_ready:
        if getattr(db, '_in_tx', False):
            raise QueryError('AccountLedger is missing; call ensure_ledger_table() before money movement')
        ensure_ledger_table(db)
    created_at = created_at or ledger_now()
    _, lastrowid = db.execute(
        """
        INSERT INTO AccountLedger (
            account_no, counterpart_account, amount, direction, kind,
            description, transaction_no, balance_after, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            int(account_no),
            None if counterpart_account is None else int(counterpart_account),
            float(amount),
            direction,
            kind,
            str(description),
            None if transaction_no in (None, -1) else int(transaction_no),
            None if balance_after is None else float(balance_after),
            created_at,
        ),
    )
    return lastrowid


def list_entries(db, account_no, limit=100):
    """Newest-first ledger rows for one account, as JSON-serializable dicts."""
    global _schema_ready
    if not _schema_ready:
        ensure_ledger_table(db)
        _schema_ready = True
    limit = max(1, min(int(limit), 500))
    rows = db.fetch_all(
        """
        SELECT ledger_id, account_no, counterpart_account, amount, direction,
               kind, description, transaction_no, balance_after, created_at
        FROM AccountLedger
        WHERE account_no = %s
        ORDER BY created_at DESC, ledger_id DESC
        LIMIT %s
        """,
        (int(account_no), limit),
        dictionaries=True,
    )
    return [serialize_entry(row) for row in rows]


def serialize_entry(row):
    created = row.get('created_at') if isinstance(row, dict) else row[9]
    if hasattr(created, 'isoformat'):
        created = created.isoformat(sep=' ')
    if isinstance(row, dict):
        return {
            'ledger_id': row.get('ledger_id'),
            'account_no': row.get('account_no'),
            'counterpart_account': row.get('counterpart_account'),
            'amount': row.get('amount'),
            'direction': row.get('direction'),
            'kind': row.get('kind'),
            'description': row.get('description') or '',
            'transaction_no': row.get('transaction_no'),
            'balance_after': row.get('balance_after'),
            'created_at': created or '',
        }
    return {
        'ledger_id': row[0],
        'account_no': row[1],
        'counterpart_account': row[2],
        'amount': row[3],
        'direction': row[4],
        'kind': row[5],
        'description': row[6] or '',
        'transaction_no': row[7],
        'balance_after': row[8],
        'created_at': created or '',
    }


def entries_as_legacy_html(entries):
    """Plain-text HTML used by the old innerHTML history dump / PDF download."""
    parts = []
    for entry in entries:
        desc = escape(str(entry.get('description') or ''), quote=True)
        parts.append('%s,<br>' % desc)
    return ''.join(parts)
