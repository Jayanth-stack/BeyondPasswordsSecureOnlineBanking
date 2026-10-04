"""Card-network chargeback representment and merchant evidence packets.

Staff ingest incoming Visa / Mastercard / Amex / Discover chargebacks
(the bank as acquirer for a merchant customer). The merchant attaches
an evidence packet; staff submit representment. A later network win
credits the clawback back. Independent of in-app disputes (PR #58),
domestic Fedwire (PR #73), and ACH linking (PR #68). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.
`Customers.debit_request` / `credit_request` still write `debited` /
`direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- 23-digit Acquirer Reference Number (ARN) with Luhn
- Card-network reason codes (Visa / MC / Amex / Discover)
- BIN + last-4 only (never a full PAN)
- Merchant evidence packet compose / fingerprint
- VCR / Mastercom-style pipe file parse / compose (XML rejected)
- Per-network representment windows
- Dual-control release for high-value clawbacks
- OFAC-style cardholder screening via utility.wire

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full merchant account numbers and PAN never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    normalize_id,
    normalize_note,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

CHGBK_UNMATCHED = 'unmatched'
CHGBK_HELD = 'held'
CHGBK_PENDING = 'pending_release'
CHGBK_POSTED = 'posted'
CHGBK_EVIDENCE = 'evidence'
CHGBK_REPRESENTED = 'represented'
CHGBK_WON = 'won'
CHGBK_LOST = 'lost'
CHGBK_REJECTED = 'rejected'
CHGBK_EXPIRED = 'expired'
CHGBK_NSF = 'nsf'
CHGBK_FAILED = 'failed'
CHGBK_STATUSES = frozenset({
    CHGBK_UNMATCHED, CHGBK_HELD, CHGBK_PENDING, CHGBK_POSTED, CHGBK_EVIDENCE,
    CHGBK_REPRESENTED, CHGBK_WON, CHGBK_LOST, CHGBK_REJECTED, CHGBK_EXPIRED,
    CHGBK_NSF, CHGBK_FAILED,
})
OPEN_CASES = frozenset({
    CHGBK_UNMATCHED, CHGBK_HELD, CHGBK_PENDING, CHGBK_POSTED, CHGBK_EVIDENCE,
})
REPRESENTABLE = frozenset({CHGBK_POSTED, CHGBK_EVIDENCE})
ASSIGNABLE = frozenset({CHGBK_UNMATCHED})
OVERRIDABLE = frozenset({CHGBK_HELD})
RELEASABLE = frozenset({CHGBK_PENDING})
REJECTABLE = frozenset({CHGBK_UNMATCHED, CHGBK_HELD, CHGBK_PENDING, CHGBK_POSTED, CHGBK_EVIDENCE})
NETWORKS = frozenset({'visa', 'mastercard', 'amex', 'discover'})
NETWORK_ALIASES = {
    'visa': 'visa', 'vi': 'visa', 'vs': 'visa',
    'mastercard': 'mastercard', 'mc': 'mastercard', 'master': 'mastercard',
    'master_card': 'mastercard', 'master card': 'mastercard',
    'amex': 'amex', 'americanexpress': 'amex', 'american_express': 'amex',
    'american express': 'amex', 'ax': 'amex',
    'discover': 'discover', 'ds': 'discover', 'disc': 'discover',
}
REASONS = frozenset({
    'fraud', 'no_auth', 'not_as_described', 'not_received',
    'cancelled', 'credit_not_processed',
})
REASON_ALIASES = {
    'fraud': 'fraud', 'unauthorized': 'fraud', '10.4': 'fraud',
    '4837': 'fraud', '4541': 'fraud', '4752': 'fraud',
    'no_auth': 'no_auth', 'noauth': 'no_auth', 'no authorization': 'no_auth',
    '11.3': 'no_auth',
    'not_as_described': 'not_as_described', 'not as described': 'not_as_described',
    '12.5': 'not_as_described', '4853': 'not_as_described',
    'not_received': 'not_received', 'not received': 'not_received',
    'merchandise_not_received': 'not_received', '13.1': 'not_received',
    '4855': 'not_received', '4553': 'not_received', '4554': 'not_received',
    'cancelled': 'cancelled', 'canceled': 'cancelled', '13.2': 'cancelled',
    '4513': 'cancelled',
    'credit_not_processed': 'credit_not_processed',
    'credit not processed': 'credit_not_processed',
    '13.6': 'credit_not_processed', '4860': 'credit_not_processed',
}
NETWORK_REASON_CODES = {
    'visa': {
        'fraud': '10.4', 'no_auth': '11.3', 'not_as_described': '12.5',
        'not_received': '13.1', 'cancelled': '13.2', 'credit_not_processed': '13.6',
    },
    'mastercard': {
        'fraud': '4837', 'no_auth': '4837', 'not_as_described': '4853',
        'not_received': '4855', 'cancelled': '4853', 'credit_not_processed': '4860',
    },
    'amex': {
        'fraud': '4541', 'no_auth': '4541', 'not_as_described': '4553',
        'not_received': '4553', 'cancelled': '4513', 'credit_not_processed': '4513',
    },
    'discover': {
        'fraud': '4752', 'no_auth': '4752', 'not_as_described': '4554',
        'not_received': '4554', 'cancelled': '4554', 'credit_not_processed': '4554',
    },
}
WINDOW_DAYS = {
    'visa': 30,
    'mastercard': 45,
    'amex': 20,
    'discover': 20,
}
EVIDENCE_KINDS = frozenset({
    'receipt', 'avs', 'cvc', 'delivery', 'threeds', 'correspondence', 'refund', 'other',
})
EVIDENCE_ALIASES = {
    'receipt': 'receipt', 'invoice': 'receipt', 'slip': 'receipt',
    'avs': 'avs', 'address': 'avs',
    'cvc': 'cvc', 'cvv': 'cvc', 'cid': 'cvc',
    'delivery': 'delivery', 'shipping': 'delivery', 'pod': 'delivery',
    'threeds': 'threeds', '3ds': 'threeds', 'three_ds': 'threeds',
    'correspondence': 'correspondence', 'email': 'correspondence', 'letter': 'correspondence',
    'refund': 'refund', 'credit': 'refund',
    'other': 'other',
}
DEFAULT_ICA = '400000'
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/chgbk.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class ChgbkError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def luhn_checksum(digits: str) -> int:
    """Luhn residual. A valid number (including check digit) has residual 0."""
    if not digits or not str(digits).isdigit():
        return -1
    total = 0
    reversed_digits = str(digits)[::-1]
    for index, char in enumerate(reversed_digits):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10


def luhn_ok(digits: str) -> bool:
    return bool(digits) and str(digits).isdigit() and len(digits) >= 2 and luhn_checksum(digits) == 0


def luhn_check_digit(body: str) -> str:
    if not body or not str(body).isdigit():
        raise ChgbkError('invalid_arn', 'ARN body must be digits.')
    for candidate in '0123456789':
        if luhn_checksum(body + candidate) == 0:
            return candidate
    raise ChgbkError('invalid_arn', 'Could not compute Luhn check digit.')


def normalize_network(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    text = text.replace('__', '_')
    spaced = str(value or '').strip().lower()
    network = NETWORK_ALIASES.get(text) or NETWORK_ALIASES.get(spaced)
    if network not in NETWORKS:
        raise ChgbkError('invalid_network', 'Network must be visa, mastercard, amex, or discover.')
    return network


def normalize_reason(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_')
    text = ' '.join(text.split())
    reason = REASON_ALIASES.get(text) or REASON_ALIASES.get(text.replace(' ', '_'))
    if reason not in REASONS:
        raise ChgbkError('invalid_reason', 'Unknown chargeback reason.')
    return reason


def reason_code_for(network: str, reason: str) -> str:
    return NETWORK_REASON_CODES[network][reason]


def window_days_for(network: str) -> int:
    return WINDOW_DAYS[network]


def normalize_ica(value: Any, *, default: str = DEFAULT_ICA) -> str:
    digits = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if not digits:
        digits = default
    if len(digits) < 6:
        digits = digits.zfill(6)
    if len(digits) != 6 or digits == '000000':
        raise ChgbkError('invalid_ica', 'ICA must be six digits.')
    return digits


def normalize_bin(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 6:
        digits = digits.zfill(6) if digits else ''
    if len(digits) != 6 or digits == '000000':
        raise ChgbkError('invalid_bin', 'Card BIN must be six digits.')
    return digits


def normalize_last4(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        raise ChgbkError('invalid_card', 'Card last-4 is required.')
    return digits[-4:]


def normalize_merchant_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (4 <= len(digits) <= 17):
        raise ChgbkError('invalid_account', 'Merchant settlement account must be 4-17 digits.')
    return digits


def normalize_party_name(value: Any, *, field: str = 'name') -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise ChgbkError('invalid_name', '%s must be 2-80 characters.' % field.replace('_', ' ').title())
    return text


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise ChgbkError('invalid_currency', 'Chargebacks are USD only.')
    return text


def normalize_cycle_date(value: Any, *, now: Optional[float] = None) -> str:
    text = str(value or '').strip()
    if not text:
        stamp = datetime.fromtimestamp(float(now or datetime.now(timezone.utc).timestamp()), tz=timezone.utc)
        return stamp.date().isoformat()
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        parsed = date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        return parsed.isoformat()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ChgbkError('invalid_date', 'Chargeback date must be YYYY-MM-DD.') from exc
    return parsed.isoformat()


def add_days(day: str, count: int) -> str:
    parsed = date.fromisoformat(day[:10])
    return (parsed + timedelta(days=int(count))).isoformat()


def compose_arn(ica: Any, cycle_date: Any, sequence: int) -> str:
    """23-digit ARN: {6 ICA}{YYMMDD}{10 sequence}{Luhn}."""
    routing = normalize_ica(ica)
    day = normalize_cycle_date(cycle_date)
    compact = day.replace('-', '')
    body_date = compact[2:]
    seq = int(sequence)
    if seq < 1 or seq > 9_999_999_999:
        raise ChgbkError('invalid_arn', 'ARN sequence out of range.')
    body = '%s%s%010d' % (routing, body_date, seq)
    return body + luhn_check_digit(body)


def normalize_arn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 23 or not luhn_ok(digits):
        raise ChgbkError('invalid_arn', 'ARN must be a 23-digit Luhn number.')
    if digits == '0' * 23:
        raise ChgbkError('invalid_arn', 'ARN must be a 23-digit Luhn number.')
    return digits


def mask_arn(arn: str) -> str:
    digits = ''.join(ch for ch in str(arn) if ch.isdigit())
    if len(digits) < 8:
        return digits
    return digits[:6] + '*********' + digits[-8:]


def normalize_evidence_kind(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    kind = EVIDENCE_ALIASES.get(text)
    if kind not in EVIDENCE_KINDS:
        raise ChgbkError('invalid_evidence', 'Unknown evidence kind.')
    return kind


def evidence_fingerprint(kind: str, ref: Any, note: Any = '') -> str:
    material = '%s|%s|%s' % (kind, str(ref or '').strip(), str(note or '').strip())
    return hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]


def compose_packet_id(arn: str, items: Sequence['EvidenceItem']) -> str:
    material = arn + '|' + '|'.join(item.fingerprint for item in items)
    return 'PKT' + hashlib.sha256(material.encode('utf-8')).hexdigest()[:16].upper()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return parse_money(default, allow_zero=True)
    return parse_money(raw, allow_zero=True)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _classify_money_result(result: Any) -> str:
    if result in (None, True, 1):
        return 'ok'
    if isinstance(result, str):
        low = result.lower()
        if any(token in low for token in DEBIT_NSF):
            return 'nsf'
        if low in DEBIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def reject_markup(payload: str) -> None:
    low = str(payload or '').lower()
    if '<?xml' in low or '<!doctype' in low or '<chgbk' in low:
        raise ChgbkError('invalid_file', 'XML chargeback files are rejected.')


def compose_chgbk_record(message: 'ChargebackMessage') -> str:
    return '|'.join((
        'CHGBK',
        message.network.upper(),
        message.arn,
        message.currency,
        message.amount,
        message.reason_code,
        message.ica,
        message.card_bin,
        message.card_last4,
        message.merchant_account,
        message.cardholder.replace('|', ' '),
        message.merchant.replace('|', ' '),
        message.chargeback_date.replace('-', ''),
    ))


def compose_repmt_record(row: 'Chargeback') -> str:
    return '|'.join((
        'REPMT',
        row.network.upper(),
        row.arn,
        'USD',
        row.amount,
        row.reason_code,
        row.packet_id or 'PKT0',
        str(len(row.evidence)),
        datetime.fromtimestamp(row.represented_at or row.updated_at, tz=timezone.utc).strftime('%Y%m%d'),
    ))


def compose_vcr_file(messages: Sequence['ChargebackMessage']) -> str:
    return '\n'.join(compose_chgbk_record(item) for item in messages) + '\n'


def split_vcr_file(payload: str) -> List[str]:
    reject_markup(payload)
    lines = []
    for raw in str(payload or '').splitlines():
        text = raw.strip()
        if not text or text.startswith('#'):
            continue
        lines.append(text)
    if not lines:
        raise ChgbkError('invalid_file', 'Chargeback file is empty.')
    return lines


@dataclass
class ChargebackMessage:
    arn: str
    network: str
    reason: str
    reason_code: str
    amount: str
    currency: str
    ica: str
    card_bin: str
    card_last4: str
    merchant_account: str
    cardholder: str
    merchant: str
    chargeback_date: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'arn': self.arn,
            'arn_masked': mask_arn(self.arn),
            'network': self.network,
            'reason': self.reason,
            'reason_code': self.reason_code,
            'amount': self.amount,
            'currency': self.currency,
            'ica': self.ica,
            'card_bin': self.card_bin,
            'card_last4': self.card_last4,
            'account_last4': last4(self.merchant_account),
            'cardholder': self.cardholder,
            'merchant': self.merchant,
            'chargeback_date': self.chargeback_date,
        }


def message_from_values(values: Dict[str, Any], *, now: Optional[float] = None) -> ChargebackMessage:
    network = normalize_network(values.get('network') or 'visa')
    reason = normalize_reason(values.get('reason') or values.get('reason_code') or 'not_received')
    ica = normalize_ica(values.get('ica') or values.get('receiver') or DEFAULT_ICA)
    cycle = normalize_cycle_date(values.get('chargeback_date') or values.get('date'), now=now)
    raw_arn = values.get('arn')
    if raw_arn:
        arn = normalize_arn(raw_arn)
    else:
        sequence = int(values.get('sequence') or 1)
        arn = compose_arn(ica, cycle, sequence)
    amount = money_str(parse_money(values.get('amount')))
    return ChargebackMessage(
        arn=arn,
        network=network,
        reason=reason,
        reason_code=reason_code_for(network, reason),
        amount=amount,
        currency=normalize_currency(values.get('currency')),
        ica=ica,
        card_bin=normalize_bin(values.get('card_bin') or values.get('bin')),
        card_last4=normalize_last4(values.get('card_last4') or values.get('last4')),
        merchant_account=normalize_merchant_account(
            values.get('merchant_account') or values.get('account') or values.get('beneficiary_account'),
        ),
        cardholder=normalize_party_name(values.get('cardholder') or values.get('cardholder_name'), field='cardholder'),
        merchant=normalize_party_name(values.get('merchant') or values.get('merchant_name'), field='merchant'),
        chargeback_date=cycle,
    )


def parse_vcr_line(line: str, *, now: Optional[float] = None) -> ChargebackMessage:
    reject_markup(line)
    parts = [item.strip() for item in line.split('|')]
    if len(parts) < 13 or parts[0].upper() != 'CHGBK':
        raise ChgbkError('invalid_file', 'Chargeback record must be a CHGBK pipe line.')
    return message_from_values(
        {
            'network': parts[1],
            'arn': parts[2],
            'currency': parts[3],
            'amount': parts[4],
            'reason': parts[5],
            'ica': parts[6],
            'card_bin': parts[7],
            'card_last4': parts[8],
            'merchant_account': parts[9],
            'cardholder': parts[10],
            'merchant': parts[11],
            'chargeback_date': parts[12],
        },
        now=now,
    )


def parse_vcr_file(payload: str, *, now: Optional[float] = None) -> List[ChargebackMessage]:
    return [parse_vcr_line(line, now=now) for line in split_vcr_file(payload)]


@dataclass
class EvidenceItem:
    kind: str
    fingerprint: str
    note: str
    added_at: float
    actor: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind,
            'fingerprint': self.fingerprint,
            'note': self.note,
            'added_at': self.added_at,
            'actor': self.actor,
        }


def _evidence_from_payload(raw: Any) -> List[EvidenceItem]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    items = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        items.append(EvidenceItem(
            kind=str(item.get('kind') or 'other'),
            fingerprint=str(item.get('fingerprint') or ''),
            note=str(item.get('note') or ''),
            added_at=float(item.get('added_at') or 0),
            actor=str(item.get('actor') or ''),
        ))
    return items


@dataclass
class ChgbkPolicy:
    enabled: bool = True
    customer_evidence: bool = True
    customer_represent: bool = True
    allow_credit: bool = False
    max_cases: int = 240
    max_evidence: int = 12
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('100000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    receiver_ica: str = DEFAULT_ICA
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'ChgbkPolicy':
        extra = _env_list('CHGBK_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('CHGBK_ENABLED', True),
            customer_evidence=_env_bool('CHGBK_CUSTOMER_EVIDENCE', True),
            customer_represent=_env_bool('CHGBK_CUSTOMER_REPRESENT', True),
            allow_credit=_env_bool('CHGBK_ALLOW_CREDIT', False),
            max_cases=max(1, _env_int('CHGBK_MAX_CASES', 240)),
            max_evidence=max(1, _env_int('CHGBK_MAX_EVIDENCE', 12)),
            min_amount=_env_money('CHGBK_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('CHGBK_MAX_AMOUNT', '100000.00'),
            dual_control_threshold=_env_money('CHGBK_DUAL_CONTROL', '10000.00'),
            receiver_ica=normalize_ica(os.environ.get('CHGBK_ICA', DEFAULT_ICA)),
            watchlist=watch,
        )


@dataclass
class Chargeback:
    case_id: str
    arn: str
    userid: str
    merchant_account: str
    account_last4: str
    card_bin: str
    card_last4: str
    network: str
    reason: str
    reason_code: str
    amount: str
    ica: str
    cardholder: str
    merchant: str
    status: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    chargeback_date: str
    window_due: str
    created_at: float
    updated_at: float
    represented_at: float = 0.0
    resolved_at: float = 0.0
    packet_id: str = ''
    evidence: List[EvidenceItem] = field(default_factory=list)
    note: str = ''
    reason_note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'case_id': self.case_id,
            'arn': self.arn,
            'arn_masked': mask_arn(self.arn),
            'userid': self.userid,
            'account_last4': self.account_last4,
            'card_bin': self.card_bin,
            'card_last4': self.card_last4,
            'network': self.network,
            'reason': self.reason,
            'reason_code': self.reason_code,
            'amount': self.amount,
            'ica': self.ica,
            'cardholder': self.cardholder,
            'merchant': self.merchant,
            'status': self.status,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'chargeback_date': self.chargeback_date,
            'window_due': self.window_due,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'represented_at': self.represented_at,
            'resolved_at': self.resolved_at,
            'packet_id': self.packet_id,
            'evidence': [item.to_dict() for item in self.evidence],
            'evidence_count': len(self.evidence),
            'note': self.note,
            'reason_note': self.reason_note,
            'unmatched': self.status == CHGBK_UNMATCHED,
            'held': self.status == CHGBK_HELD,
            'pending_release': self.status == CHGBK_PENDING,
            'posted': self.status == CHGBK_POSTED,
            'representable': self.status in REPRESENTABLE,
            'open': self.status in OPEN_CASES,
        }


def _clone_case(row: Chargeback) -> Chargeback:
    cloned = Chargeback(**{key: getattr(row, key) for key in row.__dataclass_fields__})
    cloned.evidence = list(row.evidence)
    return cloned


def _case_from_row(row: Any) -> Chargeback:
    return Chargeback(
        case_id=row['case_id'],
        arn=row['arn'],
        userid=row['userid'] or '',
        merchant_account=row['merchant_account'],
        account_last4=row['account_last4'],
        card_bin=row['card_bin'],
        card_last4=row['card_last4'],
        network=row['network'],
        reason=row['reason'],
        reason_code=row['reason_code'],
        amount=row['amount'],
        ica=row['ica'],
        cardholder=row['cardholder'],
        merchant=row['merchant'],
        status=row['status'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        chargeback_date=row['chargeback_date'],
        window_due=row['window_due'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        represented_at=float(row['represented_at'] or 0),
        resolved_at=float(row['resolved_at'] or 0),
        packet_id=row['packet_id'] or '',
        evidence=_evidence_from_payload(row['evidence_json']),
        note=row['note'] or '',
        reason_note=row['reason_note'] or '',
    )


class MemoryChgbkStore:
    def __init__(self) -> None:
        self._cases: Dict[str, Chargeback] = {}
        self._by_arn: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_case(self, row: Chargeback) -> Chargeback:
        with self._lock:
            existing_id = self._by_arn.get(row.arn)
            if existing_id is not None:
                return _clone_case(self._cases[existing_id])
            self._cases[row.case_id] = row
            self._by_arn[row.arn] = row.case_id
            return row

    def update_case(self, row: Chargeback) -> None:
        with self._lock:
            self._cases[row.case_id] = row

    def get_case(self, case_id: str) -> Optional[Chargeback]:
        with self._lock:
            row = self._cases.get(case_id)
            return _clone_case(row) if row else None

    def get_case_by_arn(self, arn: str) -> Optional[Chargeback]:
        with self._lock:
            case_id = self._by_arn.get(arn)
            return _clone_case(self._cases[case_id]) if case_id else None

    def list_cases(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[Chargeback]:
        with self._lock:
            rows = [_clone_case(row) for row in self._cases.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if unmatched_only:
            rows = [row for row in rows if row.status == CHGBK_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows


class SqliteChgbkStore:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chargebacks (
                    case_id TEXT PRIMARY KEY,
                    arn TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    merchant_account TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    card_bin TEXT NOT NULL,
                    card_last4 TEXT NOT NULL,
                    network TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    ica TEXT NOT NULL,
                    cardholder TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    chargeback_date TEXT NOT NULL,
                    window_due TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    represented_at REAL NOT NULL DEFAULT 0,
                    resolved_at REAL NOT NULL DEFAULT 0,
                    packet_id TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    reason_note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def _row_tuple(self, row: Chargeback) -> Tuple[Any, ...]:
        return (
            row.case_id, row.arn, row.userid, row.merchant_account, row.account_last4,
            row.card_bin, row.card_last4, row.network, row.reason, row.reason_code,
            row.amount, row.ica, row.cardholder, row.merchant, row.status, row.actor,
            row.releaser, row.ofac_hit, row.ofac_match, row.chargeback_date, row.window_due,
            row.created_at, row.updated_at, row.represented_at, row.resolved_at, row.packet_id,
            json.dumps([item.to_dict() for item in row.evidence]), row.note, row.reason_note,
        )

    def put_case(self, row: Chargeback) -> Chargeback:
        with self._lock, self._connect() as conn:
            existing = conn.execute('SELECT * FROM chargebacks WHERE arn = ?', (row.arn,)).fetchone()
            if existing is not None:
                return _case_from_row(existing)
            conn.execute(
                """
                INSERT INTO chargebacks (
                    case_id, arn, userid, merchant_account, account_last4, card_bin,
                    card_last4, network, reason, reason_code, amount, ica, cardholder,
                    merchant, status, actor, releaser, ofac_hit, ofac_match,
                    chargeback_date, window_due, created_at, updated_at, represented_at,
                    resolved_at, packet_id, evidence_json, note, reason_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_tuple(row),
            )
            conn.commit()
            return row

    def update_case(self, row: Chargeback) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chargebacks SET userid=?, merchant_account=?, account_last4=?,
                    status=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, represented_at=?, resolved_at=?, packet_id=?,
                    evidence_json=?, note=?, reason_note=?
                WHERE case_id=?
                """,
                (
                    row.userid, row.merchant_account, row.account_last4, row.status,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.represented_at, row.resolved_at, row.packet_id,
                    json.dumps([item.to_dict() for item in row.evidence]),
                    row.note, row.reason_note, row.case_id,
                ),
            )
            conn.commit()

    def get_case(self, case_id: str) -> Optional[Chargeback]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM chargebacks WHERE case_id = ?', (case_id,)).fetchone()
        return _case_from_row(row) if row else None

    def get_case_by_arn(self, arn: str) -> Optional[Chargeback]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM chargebacks WHERE arn = ?', (arn,)).fetchone()
        return _case_from_row(row) if row else None

    def list_cases(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[Chargeback]:
        sql = 'SELECT * FROM chargebacks'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if unmatched_only:
            clauses.append("status = 'unmatched'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_case_from_row(row) for row in rows]


class ChgbkService:
    def __init__(
        self,
        policy: ChgbkPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        directory_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.directory_fn = directory_fn
        self.screen_fn = screen_fn

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise ChgbkError('chgbk_disabled', 'Chargeback representment is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise ChgbkError('chgbk_forbidden', 'Staff only.')

    def _require_evidence(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_evidence:
            raise ChgbkError('chgbk_forbidden', 'Customers cannot attach evidence.')

    def _require_represent(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_represent:
            raise ChgbkError('chgbk_forbidden', 'Customers cannot submit representment.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _lookup_owner(self, account: str) -> str:
        if self.directory_fn is None:
            return ''
        try:
            found = self.directory_fn(account)
        except Exception:
            return ''
        if found in (None, '', -1, '-1'):
            return ''
        return str(found).strip()

    def _assert_settlement_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise ChgbkError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise ChgbkError('credit_not_allowed', 'Credit accounts cannot receive chargeback clawbacks.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise ChgbkError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _today(self) -> str:
        return datetime.fromtimestamp(float(self.clock()), tz=timezone.utc).date().isoformat()

    def get_case(self, *, case_id: str, actor: str, actor_type: str) -> Chargeback:
        self._require_enabled()
        row = self.store.get_case(case_id)
        if row is None:
            raise ChgbkError('inbound_not_found', 'Chargeback not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise ChgbkError('chgbk_forbidden', 'Not allowed to view this chargeback.')
        return row

    def preview(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values, now=self.clock())
        dollars = parse_money(message.amount)
        self._assert_amount(dollars)
        ofac = self._screen(message.cardholder, aliases=(message.merchant,))
        return {
            'message': message.to_dict(),
            'window_due': add_days(message.chargeback_date, window_days_for(message.network)),
            'window_days': window_days_for(message.network),
            'dual_control': self._needs_dual_control(dollars),
            'ofac': ofac.to_dict(),
            'receiver_ok': message.ica == self.policy.receiver_ica,
        }

    def _debit(self, row: Chargeback) -> None:
        if self.debit_fn is None:
            return
        remark = 'chgbk %s' % row.arn[-12:]
        try:
            result = self.debit_fn(row.merchant_account, row.amount, remark)
        except Exception as exc:
            raise ChgbkError('failed', 'Chargeback debit failed.', case=row) from exc
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise ChgbkError('nsf', 'Insufficient funds for chargeback clawback.', case=row)
        if classified != 'ok':
            raise ChgbkError('failed', 'Chargeback debit failed.', case=row)

    def _credit(self, row: Chargeback, remark: str) -> None:
        if self.credit_fn is None:
            return
        try:
            result = self.credit_fn(row.merchant_account, row.amount, remark)
        except Exception as exc:
            raise ChgbkError('failed', 'Chargeback credit failed.', case=row) from exc
        if _classify_money_result(result) != 'ok':
            raise ChgbkError('failed', 'Chargeback credit failed.', case=row)

    def _evaluate(self, row: Chargeback, *, actor: str, skip_ofac: bool = False) -> Chargeback:
        now = float(self.clock())
        if row.ica != self.policy.receiver_ica:
            raise ChgbkError('wrong_receiver', 'Chargeback ICA is not this acquirer.')
        dollars = parse_money(row.amount)
        self._assert_amount(dollars)
        if not row.userid:
            row.status = CHGBK_UNMATCHED
            row.updated_at = now
            self.store.update_case(row)
            return row
        try:
            self._assert_settlement_account(row.userid, row.merchant_account)
        except ChgbkError as exc:
            if exc.code == 'credit_not_allowed':
                row.status = CHGBK_UNMATCHED
                row.reason_note = 'credit_not_allowed'
                row.updated_at = now
                self.store.update_case(row)
                return row
            raise
        if not skip_ofac:
            ofac = self._screen(row.cardholder, aliases=(row.merchant,))
            row.ofac_hit = 1 if ofac.hit else 0
            row.ofac_match = ofac.matched
            if ofac.hit:
                row.status = CHGBK_HELD
                row.updated_at = now
                self.store.update_case(row)
                return row
        else:
            row.ofac_hit = 0
            row.ofac_match = ''
        if self._needs_dual_control(dollars):
            row.status = CHGBK_PENDING
            row.updated_at = now
            self.store.update_case(row)
            return row
        self._debit(row)
        row.status = CHGBK_POSTED
        row.actor = str(actor)
        row.updated_at = now
        self.store.update_case(row)
        return row

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[Chargeback, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values, now=self.clock())
        if message.ica != self.policy.receiver_ica:
            raise ChgbkError('wrong_receiver', 'Chargeback ICA is not this acquirer.')
        existing = self.store.get_case_by_arn(message.arn)
        if existing is not None:
            return existing, False
        if len(self.store.list_cases()) >= self.policy.max_cases:
            raise ChgbkError('inbound_limit', 'Chargeback case limit reached.')
        owner = str(values.get('customer_id') or values.get('owner') or '').strip()
        account = message.merchant_account
        if values.get('internal_account'):
            account = normalize_account(values.get('internal_account'))
        if not owner:
            owner = self._lookup_owner(account)
        now = float(self.clock())
        row = Chargeback(
            case_id=uuid.uuid4().hex,
            arn=message.arn,
            userid=owner,
            merchant_account=account,
            account_last4=last4(account),
            card_bin=message.card_bin,
            card_last4=message.card_last4,
            network=message.network,
            reason=message.reason,
            reason_code=message.reason_code,
            amount=message.amount,
            ica=message.ica,
            cardholder=message.cardholder,
            merchant=message.merchant,
            status=CHGBK_UNMATCHED,
            actor=str(actor),
            releaser='',
            ofac_hit=0,
            ofac_match='',
            chargeback_date=message.chargeback_date,
            window_due=add_days(message.chargeback_date, window_days_for(message.network)),
            created_at=now,
            updated_at=now,
        )
        stored = self.store.put_case(row)
        if stored.case_id != row.case_id:
            return stored, False
        return self._evaluate(stored, actor=actor), True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        customer_id: str = '',
    ) -> List[Tuple[Chargeback, bool]]:
        messages = parse_vcr_file(payload, now=self.clock())
        results = []
        for message in messages:
            values = {
                'arn': message.arn,
                'network': message.network,
                'reason': message.reason,
                'amount': message.amount,
                'currency': message.currency,
                'ica': message.ica,
                'card_bin': message.card_bin,
                'card_last4': message.card_last4,
                'merchant_account': message.merchant_account,
                'cardholder': message.cardholder,
                'merchant': message.merchant,
                'chargeback_date': message.chargeback_date,
            }
            if customer_id:
                values['customer_id'] = customer_id
            results.append(self.ingest(actor=actor, actor_type=actor_type, values=values))
        return results

    def assign(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        internal_account: Any = None,
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in ASSIGNABLE:
            raise ChgbkError('not_assignable', 'Only unmatched chargebacks can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise ChgbkError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.merchant_account)
        self._assert_settlement_account(owner, account)
        row.userid = owner
        row.merchant_account = account
        row.account_last4 = last4(account)
        row.actor = str(actor)
        row.reason_note = ''
        return self._evaluate(row, actor=actor)

    def override_ofac(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in OVERRIDABLE:
            raise ChgbkError('not_overridable', 'Only OFAC-held chargebacks can be overridden.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        return self._evaluate(row, actor=actor, skip_ofac=True)

    def release(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in RELEASABLE:
            raise ChgbkError('not_releasable', 'Only pending chargebacks can be released.')
        if row.actor and str(actor) == str(row.actor):
            raise ChgbkError('same_approver', 'A different employee must release this chargeback.')
        self._debit(row)
        now = float(self.clock())
        row.status = CHGBK_POSTED
        row.releaser = str(actor)
        row.updated_at = now
        self.store.update_case(row)
        return row

    def reject(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in REJECTABLE:
            raise ChgbkError('not_rejectable', 'Chargeback cannot be rejected.')
        now = float(self.clock())
        row.status = CHGBK_REJECTED
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        row.updated_at = now
        row.resolved_at = now
        self.store.update_case(row)
        return row

    def add_evidence(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        kind: Any,
        ref: Any,
        note: Any = '',
    ) -> Chargeback:
        self._require_evidence(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in REPRESENTABLE:
            raise ChgbkError('not_representable', 'Evidence can only be added before representment.')
        if self._today() > row.window_due:
            raise ChgbkError('represent_window_closed', 'Representment window is closed.')
        if len(row.evidence) >= self.policy.max_evidence:
            raise ChgbkError('evidence_limit', 'Evidence packet is full.')
        item_kind = normalize_evidence_kind(kind)
        fingerprint = evidence_fingerprint(item_kind, ref, note)
        if any(item.fingerprint == fingerprint for item in row.evidence):
            raise ChgbkError('evidence_duplicate', 'That evidence item is already on the packet.')
        row.evidence.append(EvidenceItem(
            kind=item_kind,
            fingerprint=fingerprint,
            note=normalize_note(note, limit=160),
            added_at=float(self.clock()),
            actor=str(actor),
        ))
        row.packet_id = compose_packet_id(row.arn, row.evidence)
        row.status = CHGBK_EVIDENCE
        row.updated_at = float(self.clock())
        self.store.update_case(row)
        return row

    def represent(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_represent(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in REPRESENTABLE:
            raise ChgbkError('not_representable', 'Only posted chargebacks with an open window can be represented.')
        if self._today() > row.window_due:
            raise ChgbkError('represent_window_closed', 'Representment window is closed.')
        if not row.evidence:
            raise ChgbkError('missing_evidence', 'At least one evidence item is required.')
        now = float(self.clock())
        row.packet_id = compose_packet_id(row.arn, row.evidence)
        row.status = CHGBK_REPRESENTED
        row.represented_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_case(row)
        return row

    def accept_liability(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_enabled()
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status not in REPRESENTABLE:
            raise ChgbkError('not_acceptable', 'Only an open posted chargeback can be accepted.')
        now = float(self.clock())
        row.status = CHGBK_LOST
        row.resolved_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or 'merchant accepted liability'
        self.store.update_case(row)
        return row

    def record_win(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status != CHGBK_REPRESENTED:
            raise ChgbkError('not_winnable', 'Only represented chargebacks can be marked won.')
        self._credit(row, 'chgbk win %s' % row.arn[-12:])
        now = float(self.clock())
        row.status = CHGBK_WON
        row.resolved_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_case(row)
        return row

    def record_loss(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Chargeback:
        self._require_staff(actor_type)
        row = self.get_case(case_id=case_id, actor=actor, actor_type=actor_type)
        if row.status != CHGBK_REPRESENTED:
            raise ChgbkError('not_losable', 'Only represented chargebacks can be marked lost.')
        now = float(self.clock())
        row.status = CHGBK_LOST
        row.resolved_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_case(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[Chargeback]:
        today = self._today()
        changed: List[Chargeback] = []
        for row in self.store.list_cases(userid):
            if row.status not in REPRESENTABLE:
                continue
            if today <= row.window_due:
                continue
            row.status = CHGBK_EXPIRED
            row.resolved_at = float(self.clock())
            row.updated_at = row.resolved_at
            self.store.update_case(row)
            changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        self.run_due(userid)
        cases = self.store.list_cases(userid)
        posted = Decimal('0.00')
        won = Decimal('0.00')
        lost = Decimal('0.00')
        for row in cases:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {CHGBK_POSTED, CHGBK_EVIDENCE, CHGBK_REPRESENTED, CHGBK_LOST, CHGBK_EXPIRED}:
                posted += amount
            if row.status == CHGBK_WON:
                posted += amount
                won += amount
            if row.status in {CHGBK_LOST, CHGBK_EXPIRED}:
                lost += amount
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'receiver_ica': self.policy.receiver_ica,
            'windows': dict(WINDOW_DAYS),
            'cases': [row.to_dict() for row in cases[:40]],
            'ytd_clawed': money_str(posted),
            'ytd_won': money_str(won),
            'ytd_lost': money_str(lost),
            'open_count': sum(1 for row in cases if row.status in OPEN_CASES or row.status == CHGBK_REPRESENTED),
            'unmatched_count': sum(1 for row in cases if row.status == CHGBK_UNMATCHED),
        }


_SERVICE: Optional[ChgbkService] = None


def set_service(service: Optional[ChgbkService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[ChgbkService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CHGBK_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryChgbkStore()
    path = os.environ.get('CHGBK_DB', DEFAULT_STORE_PATH)
    return SqliteChgbkStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    directory_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
) -> ChgbkService:
    if store is None:
        store = default_store()
    return ChgbkService(
        ChgbkPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        directory_fn=directory_fn,
        screen_fn=screen_fn,
    )


def _session_userid() -> Optional[str]:
    return session.get('userid')


def _require_session_user() -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    userid = _session_userid()
    if not userid:
        return None, (jsonify({'message': 'Unauthorized access or session expired'}), 401)
    values = request.get_json(silent=True) or {}
    claimed = values.get('userid')
    if claimed is not None and str(claimed) != str(userid):
        return None, (jsonify({'message': 'User ID mismatch', 'error': 'userid_mismatch'}), 403)
    return str(userid), None


def _error_status(code: str) -> int:
    return {
        'inbound_limit': 409,
        'already_returned': 409,
        'evidence_limit': 409,
        'evidence_duplicate': 409,
        'nsf': 409,
        'failed': 409,
        'chgbk_forbidden': 403,
        'chgbk_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_representable': 403,
        'not_acceptable': 403,
        'not_winnable': 403,
        'not_losable': 403,
        'represent_window_closed': 403,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_arn': 400,
        'invalid_bin': 400,
        'invalid_card': 400,
        'invalid_ica': 400,
        'invalid_network': 400,
        'invalid_reason': 400,
        'invalid_currency': 400,
        'invalid_date': 400,
        'invalid_name': 400,
        'invalid_file': 400,
        'invalid_evidence': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
        'missing_evidence': 400,
    }.get(code, 400)


def _error_body(exc: ChgbkError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    case = exc.extra.get('case')
    if case is not None:
        body['case'] = case.to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except ChgbkError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_payload(service: ChgbkService, owner: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=actor, actor_type=actor_type)


def handle_list(service: ChgbkService, *, unmatched_only: bool = False):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    if unmatched_only:
        if actor_type not in EMPLOYEE_ROLES:
            return jsonify({'message': 'Staff only', 'error': 'chgbk_forbidden'}), 403
        cases = [row.to_dict() for row in service.store.list_cases(unmatched_only=True)[:40]]
        return jsonify({'Chargebacks': {'cases': cases, 'unmatched_count': len(cases)}}), 200
    return jsonify({'Chargebacks': _snapshot_payload(service, owner, userid, actor_type)}), 200


def handle_preview(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'chgbk_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        return jsonify({'preview': service.preview(values)}), 200

    return _handle_errors(_run)


def handle_ingest(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Chargeback ingested' if created else 'Chargeback already posted',
            'case': row.to_dict(),
            'Chargebacks': _snapshot_payload(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('contents') or ''
    uploaded = request.files.get('file') if request.files else None
    if uploaded is not None:
        payload = uploaded.read().decode('utf-8', errors='replace')
    if not str(payload).strip():
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400

    def _run():
        results = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            payload=str(payload),
            customer_id=str(values.get('customer_id') or ''),
        )
        first, created = results[0]
        owner = first.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Chargeback file ingested',
            'count': len(results),
            'created': sum(1 for _row, was_new in results if was_new),
            'case': first.to_dict(),
            'Chargebacks': _snapshot_payload(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def _case_id_from_request() -> Tuple[str, Optional[Tuple[Any, int]]]:
    values = request.get_json(silent=True) or {}
    case_id = str(values.get('case_id') or values.get('inbound') or '').strip()
    if not case_id:
        return '', (jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400)
    return case_id, None


def _staff_case_route(service: ChgbkService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    case_id, missing = _case_id_from_request()
    if missing:
        return missing
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'assign':
            row = service.assign(
                case_id=case_id, actor=userid, actor_type=actor_type,
                customer_id=values.get('customer_id') or values.get('owner'),
                internal_account=values.get('account') or values.get('internal_account'),
            )
            message = 'Chargeback assigned'
        elif action == 'override':
            row = service.override_ofac(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(case_id=case_id, actor=userid, actor_type=actor_type)
            message = 'Chargeback released'
        elif action == 'reject':
            row = service.reject(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Chargeback rejected'
        elif action == 'represent':
            row = service.represent(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Representment submitted'
        elif action == 'win':
            row = service.record_win(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Chargeback won'
        elif action == 'loss':
            row = service.record_loss(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Chargeback lost'
        else:
            raise ChgbkError('invalid_reason', 'Unknown chargeback action.')
        return jsonify({
            'message': message,
            'case': row.to_dict(),
            'Chargebacks': _snapshot_payload(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_evidence(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    case_id, missing = _case_id_from_request()
    if missing:
        return missing
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.add_evidence(
            case_id=case_id,
            actor=userid,
            actor_type=actor_type,
            kind=values.get('kind') or values.get('evidence_kind'),
            ref=values.get('ref') or values.get('document') or values.get('reference'),
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Evidence attached',
            'case': row.to_dict(),
            'Chargebacks': _snapshot_payload(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_represent(service: ChgbkService):
    return _staff_case_route(service, 'represent')


def handle_accept(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    case_id, missing = _case_id_from_request()
    if missing:
        return missing
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.accept_liability(
            case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Liability accepted',
            'case': row.to_dict(),
            'Chargebacks': _snapshot_payload(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: ChgbkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Chargebacks': _snapshot_payload(service, owner, userid, actor_type)}), 200


def attach_chgbk_routes(app, service: ChgbkService) -> None:
    @app.route('/listChgbks', methods=['POST', 'GET'])
    def list_chgbks_route():
        return handle_list(service)

    @app.route('/listUnmatchedChgbks', methods=['POST', 'GET'])
    def list_unmatched_chgbks_route():
        return handle_list(service, unmatched_only=True)

    @app.route('/previewChgbk', methods=['POST', 'GET'])
    def preview_chgbk_route():
        return handle_preview(service)

    @app.route('/ingestChgbk', methods=['POST', 'GET'])
    def ingest_chgbk_route():
        return handle_ingest(service)

    @app.route('/ingestChgbkFile', methods=['POST', 'GET'])
    def ingest_chgbk_file_route():
        return handle_ingest_file(service)

    @app.route('/assignChgbk', methods=['POST', 'GET'])
    def assign_chgbk_route():
        return _staff_case_route(service, 'assign')

    @app.route('/overrideChgbkOfac', methods=['POST', 'GET'])
    def override_chgbk_ofac_route():
        return _staff_case_route(service, 'override')

    @app.route('/releaseChgbk', methods=['POST', 'GET'])
    def release_chgbk_route():
        return _staff_case_route(service, 'release')

    @app.route('/rejectChgbk', methods=['POST', 'GET'])
    def reject_chgbk_route():
        return _staff_case_route(service, 'reject')

    @app.route('/addChgbkEvidence', methods=['POST', 'GET'])
    def add_chgbk_evidence_route():
        return handle_add_evidence(service)

    @app.route('/representChgbk', methods=['POST', 'GET'])
    def represent_chgbk_route():
        return handle_represent(service)

    @app.route('/acceptChgbk', methods=['POST', 'GET'])
    def accept_chgbk_route():
        return handle_accept(service)

    @app.route('/recordChgbkWin', methods=['POST', 'GET'])
    def record_chgbk_win_route():
        return _staff_case_route(service, 'win')

    @app.route('/recordChgbkLoss', methods=['POST', 'GET'])
    def record_chgbk_loss_route():
        return _staff_case_route(service, 'loss')

    @app.route('/runDueChgbks', methods=['POST', 'GET'])
    def run_due_chgbks_route():
        return handle_run_due(service)
