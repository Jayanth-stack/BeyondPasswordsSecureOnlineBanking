"""Reusable account-authorization for money-moving operations.

Routes must not decide ownership themselves. They ask this service whether the
session principal may act on an account for a given purpose (debit, credit,
transfer_from, issue_cheque, ...). The policy is role-aware:

- Customers may mutate only accounts they own. Existence of other customers'
  accounts is not revealed (same 403 as a not-owned account).
- Tellers (employee / tier1 / tier2 / admin) may operate on any existing
  account, which is how the existing teller dashboards work.

Lookups are parameterized and go through an injectable repository so the
policy can be tested without MySQL.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

logger = logging.getLogger(__name__)

TELLER_TYPES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
CUSTOMER_TYPES = frozenset({'customer'})

# What the policy requires of the target account for each purpose.
# own_active      — caller must own it and it must be active
# own             — caller must own it (active or not)
# exists_active   — account must exist and be active
# exists          — account must exist
PURPOSE_RULES = {
    'customer': {
        'transfer_from': 'own_active',
        'transfer_to': 'exists',
        'debit': 'own_active',
        'credit': 'own_active',
        'request_from': 'exists',
        'request_to': 'own_active',
        'issue_cheque_from': 'own_active',
        'issue_cheque_to': 'exists',
        'read': 'own',
    },
    'teller': {
        'transfer_from': 'exists_active',
        'transfer_to': 'exists',
        'debit': 'exists_active',
        'credit': 'exists_active',
        'request_from': 'exists',
        'request_to': 'exists_active',
        'issue_cheque_from': 'exists_active',
        'issue_cheque_to': 'exists',
        'read': 'exists',
    },
}


class AccountStoreUnavailable(Exception):
    """Repository could not be reached."""


@dataclass(frozen=True)
class AccountRecord:
    account_no: int
    customer_id: str
    account_type: str
    active: bool
    balance: float = 0.0


@dataclass(frozen=True)
class ChequeRecord:
    cheque_no: int
    issuer_id: str
    to_account: int
    from_account: int
    amount: float
    active: bool


@dataclass
class AccessDecision:
    allowed: bool
    status_code: int = 200
    message: str = 'ok'
    error: str = ''
    account: Optional[AccountRecord] = None
    cheque: Optional[ChequeRecord] = None

    def as_json(self) -> Dict[str, str]:
        payload = {'message': self.message}
        if self.error:
            payload['error'] = self.error
        return payload


def deny(status_code: int, message: str, error: str) -> AccessDecision:
    return AccessDecision(False, status_code, message, error)


def allow(account: Optional[AccountRecord] = None,
          cheque: Optional[ChequeRecord] = None) -> AccessDecision:
    return AccessDecision(True, 200, 'ok', '', account, cheque)


class AccountRepository(Protocol):
    def fetch_account(self, account_no: int) -> Optional[AccountRecord]:
        ...

    def list_accounts(self, customer_id: str) -> list[AccountRecord]:
        ...

    def fetch_cheque(self, cheque_no: int) -> Optional[ChequeRecord]:
        ...


class MemoryAccountRepository:
    """In-memory backend for tests and for injecting fixtures."""

    def __init__(self,
                 accounts: Optional[list[AccountRecord]] = None,
                 cheques: Optional[list[ChequeRecord]] = None):
        self.accounts: Dict[int, AccountRecord] = {
            a.account_no: a for a in (accounts or [])
        }
        self.cheques: Dict[int, ChequeRecord] = {
            c.cheque_no: c for c in (cheques or [])
        }

    def fetch_account(self, account_no: int) -> Optional[AccountRecord]:
        return self.accounts.get(account_no)

    def list_accounts(self, customer_id: str) -> list[AccountRecord]:
        return [a for a in self.accounts.values() if a.customer_id == customer_id]

    def fetch_cheque(self, cheque_no: int) -> Optional[ChequeRecord]:
        return self.cheques.get(cheque_no)

    def add_account(self, account: AccountRecord) -> None:
        self.accounts[account.account_no] = account

    def add_cheque(self, cheque: ChequeRecord) -> None:
        self.cheques[cheque.cheque_no] = cheque


def _row_to_account(row) -> AccountRecord:
    return AccountRecord(
        account_no=int(row[0]),
        customer_id=str(row[1]),
        account_type=str(row[2]),
        active=bool(row[3]),
        balance=float(row[4] if row[4] is not None else 0),
    )


@contextmanager
def _mysql_cursor():
    import mysql.connector
    try:
        conn = mysql.connector.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            user=os.getenv('DB_USER', 'root'),
            password=os.getenv('DB_PASSWORD', 'root'),
            port=int(os.getenv('DB_PORT', '3306')),
            database=os.getenv('DB_NAME', 'bankingapplication'),
        )
    except Exception as exc:
        raise AccountStoreUnavailable(str(exc)) from exc
    cursor = None
    try:
        cursor = conn.cursor()
        yield cursor
    except AccountStoreUnavailable:
        raise
    except Exception as exc:
        raise AccountStoreUnavailable(str(exc)) from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


class MysqlAccountRepository:
    """Parameterized lookups against the existing Accounts / Cheque tables."""

    def fetch_account(self, account_no: int) -> Optional[AccountRecord]:
        with _mysql_cursor() as cursor:
            cursor.execute(
                "SELECT account_no, customer_id, account_type, active, balance "
                "FROM Accounts WHERE account_no = %s",
                (account_no,),
            )
            row = cursor.fetchone()
        return None if not row else _row_to_account(row)

    def list_accounts(self, customer_id: str) -> list[AccountRecord]:
        with _mysql_cursor() as cursor:
            cursor.execute(
                "SELECT account_no, customer_id, account_type, active, balance "
                "FROM Accounts WHERE customer_id = %s",
                (customer_id,),
            )
            rows = cursor.fetchall()
        return [_row_to_account(row) for row in rows]

    def fetch_cheque(self, cheque_no: int) -> Optional[ChequeRecord]:
        with _mysql_cursor() as cursor:
            cursor.execute(
                "SELECT cheque_no, issuer_id, to_account, from_account, amount, active "
                "FROM Cheque WHERE cheque_no = %s",
                (cheque_no,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return ChequeRecord(
            cheque_no=int(row[0]),
            issuer_id=str(row[1] or ''),
            to_account=int(row[2]),
            from_account=int(row[3]),
            amount=float(row[4]),
            active=bool(row[5]),
        )


def parse_account_no(value) -> int:
    try:
        account_no = int(value)
    except (TypeError, ValueError):
        raise ValueError('Invalid account number')
    if account_no <= 0:
        raise ValueError('Invalid account number')
    return account_no


def parse_cheque_no(value) -> int:
    try:
        cheque_no = int(value)
    except (TypeError, ValueError):
        raise ValueError('Invalid cheque number')
    if cheque_no <= 0:
        raise ValueError('Invalid cheque number')
    return cheque_no


def _role_for(session: dict) -> Optional[str]:
    userid = session.get('userid')
    usertype = session.get('usertype')
    if not userid:
        return None
    if usertype in TELLER_TYPES:
        return 'teller'
    if usertype in CUSTOMER_TYPES or usertype is None:
        # Login sets usertype='customer'. Treat a logged-in userid without a
        # recognised teller type as a customer so we fail closed for teller ops.
        return 'customer'
    return 'customer'


class AccountAccess:
    def __init__(self, repository: AccountRepository):
        self.repository = repository

    def customer_owns(self, customer_id: str, account_no: int) -> bool:
        account = self.repository.fetch_account(account_no)
        return bool(account and account.customer_id == customer_id)

    def list_owned_accounts(self, customer_id: str) -> list[AccountRecord]:
        return self.repository.list_accounts(customer_id)

    def authorize(self, session: dict, account_no, purpose: str) -> AccessDecision:
        role = _role_for(session)
        if role is None:
            return deny(401, 'Unauthorized access or session expired', 'unauthenticated')

        try:
            parsed = parse_account_no(account_no)
        except ValueError:
            return deny(400, 'Invalid account number', 'invalid_account')

        rules = PURPOSE_RULES.get(role, {})
        rule = rules.get(purpose)
        if rule is None:
            return deny(400, 'Unknown authorization purpose', 'unknown_purpose')

        try:
            account = self.repository.fetch_account(parsed)
        except AccountStoreUnavailable as exc:
            logger.error('account store unavailable during authorize: %s', exc)
            return deny(503, 'Account service unavailable', 'store_unavailable')

        userid = session.get('userid')
        owns = bool(account and account.customer_id == userid)

        if account is None:
            if role == 'customer':
                # Do not confirm whether the account number exists.
                logger.warning(
                    'account_access denied userid=%s purpose=%s account=%s reason=not_found',
                    userid, purpose, parsed,
                )
                return deny(403, 'Not authorized to operate on this account', 'account_not_owned')
            return deny(404, 'Account not found', 'account_not_found')

        if rule in ('own_active', 'own') and not owns:
            logger.warning(
                'account_access denied userid=%s purpose=%s account=%s owner=%s',
                userid, purpose, parsed, account.customer_id,
            )
            return deny(403, 'Not authorized to operate on this account', 'account_not_owned')

        if rule in ('own_active', 'exists_active') and not account.active:
            logger.warning(
                'account_access denied userid=%s purpose=%s account=%s reason=inactive',
                userid, purpose, parsed,
            )
            return deny(403, 'Account is not active', 'account_inactive')

        return allow(account)

    def authorize_cheque_credit(self, session: dict, cheque_no) -> AccessDecision:
        """Authorize depositing a cheque into its destination account.

        Missing / already-used cheques are left to the existing deposit path so
        those response strings stay unchanged. Ownership is enforced only when
        the cheque can be resolved.
        """
        role = _role_for(session)
        if role is None:
            return deny(401, 'Unauthorized access or session expired', 'unauthenticated')

        try:
            parsed = parse_cheque_no(cheque_no)
        except ValueError:
            return deny(400, 'Invalid cheque number', 'invalid_cheque')

        try:
            cheque = self.repository.fetch_cheque(parsed)
        except AccountStoreUnavailable as exc:
            logger.error('account store unavailable during cheque authorize: %s', exc)
            return deny(503, 'Account service unavailable', 'store_unavailable')

        if cheque is None:
            return allow()

        decision = self.authorize(session, cheque.to_account, 'credit')
        decision.cheque = cheque
        return decision


_access: Optional[AccountAccess] = None


def get_access() -> AccountAccess:
    global _access
    if _access is None:
        _access = AccountAccess(MysqlAccountRepository())
    return _access


def set_access(access: Optional[AccountAccess]) -> None:
    global _access
    _access = access


def authorize_account(session, account_no, purpose: str) -> AccessDecision:
    return get_access().authorize(session, account_no, purpose)


def authorize_cheque_credit(session, cheque_no) -> AccessDecision:
    return get_access().authorize_cheque_credit(session, cheque_no)


def flask_error(decision: AccessDecision):
    """Return a Flask (response, status) tuple when denied, else None."""
    if decision.allowed:
        return None
    from flask import jsonify
    return jsonify(decision.as_json()), decision.status_code
