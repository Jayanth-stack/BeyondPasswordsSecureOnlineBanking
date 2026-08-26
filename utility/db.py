"""Shared MySQL access: env-based connections and parameterized queries only.

Callers pass values as a sequence of bind parameters. Do not interpolate
user input into SQL strings — `%s` placeholders are required for values.
"""

import os
import re
from contextlib import contextmanager

from dotenv import load_dotenv
import mysql.connector

load_dotenv()

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_PLACEHOLDER = re.compile(r'(?<!%)%s')


class QueryError(ValueError):
    """Invalid SQL, identifiers, or bind parameters."""


def db_config():
    """Shared MySQL connection settings from the environment."""
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', 'root'),
        'port': os.getenv('DB_PORT', '3306'),
        'database': os.getenv('DB_NAME', 'bankingapplication'),
    }


def get_connection():
    """Single connection factory used by customer, employee, and schema scripts."""
    return mysql.connector.connect(**db_config())


def quote_ident(name):
    """Quote a SQL identifier after rejecting anything that is not a simple name."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise QueryError('Invalid SQL identifier: %r' % (name,))
    return '`%s`' % name


def bind_params(sql, params=()):
    """Normalize bind parameters and reject string interpolation mistakes."""
    if not isinstance(sql, str) or not sql.strip():
        raise QueryError('SQL must be a non-empty string')
    if params is None:
        params = ()
    if isinstance(params, (str, bytes)):
        raise QueryError('params must be a sequence of values, not a single string')
    if isinstance(params, dict):
        raise QueryError('named parameters are not supported; use positional %s placeholders')
    try:
        params = tuple(params)
    except TypeError:
        raise QueryError('params must be a sequence of values')
    placeholders = len(_PLACEHOLDER.findall(sql))
    if placeholders != len(params):
        raise QueryError(
            'SQL has %d %%s placeholder(s) but %d param(s) were supplied'
            % (placeholders, len(params))
        )
    return sql, params


class Database:
    """Thin wrapper around a MySQL connection that only runs parameterized SQL."""

    def __init__(self, conn=None):
        self._conn = conn
        self._owns_connection = conn is None
        self._in_tx = False

    @property
    def conn(self):
        if self._conn is None:
            self._conn = get_connection()
        return self._conn

    def _cursor(self, dictionary=False):
        if dictionary:
            return self.conn.cursor(dictionary=True)
        return self.conn.cursor()

    def fetch_one(self, sql, params=(), dictionaries=False):
        sql, params = bind_params(sql, params)
        cursor = self._cursor(dictionary=dictionaries)
        try:
            cursor.execute(sql, params)
            return cursor.fetchone()
        finally:
            cursor.close()

    def fetch_all(self, sql, params=(), dictionaries=False):
        sql, params = bind_params(sql, params)
        cursor = self._cursor(dictionary=dictionaries)
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def execute(self, sql, params=(), commit=None):
        sql, params = bind_params(sql, params)
        if commit is None:
            commit = not self._in_tx
        cursor = self._cursor()
        try:
            cursor.execute(sql, params)
            lastrowid = cursor.lastrowid
            rowcount = cursor.rowcount
            if commit:
                self.conn.commit()
            return rowcount, lastrowid
        except Exception:
            self.conn.rollback()
            self._in_tx = False
            raise
        finally:
            cursor.close()

    @contextmanager
    def transaction(self):
        if self._in_tx:
            yield self
            return
        self.conn.start_transaction()
        self._in_tx = True
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._in_tx = False

    def close(self):
        if self._owns_connection and self._conn is not None:
            self._conn.close()
            self._conn = None
