"""Inbound ACH / NACHA receive posting from an operator file.

Staff ingest incoming NACHA credits (standard T+1 or Same-Day ACH) and
credit the beneficiary customer. Independent of outbound Fedwire (PR #73),
ACH linking / micro-deposits (PR #68), bill-pay outgoing ACH (PR #66),
inbound payroll splits (PR #64), and the unmerged inbound Fedwire/RTP/UK
PRs. Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- NACHA 94-byte record parse / compose / file split (types 1/5/6/7/8/9)
- Incoming 15-digit ACH trace uniqueness
- Receiver-ABA (RDFI) acceptance (this bank)
- Account-directory lookup (DFI account → customer)
- Incoming credit posting + Nacha return (txn 21/31 + addenda 99)
- Fed ACH business-day / Same-Day 16:45 ET cutoff (reused WireCalendar)
- OFAC-style originator screening (reused)
- Dual-control release for high-value inbound credits

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Originator and beneficiary account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    WireCalendar,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_external_account,
    normalize_id,
    normalize_legal_name,
    normalize_note,
    normalize_party,
    normalize_purpose,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

IN_HELD = 'held'
IN_UNMATCHED = 'unmatched'
IN_QUEUED = 'queued'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})
RAIL_STANDARD = 'standard'
RAIL_SAMEDAY = 'sameday'
RAILS = frozenset({RAIL_STANDARD, RAIL_SAMEDAY})
RAIL_ALIASES = {
    'standard': RAIL_STANDARD, 'std': RAIL_STANDARD, 'next_day': RAIL_STANDARD,
    'nextday': RAIL_STANDARD, 't+1': RAIL_STANDARD, 't1': RAIL_STANDARD,
    'sameday': RAIL_SAMEDAY, 'same_day': RAIL_SAMEDAY, 'same-day': RAIL_SAMEDAY,
    'sda': RAIL_SAMEDAY, 'same': RAIL_SAMEDAY,
}
SEC_CODES = frozenset({'PPD', 'CCD', 'WEB', 'CTX', 'TEL', 'IAT', 'CIE', 'POP', 'POS'})
CREDIT_TXN = frozenset({'22', '32', '42', '52'})
RETURN_TXN = {'22': '21', '32': '31', '42': '41', '52': '51'}
DEBIT_TXN = frozenset({
    '26', '27', '28', '29', '36', '37', '38', '39',
    '46', '47', '48', '49', '55', '56', '57', '58', '59',
})
RETURN_REASONS = frozenset({
    'R01', 'R02', 'R03', 'R04', 'R08', 'R10', 'R16', 'R17', 'R23', 'R29',
})
RETURN_ALIASES = {
    'r01': 'R01', 'nsf': 'R01', 'insufficient': 'R01',
    'r02': 'R02', 'closed': 'R02',
    'r03': 'R03', 'acct': 'R03', 'account': 'R03', 'unknown': 'R03', 'no_account': 'R03',
    'r04': 'R04', 'invalid': 'R04',
    'r08': 'R08', 'stop': 'R08',
    'r10': 'R10', 'unauth': 'R10', 'unauthorized': 'R10',
    'r16': 'R16', 'freeze': 'R16', 'frozen': 'R16', 'ofac': 'R16',
    'r17': 'R17', 'dup': 'R17', 'duplicate': 'R17', 'edit': 'R17',
    'r23': 'R23', 'cust': 'R23', 'customer': 'R23', 'refused': 'R23', 'other': 'R23',
    'r29': 'R29', 'corp': 'R29',
}
DEFAULT_STORE_PATH = 'SystemLogs/inach.sqlite'
DEFAULT_RECEIVER_ABA = '021000021'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class InAchError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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
        if any(token in low for token in CREDIT_NSF):
            return 'nsf'
        if low in CREDIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def _pad(text: Any, width: int, *, right: bool = False, fill: str = ' ') -> str:
    raw = str(text or '')
    if len(raw) > width:
        raw = raw[:width]
    if right:
        return raw.rjust(width, fill)
    return raw.ljust(width, fill)


def _fit94(line: str) -> str:
    text = line.replace('\r', '').replace('\n', '')
    if len(text) < 94:
        return text.ljust(94)
    return text[:94]


def _slice(record: str, start: int, end: int) -> str:
    return record[start - 1:end]


def compose_amount_field(amount: Decimal) -> str:
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 9999999999:
        raise InAchError('invalid_amount', 'Amount cannot be encoded in a NACHA entry.')
    return '%010d' % cents


def parse_amount_field(value: Any) -> Decimal:
    """NACHA amount is 10-digit cents. Dollar strings and shorter digit forms still parse."""
    text = str(value or '').strip().replace(',', '').replace('$', '')
    if not text:
        raise InAchError('invalid_amount', 'Amount is required.')
    if text.isdigit() and len(text) == 10:
        return (Decimal(text) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    try:
        return parse_money(text)
    except AmountError as exc:
        raise InAchError('invalid_amount', 'Invalid ACH amount.') from exc


def compose_trace(aba: str, sequence: int) -> str:
    """15-digit ACH trace: ODFI routing first 8 + 7-digit sequence."""
    try:
        routing = normalize_aba(aba)
    except WireError as exc:
        raise InAchError('invalid_aba', exc.message) from exc
    seq = int(sequence)
    if seq < 1 or seq > 9999999:
        raise InAchError('invalid_trace', 'ACH trace sequence out of range.')
    return '%s%07d' % (routing[:8], seq)


def normalize_trace(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 15:
        raise InAchError('invalid_trace', 'ACH trace must be 15 digits (ODFI 8 + sequence 7).')
    if digits.endswith('0000000'):
        raise InAchError('invalid_trace', 'ACH trace sequence out of range.')
    odfi8 = digits[:8]
    try:
        normalize_aba(odfi8 + '0')  # may fail checksum; accept 8-digit ODFI as-is if 9th unknown
    except WireError:
        pass
    if int(digits[8:]) < 1:
        raise InAchError('invalid_trace', 'ACH trace sequence out of range.')
    return digits


def aba_from_odfi8(odfi8: str, check: str = '') -> str:
    digits = ''.join(ch for ch in str(odfi8 or '') if ch.isdigit())[:8]
    if len(digits) != 8:
        raise InAchError('invalid_aba', 'ODFI identification must be eight digits.')
    if str(check or '').isdigit() and len(str(check).strip()) == 1:
        candidate = digits + str(check).strip()
        try:
            return normalize_aba(candidate)
        except WireError:
            pass
    for check_digit in range(10):
        candidate = '%s%d' % (digits, check_digit)
        try:
            return normalize_aba(candidate)
        except WireError:
            continue
    raise InAchError('invalid_aba', 'ODFI identification failed ABA checksum.')


def extract_dfi_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    if digits.endswith('.0') and digits[:-2].isdigit():
        digits = digits[:-2]
    return str(int(digits)) if digits.isdigit() else digits


def normalize_sec(value: Any, *, default: str = 'PPD') -> str:
    text = str(value or default).strip().upper()
    if text not in SEC_CODES:
        raise InAchError('invalid_sec', 'Unknown Standard Entry Class.')
    return text


def normalize_rail(value: Any, *, default: str = RAIL_STANDARD) -> str:
    text = str(value or default).strip().lower().replace(' ', '_').replace('-', '_')
    mapped = RAIL_ALIASES.get(text, text)
    if mapped not in RAILS:
        raise InAchError('invalid_rail', 'Rail must be standard or sameday.')
    return mapped


def normalize_txn_code(value: Any, *, default: str = '22') -> str:
    text = str(value or default).strip()
    if not text:
        text = default
    if text in DEBIT_TXN:
        raise InAchError('invalid_txn_code', 'Inbound ACH receive posts credits only.')
    if text not in CREDIT_TXN:
        raise InAchError('invalid_txn_code', 'Transaction code is not a live credit.')
    return text


def normalize_return_reason(value: Any, *, default: str = 'R23') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    mapped = RETURN_ALIASES.get(text, text.upper() if len(text) == 3 else text)
    if mapped not in RETURN_REASONS:
        raise InAchError('invalid_reason', 'Unknown ACH return reason.')
    return mapped


def rail_from_batch(discretionary: str = '', description: str = '', explicit: Any = None) -> str:
    if explicit not in (None, ''):
        return normalize_rail(explicit)
    blob = ('%s %s' % (discretionary, description)).upper()
    if 'SAMEDAY' in blob or 'SAME DAY' in blob or ' SDA ' in (' %s ' % blob):
        return RAIL_SAMEDAY
    return RAIL_STANDARD


def split_nacha_records(text: Any) -> List[str]:
    raw = str(text or '')
    stripped = raw.lstrip()
    if stripped.startswith('<') or stripped.upper().startswith('<!DOCTYPE'):
        raise InAchError('invalid_nacha', 'XML is not a NACHA file.')
    normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
    if '\n' in normalized.strip():
        lines = [_fit94(line) for line in normalized.split('\n') if line.strip()]
        if not lines:
            raise InAchError('invalid_nacha', 'Operator file has no NACHA records.')
        return lines
    compact = ''.join(ch for ch in normalized if ch not in '\n\r')
    if len(compact) < 94:
        raise InAchError('invalid_nacha', 'Operator file has no NACHA records.')
    if len(compact) % 94 != 0:
        raise InAchError('invalid_nacha', 'NACHA file length is not a multiple of 94.')
    return [compact[i:i + 94] for i in range(0, len(compact), 94)]


def parse_file_header(record: str) -> Dict[str, str]:
    rec = _fit94(record)
    if rec[0] != '1':
        raise InAchError('invalid_nacha', 'File header must be record type 1.')
    dest = _slice(rec, 4, 13).strip()
    origin = _slice(rec, 14, 23).strip()
    try:
        dest_aba = normalize_aba(dest) if dest else ''
        origin_aba = normalize_aba(origin) if origin else ''
    except WireError as exc:
        raise InAchError('invalid_aba', exc.message) from exc
    return {
        'priority': _slice(rec, 2, 3),
        'immediate_destination': dest_aba,
        'immediate_origin': origin_aba,
        'creation_date': _slice(rec, 24, 29).strip(),
        'creation_time': _slice(rec, 30, 33).strip(),
        'file_id': _slice(rec, 34, 34).strip() or 'A',
        'destination_name': _slice(rec, 41, 63).strip(),
        'origin_name': _slice(rec, 64, 86).strip(),
        'reference': _slice(rec, 87, 94).strip(),
        'raw': rec,
    }


def parse_batch_header(record: str) -> Dict[str, str]:
    rec = _fit94(record)
    if rec[0] != '5':
        raise InAchError('invalid_nacha', 'Batch header must be record type 5.')
    service = _slice(rec, 2, 4).strip()
    if service == '225':
        raise InAchError('invalid_txn_code', 'Debit-only batches cannot be received as credits.')
    odfi8 = _slice(rec, 80, 87).strip()
    return {
        'service_class': service or '220',
        'company_name': _slice(rec, 5, 20).strip(),
        'discretionary': _slice(rec, 21, 40).strip(),
        'company_id': _slice(rec, 41, 50).strip(),
        'sec': normalize_sec(_slice(rec, 51, 53).strip() or 'PPD'),
        'description': _slice(rec, 54, 63).strip(),
        'descriptive_date': _slice(rec, 64, 69).strip(),
        'effective_date': _slice(rec, 70, 75).strip(),
        'odfi8': odfi8,
        'batch_number': _slice(rec, 88, 94).strip(),
        'raw': rec,
    }


def parse_entry_detail(record: str) -> Dict[str, Any]:
    rec = _fit94(record)
    if rec[0] != '6':
        raise InAchError('invalid_nacha', 'Entry detail must be record type 6.')
    txn = _slice(rec, 2, 3).strip()
    rdfi = _slice(rec, 4, 12).strip()
    try:
        receiver = normalize_aba(rdfi)
    except WireError as exc:
        raise InAchError('invalid_aba', exc.message) from exc
    amount = parse_amount_field(_slice(rec, 30, 39))
    return {
        'txn_code': txn,
        'receiver_aba': receiver,
        'beneficiary_account': extract_dfi_account(_slice(rec, 13, 29)),
        'amount': money_str(amount),
        'individual_id': _slice(rec, 40, 54).strip(),
        'beneficiary_name': _slice(rec, 55, 76).strip(),
        'discretionary': _slice(rec, 77, 78).strip(),
        'addenda_indicator': _slice(rec, 79, 79).strip(),
        'trace': normalize_trace(_slice(rec, 80, 94)),
        'raw': rec,
    }


def parse_return_addenda(record: str) -> Dict[str, str]:
    rec = _fit94(record)
    if rec[0] != '7':
        raise InAchError('invalid_nacha', 'Addenda must be record type 7.')
    return {
        'addenda_type': _slice(rec, 2, 3).strip(),
        'return_reason': _slice(rec, 4, 6).strip(),
        'original_trace': _slice(rec, 7, 21).strip(),
        'information': _slice(rec, 36, 79).strip(),
        'trace': _slice(rec, 80, 94).strip(),
        'raw': rec,
    }


def compose_file_header(
    *,
    destination_aba: str,
    origin_aba: str,
    creation_date: str,
    creation_time: str = '1100',
    destination_name: str = 'KONOHA BANK',
    origin_name: str = 'ORIGINATING BANK',
    file_id: str = 'A',
    reference: str = '',
) -> str:
    dest = ' ' + normalize_aba(destination_aba)
    origin = ' ' + normalize_aba(origin_aba)
    yymmdd = str(creation_date)[-6:]
    rec = (
        '1' + '01' + _pad(dest, 10) + _pad(origin, 10) + _pad(yymmdd, 6, right=True, fill='0')
        + _pad(creation_time, 4, right=True, fill='0') + _pad(file_id, 1)
        + '094' + '10' + '1' + _pad(destination_name, 23) + _pad(origin_name, 23)
        + _pad(reference, 8)
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'File header is not 94 characters.')
    return rec


def compose_batch_header(
    *,
    company_name: str,
    company_id: str,
    sec: str,
    description: str,
    effective_date: str,
    odfi_aba: str,
    batch_number: int = 1,
    discretionary: str = '',
    service_class: str = '220',
) -> str:
    odfi = normalize_aba(odfi_aba)[:8]
    rec = (
        '5' + _pad(service_class, 3, right=True, fill='0') + _pad(company_name, 16)
        + _pad(discretionary, 20) + _pad(company_id, 10) + _pad(sec, 3)
        + _pad(description, 10) + _pad('', 6) + _pad(str(effective_date)[-6:], 6, right=True, fill='0')
        + '   ' + '1' + _pad(odfi, 8) + _pad(str(batch_number), 7, right=True, fill='0')
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'Batch header is not 94 characters.')
    return rec


def compose_entry_detail(
    *,
    txn_code: str,
    receiver_aba: str,
    account: str,
    amount: Decimal,
    individual_id: str,
    name: str,
    trace: str,
    addenda: bool = False,
) -> str:
    routing = normalize_aba(receiver_aba)
    rec = (
        '6' + _pad(txn_code, 2) + routing + _pad(account, 17)
        + compose_amount_field(amount) + _pad(individual_id, 15) + _pad(name, 22)
        + '  ' + ('1' if addenda else '0') + normalize_trace(trace)
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'Entry detail is not 94 characters.')
    return rec


def compose_return_addenda(
    *,
    reason: str,
    original_trace: str,
    original_rdfi: str,
    information: str,
    trace: str,
) -> str:
    rec = (
        '7' + '99' + normalize_return_reason(reason) + normalize_trace(original_trace)
        + '      ' + normalize_aba(original_rdfi)[:8] + _pad(information, 44)
        + normalize_trace(trace)
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'Return addenda is not 94 characters.')
    return rec


def compose_batch_control(
    *,
    service_class: str,
    entry_count: int,
    entry_hash: int,
    total_debit: Decimal,
    total_credit: Decimal,
    company_id: str,
    odfi_aba: str,
    batch_number: int = 1,
) -> str:
    rec = (
        '8' + _pad(service_class, 3, right=True, fill='0') + '%06d' % entry_count
        + '%010d' % (entry_hash % 10 ** 10)
        + '%012d' % int(total_debit * 100)
        + '%012d' % int(total_credit * 100)
        + _pad(company_id, 10) + (' ' * 19) + (' ' * 6)
        + _pad(normalize_aba(odfi_aba)[:8], 8)
        + _pad(str(batch_number), 7, right=True, fill='0')
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'Batch control is not 94 characters.')
    return rec


def compose_file_control(
    *,
    batch_count: int,
    block_count: int,
    entry_count: int,
    entry_hash: int,
    total_debit: Decimal,
    total_credit: Decimal,
) -> str:
    rec = (
        '9' + '%06d' % batch_count + '%06d' % block_count + '%08d' % entry_count
        + '%010d' % (entry_hash % 10 ** 10)
        + '%012d' % int(total_debit * 100)
        + '%012d' % int(total_credit * 100)
        + (' ' * 39)
    )
    if len(rec) != 94:
        raise InAchError('invalid_nacha', 'File control is not 94 characters.')
    return rec


def pad_nacha_blocks(records: Sequence[str]) -> List[str]:
    out = [_fit94(item) for item in records]
    while len(out) % 10:
        out.append('9' * 94)
    return out


def compose_nacha(fields: Dict[str, Any]) -> str:
    """Compose a one-credit NACHA file in canonical record order."""
    amount = parse_amount_field(fields.get('amount'))
    receiver = fields.get('receiver_aba') or DEFAULT_RECEIVER_ABA
    sender = fields.get('sender_aba') or fields.get('odfi') or '026009593'
    try:
        receiver = normalize_aba(receiver)
        sender = normalize_aba(sender)
    except WireError as exc:
        raise InAchError('invalid_aba', exc.message) from exc
    trace = fields.get('trace') or compose_trace(sender, int(fields.get('sequence') or 1))
    creation = str(fields.get('creation_date') or fields.get('effective_date') or '240614')
    effective = str(fields.get('effective_date') or creation)[-6:]
    sec = normalize_sec(fields.get('sec') or 'PPD')
    txn = str(fields.get('txn_code') or '22').strip() or '22'
    rail = normalize_rail(fields.get('rail') or RAIL_STANDARD)
    discretionary = 'SAMEDAY' if rail == RAIL_SAMEDAY else str(fields.get('discretionary') or '')
    entry = compose_entry_detail(
        txn_code=txn,
        receiver_aba=receiver,
        account=str(fields.get('beneficiary_account') or fields.get('account') or ''),
        amount=amount,
        individual_id=str(fields.get('individual_id') or ''),
        name=str(fields.get('beneficiary_name') or 'BENEFICIARY'),
        trace=trace,
    )
    hash_total = int(receiver[:8])
    records = [
        compose_file_header(
            destination_aba=receiver,
            origin_aba=sender,
            creation_date=creation,
            destination_name=str(fields.get('receiver_name') or 'KONOHA BANK'),
            origin_name=str(fields.get('sender_name') or 'ORIGINATING BANK'),
        ),
        compose_batch_header(
            company_name=str(fields.get('originator_name') or 'ORIGINATOR'),
            company_id=str(fields.get('company_id') or '1234567890'),
            sec=sec,
            description=str(fields.get('description') or 'CREDIT'),
            effective_date=effective,
            odfi_aba=sender,
            discretionary=discretionary,
        ),
        entry,
        compose_batch_control(
            service_class='220',
            entry_count=1,
            entry_hash=hash_total,
            total_debit=Decimal('0.00'),
            total_credit=amount,
            company_id=str(fields.get('company_id') or '1234567890'),
            odfi_aba=sender,
        ),
        compose_file_control(
            batch_count=1,
            block_count=1,
            entry_count=1,
            entry_hash=hash_total,
            total_debit=Decimal('0.00'),
            total_credit=amount,
        ),
    ]
    return '\n'.join(pad_nacha_blocks(records))


def compose_return_nacha(
    row: 'InboundAch',
    *,
    return_trace: str,
    reason: str,
    receiver_aba: str,
) -> str:
    """Nacha return of an inbound credit: reversed txn + addenda 99."""
    amount = parse_money(row.amount)
    return_txn = RETURN_TXN.get(row.txn_code, '21')
    sender = row.sender_aba
    records = [
        compose_file_header(
            destination_aba=sender,
            origin_aba=receiver_aba,
            creation_date=row.value_date,
            destination_name='ORIGINATING BANK',
            origin_name='KONOHA BANK',
        ),
        compose_batch_header(
            company_name='KONOHA BANK',
            company_id='1999999999',
            sec=row.sec,
            description='RETURN',
            effective_date=row.value_date,
            odfi_aba=receiver_aba,
        ),
        compose_entry_detail(
            txn_code=return_txn,
            receiver_aba=sender,
            account=row.originator_account_last4 or '0000',
            amount=amount,
            individual_id=row.trace,
            name=row.originator_name,
            trace=return_trace,
            addenda=True,
        ),
        compose_return_addenda(
            reason=reason,
            original_trace=row.trace,
            original_rdfi=receiver_aba,
            information='RET %s' % reason,
            trace=return_trace,
        ),
        compose_batch_control(
            service_class='220',
            entry_count=2,
            entry_hash=int(normalize_aba(sender)[:8]),
            total_debit=Decimal('0.00'),
            total_credit=amount,
            company_id='1999999999',
            odfi_aba=receiver_aba,
        ),
        compose_file_control(
            batch_count=1,
            block_count=1,
            entry_count=2,
            entry_hash=int(normalize_aba(sender)[:8]),
            total_debit=Decimal('0.00'),
            total_credit=amount,
        ),
    ]
    return '\n'.join(pad_nacha_blocks(records))


def iter_nacha_entries(text: Any) -> List[Dict[str, Any]]:
    records = split_nacha_records(text)
    file_header: Dict[str, str] = {}
    batch: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None

    def _flush() -> None:
        nonlocal pending
        if pending is not None:
            entries.append(pending)
            pending = None

    for rec in records:
        if not rec:
            continue
        if rec[0] == '9' and set(rec) <= {'9'}:
            continue
        kind = rec[0]
        if kind == '1':
            file_header = parse_file_header(rec)
        elif kind == '5':
            _flush()
            batch = parse_batch_header(rec)
        elif kind == '6':
            _flush()
            pending = parse_entry_detail(rec)
            pending['file_destination'] = file_header.get('immediate_destination', '')
            pending['originator_name'] = batch.get('company_name') or 'ORIGINATOR'
            pending['sender_name'] = file_header.get('origin_name') or ''
            pending['sec'] = batch.get('sec') or 'PPD'
            pending['description'] = batch.get('description') or ''
            pending['company_id'] = batch.get('company_id') or ''
            pending['effective_date'] = batch.get('effective_date') or ''
            pending['odfi8'] = batch.get('odfi8') or pending['trace'][:8]
            pending['rail'] = rail_from_batch(
                batch.get('discretionary') or '', batch.get('description') or '',
            )
        elif kind == '7' and pending is not None:
            pending['addenda'] = parse_return_addenda(rec)
        elif kind in {'8', '9'}:
            _flush()
    _flush()
    if not entries:
        raise InAchError('invalid_nacha', 'Operator file has no NACHA entries.')
    return entries


def split_nacha_file(text: Any) -> List[Dict[str, Any]]:
    """Split an operator file into credit-entry field maps."""
    return iter_nacha_entries(text)


def message_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    txn = normalize_txn_code(entry.get('txn_code') or '22')
    try:
        sender = aba_from_odfi8(entry.get('odfi8') or entry.get('trace', '')[:8])
    except InAchError:
        try:
            sender = normalize_aba(entry.get('sender_aba'))
        except (WireError, TypeError) as exc:
            raise InAchError('invalid_aba', 'Originating DFI is required.') from exc
    receiver = entry.get('receiver_aba')
    try:
        receiver = normalize_aba(receiver)
    except WireError as exc:
        raise InAchError('invalid_aba', exc.message) from exc
    account = extract_dfi_account(entry.get('beneficiary_account'))
    originator = str(entry.get('originator_name') or 'ORIGINATOR').strip()
    beneficiary = str(entry.get('beneficiary_name') or 'BENEFICIARY').strip()
    return {
        'trace': normalize_trace(entry['trace']),
        'amount': entry['amount'] if 'amount' in entry else money_str(parse_amount_field(entry.get('amount'))),
        'sender_aba': sender,
        'sender_name': str(entry.get('sender_name') or '').strip(),
        'receiver_aba': receiver,
        'originator_name': originator,
        'originator_account': str(entry.get('individual_id') or '').strip(),
        'beneficiary_name': beneficiary,
        'beneficiary_account': account,
        'sec': normalize_sec(entry.get('sec') or 'PPD'),
        'rail': normalize_rail(entry.get('rail') or RAIL_STANDARD),
        'txn_code': txn,
        'memo': normalize_note(entry.get('description') or entry.get('memo') or '', limit=140),
        'company_id': str(entry.get('company_id') or '').strip(),
        'effective_date': str(entry.get('effective_date') or '').strip(),
        'raw': str(entry.get('raw') or ''),
    }


def message_from_nacha(text: Any) -> Dict[str, Any]:
    entries = iter_nacha_entries(text)
    return message_from_entry(entries[0])


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """JSON operator payload → inbound field map (same shape as NACHA)."""
    if values.get('file') or values.get('nacha') or values.get('raw'):
        return message_from_nacha(values.get('file') or values.get('nacha') or values.get('raw'))
    trace = values.get('trace')
    if not trace:
        sender = values.get('sender_aba') or values.get('odfi') or values.get('sender')
        if not sender:
            raise InAchError('invalid_trace', 'ACH trace is required.')
        trace = compose_trace(sender, int(values.get('sequence') or 1))
    amount = parse_amount_field(values.get('amount'))
    try:
        sender_aba = normalize_aba(values.get('sender_aba') or values.get('odfi') or values.get('sender'))
        receiver_aba = normalize_aba(values.get('receiver_aba') or values.get('receiver') or values.get('rdfi'))
        account = normalize_external_account(values.get('beneficiary_account') or values.get('account'))
    except WireError as exc:
        raise InAchError(exc.code, exc.message) from exc
    originator = str(values.get('originator_name') or values.get('originator') or values.get('company_name') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    explicit_rail = values.get('rail')
    same_day = values.get('same_day')
    if same_day in (True, 'true', '1', 'yes', 'on', 'sameday', 'same_day'):
        explicit_rail = RAIL_SAMEDAY
    rail = rail_from_batch(
        str(values.get('discretionary') or ''),
        str(values.get('description') or ''),
        explicit_rail,
    )
    return {
        'trace': normalize_trace(trace),
        'amount': money_str(amount),
        'sender_aba': sender_aba,
        'sender_name': str(values.get('sender_name') or '').strip(),
        'receiver_aba': receiver_aba,
        'originator_name': originator or 'ORIGINATOR',
        'originator_account': str(values.get('originator_account') or values.get('individual_id') or '').strip(),
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'beneficiary_account': account,
        'sec': normalize_sec(values.get('sec') or 'PPD'),
        'rail': rail,
        'txn_code': normalize_txn_code(values.get('txn_code') or values.get('transaction_code') or '22'),
        'memo': normalize_note(values.get('memo') or values.get('description') or '', limit=140),
        'company_id': str(values.get('company_id') or '').strip(),
        'effective_date': str(values.get('effective_date') or '').strip(),
        'raw': '',
    }


@dataclass
class InAchPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('10000000.00')
    sameday_max_amount: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 16
    cutoff_minute: int = 45
    tz_offset_hours: int = -4
    receiver_aba: str = DEFAULT_RECEIVER_ABA
    return_business_days: int = 2
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'InAchPolicy':
        extra = _env_list('INACH_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INACH_RECEIVER_ABA') or DEFAULT_RECEIVER_ABA
        try:
            receiver = normalize_aba(receiver)
        except WireError:
            receiver = DEFAULT_RECEIVER_ABA
        return cls(
            enabled=_env_bool('INACH_ENABLED', True),
            customer_view=_env_bool('INACH_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INACH_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INACH_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INACH_MAX', 240)),
            min_amount=_env_money('INACH_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('INACH_MAX_AMOUNT', '10000000.00'),
            sameday_max_amount=_env_money('INACH_SAMEDAY_MAX', '1000000.00'),
            dual_control_threshold=_env_money('INACH_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('INACH_CUTOFF_HOUR', 16))),
            cutoff_minute=max(0, min(59, _env_int('INACH_CUTOFF_MINUTE', 45))),
            tz_offset_hours=_env_int('INACH_TZ_OFFSET', -4),
            receiver_aba=receiver,
            return_business_days=max(1, _env_int('INACH_RETURN_DAYS', 2)),
            watchlist=watch,
            extra_holidays=_env_list('INACH_HOLIDAYS'),
        )


@dataclass
class InboundAch:
    inbound_id: str
    trace: str
    userid: str
    internal_account: str
    amount: str
    sender_aba: str
    receiver_aba: str
    originator_name: str
    originator_account_last4: str
    beneficiary_name: str
    beneficiary_account: str
    sec: str
    rail: str
    txn_code: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_trace: str
    return_reason: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''
    company_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'trace': self.trace,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'sender_aba': self.sender_aba,
            'receiver_aba': self.receiver_aba,
            'originator_name': self.originator_name,
            'originator_last4': self.originator_account_last4,
            'beneficiary_name': self.beneficiary_name,
            'beneficiary_last4': last4(self.beneficiary_account),
            'sec': self.sec,
            'rail': self.rail,
            'txn_code': self.txn_code,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_trace': self.return_trace,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'company_id': self.company_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'queued': self.status == IN_QUEUED,
            'pending_release': self.status == IN_PENDING,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone(row: InboundAch) -> InboundAch:
    return InboundAch(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundAch:
    return InboundAch(
        inbound_id=row['inbound_id'],
        trace=row['trace'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        sender_aba=row['sender_aba'],
        receiver_aba=row['receiver_aba'],
        originator_name=row['originator_name'],
        originator_account_last4=row['originator_account_last4'] or '',
        beneficiary_name=row['beneficiary_name'],
        beneficiary_account=row['beneficiary_account'],
        sec=row['sec'] or 'PPD',
        rail=row['rail'] or RAIL_STANDARD,
        txn_code=row['txn_code'] or '22',
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_trace=row['return_trace'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
        company_id=row['company_id'] or '',
    )


_ROW_FIELDS = (
    'inbound_id', 'trace', 'userid', 'internal_account', 'amount',
    'sender_aba', 'receiver_aba', 'originator_name', 'originator_account_last4',
    'beneficiary_name', 'beneficiary_account', 'sec', 'rail', 'txn_code',
    'purpose', 'memo', 'status', 'value_date', 'actor', 'releaser',
    'ofac_hit', 'ofac_match', 'return_trace', 'return_reason',
    'created_at', 'updated_at', 'posted_at', 'returned_at', 'note',
    'batch_id', 'company_id',
)


class MemoryInAchStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundAch] = {}
        self._by_trace: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundAch) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_trace[row.trace] = row.inbound_id

    def update(self, row: InboundAch) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InAchError('inbound_not_found', 'Inbound ACH not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_trace[row.trace] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundAch]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_trace(self, trace: str) -> Optional[InboundAch]:
        with self._lock:
            inbound_id = self._by_trace.get(trace)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundAch]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundAch]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundAch]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundAch]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInAchStore:
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
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    trace TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    sender_aba TEXT NOT NULL,
                    receiver_aba TEXT NOT NULL,
                    originator_name TEXT NOT NULL,
                    originator_account_last4 TEXT NOT NULL DEFAULT '',
                    beneficiary_name TEXT NOT NULL,
                    beneficiary_account TEXT NOT NULL,
                    sec TEXT NOT NULL DEFAULT 'PPD',
                    rail TEXT NOT NULL DEFAULT 'standard',
                    txn_code TEXT NOT NULL DEFAULT '22',
                    purpose TEXT NOT NULL DEFAULT 'other',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_trace TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    company_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _write(self, conn: sqlite3.Connection, row: InboundAch) -> None:
        placeholders = ', '.join(_ROW_FIELDS)
        qmarks = ', '.join('?' for _ in _ROW_FIELDS)
        conn.execute(
            'INSERT OR REPLACE INTO inbounds (%s) VALUES (%s)' % (placeholders, qmarks),
            tuple(getattr(row, field) if field != 'ofac_hit' else int(row.ofac_hit) for field in _ROW_FIELDS),
        )

    def put(self, row: InboundAch) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundAch) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InAchError('inbound_not_found', 'Inbound ACH not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_trace(self, trace: str) -> Optional[InboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE trace = ?', (trace,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundAch]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundAch]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundAch]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundAch]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT * FROM inbounds ORDER BY created_at DESC').fetchall()
        return [_from_row(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
            current = int(row['value']) if row is not None else 0
            current += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('seq', ?)",
                (str(current),),
            )
            conn.commit()
        return current


class InAchService:
    def __init__(
        self,
        policy: InAchPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            cutoff_minute=policy.cutoff_minute,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InAchError('inach_disabled', 'Inbound ACH is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InAchError('inach_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InAchError('inach_forbidden', 'Customers cannot view inbound ACH.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InAchError('inach_forbidden', 'Customers cannot request inbound ACH returns.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _lookup(self, account: str) -> Optional[str]:
        if self.lookup_fn is None:
            return None
        found = self.lookup_fn(account)
        if found in (None, '', -1, 0):
            return None
        return str(found)

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise InAchError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InAchError('credit_not_allowed', 'Credit accounts cannot receive inbound ACH.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        if dollars < self.policy.min_amount:
            raise InAchError('amount_out_of_range', 'Amount is outside the allowed range.')
        if rail == RAIL_SAMEDAY and dollars > self.policy.sameday_max_amount:
            raise InAchError('sameday_amount_exceeded', 'Same-Day ACH exceeds the per-item cap.')
        if dollars > self.policy.max_amount:
            raise InAchError('ach_amount_exceeded', 'ACH amount exceeds the inbound cap.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_aba: str) -> None:
        if receiver_aba != self.policy.receiver_aba:
            raise InAchError('wrong_receiver', 'Entry is not addressed to this bank.')

    def _value_date(self, rail: str, ts: float) -> date:
        local = self.calendar.local_dt(ts)
        if rail == RAIL_STANDARD:
            return self.calendar.next_business_day(local.date())
        return self.calendar.value_date(ts)

    def _today(self, ts: float) -> date:
        return self.calendar.local_dt(ts).date()

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundAch:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InAchError('inbound_not_found', 'Inbound ACH not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InAchError('inach_forbidden', 'Not allowed to view this inbound ACH.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars, message['rail'])
        self._assert_receiver(message['receiver_aba'])
        ofac = self._screen(message['originator_name'])
        userid = self._lookup(message['beneficiary_account'])
        now = float(self.clock())
        return {
            'message': {
                'trace': message['trace'],
                'amount': message['amount'],
                'sender_aba': message['sender_aba'],
                'receiver_aba': message['receiver_aba'],
                'originator_name': message['originator_name'],
                'beneficiary_name': message['beneficiary_name'],
                'beneficiary_last4': last4(message['beneficiary_account']),
                'sec': message['sec'],
                'rail': message['rail'],
                'txn_code': message['txn_code'],
            },
            'matched_userid': userid or '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'value_date': self._value_date(message['rail'], now).isoformat(),
            'clock': self.calendar.snapshot(now),
        }

    def _evaluate_status(
        self,
        *,
        userid: str,
        account: str,
        dollars: Decimal,
        ofac: ScreenResult,
        rail: str,
        value_date: date,
    ) -> str:
        if not userid or not account:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(dollars):
            return IN_PENDING
        now = float(self.clock())
        today = self._today(now)
        if value_date > today:
            return IN_QUEUED
        if rail == RAIL_SAMEDAY:
            clock = self.calendar.snapshot(now)
            if clock['after_cutoff'] or not clock['business_day']:
                return IN_QUEUED
        return IN_POSTED

    def _credit(self, row: InboundAch) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = 'ach from %s' % (row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundAch) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = 'ach return %s' % (row.trace[:12])
        result = self.debit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundAch) -> InboundAch:
        classified = self._credit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        row.status = IN_FAILED
        row.note = 'credit_failed'
        row.updated_at = now
        self.store.update(row)
        raise InAchError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundAch, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars, message['rail'])
        self._assert_receiver(message['receiver_aba'])
        existing = self.store.get_by_trace(message['trace'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InAchError('inbound_limit', 'Inbound ACH limit reached.')
        try:
            account = normalize_account(message['beneficiary_account'])
        except AccountError:
            account = ''
        userid = self._lookup(account) if account else None
        credit_blocked = False
        if userid and account:
            types = self._account_types(userid)
            if types.get(account) == 'credit' and not self.policy.allow_credit:
                credit_blocked = True
                userid = None
                account = ''
        ofac = self._screen(message['originator_name'], aliases=(message.get('sender_name') or '',))
        now = float(self.clock())
        value_date = self._value_date(message['rail'], now)
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            dollars=dollars,
            ofac=ofac,
            rail=message['rail'],
            value_date=value_date,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        try:
            purpose = normalize_purpose(values.get('purpose') or 'other')
        except WireError:
            purpose = 'other'
        try:
            originator_name = normalize_legal_name(message['originator_name'])
        except WireError:
            originator_name = normalize_party(message['originator_name'])[:80] or 'ORIGINATOR'
        try:
            beneficiary_name = normalize_legal_name(message['beneficiary_name'])
        except WireError:
            beneficiary_name = normalize_party(message['beneficiary_name'])[:80] or 'BENEFICIARY'
        row = InboundAch(
            inbound_id=uuid.uuid4().hex,
            trace=message['trace'],
            userid=userid or '',
            internal_account=account,
            amount=money_str(dollars),
            sender_aba=message['sender_aba'],
            receiver_aba=message['receiver_aba'],
            originator_name=originator_name,
            originator_account_last4=last4(message.get('originator_account')),
            beneficiary_name=beneficiary_name,
            beneficiary_account=message['beneficiary_account'],
            sec=message['sec'],
            rail=message['rail'],
            txn_code=message['txn_code'],
            purpose=purpose,
            memo=message['memo'],
            status=status,
            value_date=value_date.strftime('%Y%m%d'),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_trace='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
            company_id=str(message.get('company_id') or ''),
        )
        self.store.put(row)
        if row.status == IN_POSTED:
            return self._try_post(row), True
        return row, True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        text: Any,
        purpose: str = 'other',
        rail: Any = '',
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        try:
            entries = split_nacha_file(text)
        except InAchError:
            raise
        if not entries:
            raise InAchError('invalid_nacha', 'Operator file has no NACHA entries.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        forced_rail = normalize_rail(rail) if rail else ''
        for entry in entries:
            try:
                if forced_rail:
                    entry = dict(entry)
                    entry['rail'] = forced_rail
                payload = message_from_entry(entry)
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values={
                        'trace': payload['trace'],
                        'amount': payload['amount'],
                        'sender_aba': payload['sender_aba'],
                        'receiver_aba': payload['receiver_aba'],
                        'beneficiary_account': payload['beneficiary_account'],
                        'originator_name': payload['originator_name'],
                        'beneficiary_name': payload['beneficiary_name'],
                        'sec': payload['sec'],
                        'rail': payload['rail'],
                        'txn_code': payload['txn_code'],
                        'memo': payload['memo'],
                        'company_id': payload['company_id'],
                        'purpose': purpose,
                        'batch_id': batch_id,
                    },
                    batch_id=batch_id,
                )
                snap = row.to_dict()
                if created:
                    accepted.append(snap)
                else:
                    duplicates.append(snap)
            except InAchError as exc:
                errors.append({'error': exc.code, 'message': exc.message})
        return {
            'batch_id': batch_id,
            'accepted': accepted,
            'duplicates': duplicates,
            'errors': errors,
            'accepted_count': len(accepted),
            'duplicate_count': len(duplicates),
            'error_count': len(errors),
        }

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        internal_account: Any = None,
    ) -> InboundAch:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InAchError('not_assignable', 'Only unmatched inbound ACH can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InAchError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.beneficiary_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        dollars = parse_money(row.amount)
        value = date.fromisoformat(
            '%s-%s-%s' % (row.value_date[:4], row.value_date[4:6], row.value_date[6:8])
        ) if len(row.value_date) == 8 else self._value_date(row.rail, float(self.clock()))
        status = self._evaluate_status(
            userid=owner, account=account, dollars=dollars, ofac=ofac,
            rail=row.rail, value_date=value,
        )
        row.status = status
        self.store.update(row)
        if status == IN_POSTED:
            return self._try_post(row)
        return row

    def override_ofac(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InboundAch:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InAchError('not_overridable', 'Only OFAC-held inbound ACH can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        dollars = parse_money(row.amount)
        value = date.fromisoformat(
            '%s-%s-%s' % (row.value_date[:4], row.value_date[4:6], row.value_date[6:8])
        )
        status = self._evaluate_status(
            userid=row.userid, account=row.internal_account, dollars=dollars,
            ofac=ScreenResult(False, '', 0), rail=row.rail, value_date=value,
        )
        row.status = status
        self.store.update(row)
        if status == IN_POSTED:
            return self._try_post(row)
        return row

    def release(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
    ) -> InboundAch:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InAchError('not_releasable', 'Inbound ACH is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InAchError('same_approver', 'A different employee must release this inbound ACH.')
        row.releaser = str(actor)
        now = float(self.clock())
        row.updated_at = now
        today = self._today(now).strftime('%Y%m%d')
        if row.value_date > today:
            row.status = IN_QUEUED
            self.store.update(row)
            return row
        if row.rail == RAIL_SAMEDAY:
            clock = self.calendar.snapshot(now)
            if clock['after_cutoff'] or not clock['business_day']:
                row.status = IN_QUEUED
                row.value_date = self.calendar.next_business_day(self._today(now)).strftime('%Y%m%d')
                self.store.update(row)
                return row
        row.status = IN_POSTED
        self.store.update(row)
        return self._try_post(row)

    def reject(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundAch:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InAchError('not_rejectable', 'Inbound ACH cannot be rejected.')
        row.status = IN_REJECTED
        row.return_reason = normalize_return_reason(reason)
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundAch:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'cust',
        note: Any = '',
    ) -> InboundAch:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InAchError('inach_forbidden', 'Not allowed to return this inbound ACH.')
        return self._return(row, actor=actor, reason=reason or 'cust', note=note, force_window=False)

    def _return_deadline(self, posted_at: float) -> date:
        start = self.calendar.local_dt(posted_at).date()
        if not self.calendar.is_business_day(start):
            start = self.calendar.next_business_day(start)
        deadline = start
        for _ in range(self.policy.return_business_days):
            deadline = self.calendar.next_business_day(deadline)
        return deadline

    def _return(
        self,
        row: InboundAch,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundAch:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InAchError('already_returned', 'Inbound ACH is already returned or rejected.')
        code = normalize_return_reason(reason, default='R23')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            seq = self.store.next_sequence()
            row.return_trace = compose_trace(self.policy.receiver_aba, seq)
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InAchError('not_returnable', 'Inbound ACH cannot be returned.')
        if not force_window:
            deadline = self._return_deadline(row.posted_at or row.created_at)
            if self._today(now) > deadline:
                raise InAchError('return_window_closed', 'ACH return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InAchError('nsf', 'Insufficient funds to return this inbound ACH.', inbound=row)
        if classified != 'ok':
            raise InAchError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        seq = self.store.next_sequence()
        row.return_trace = compose_trace(self.policy.receiver_aba, seq)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundAch]:
        now = float(self.clock())
        if not self.calendar.is_business_day(self._today(now)):
            return []
        today = self._today(now).strftime('%Y%m%d')
        posted = []
        for row in self.store.list_open():
            if row.status != IN_QUEUED:
                continue
            if userid is not None and row.userid != userid:
                continue
            if not row.userid or not row.internal_account:
                continue
            if row.value_date > today:
                continue
            try:
                posted.append(self._try_post(row))
            except InAchError:
                continue
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InAchError('inach_forbidden', 'Not allowed to view this inbound book.')
        self.run_due(userid)
        rows = self.store.list_for(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == IN_POSTED:
                posted_ytd += amount
            elif row.status == IN_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'receiver_aba': self.policy.receiver_aba,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'sameday_max': money_str(self.policy.sameday_max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'inbounds': [row.to_dict() for row in rows[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_returned': money_str(returned_ytd),
            'open_count': sum(1 for row in rows if row.status in OPEN_INBOUNDS),
            'posted_count': sum(1 for row in rows if row.status == IN_POSTED),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_unmatched()
        return {
            'enabled': self.policy.enabled,
            'receiver_aba': self.policy.receiver_aba,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InAchService] = None


def set_service(service: Optional[InAchService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InAchService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INACH_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInAchStore()
    path = os.environ.get('INACH_DB', DEFAULT_STORE_PATH)
    return SqliteInAchStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> InAchService:
    if store is None:
        store = default_store()
    return InAchService(
        InAchPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        calendar=calendar,
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
        'already_returned': 409,
        'inbound_limit': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'inach_forbidden': 403,
        'inach_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'return_window_closed': 403,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_aba': 400,
        'invalid_trace': 400,
        'invalid_nacha': 400,
        'invalid_txn_code': 400,
        'invalid_sec': 400,
        'invalid_rail': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'wrong_receiver': 400,
        'sameday_amount_exceeded': 400,
        'ach_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InAchError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inbound') is not None:
        body['inbound'] = exc.extra['inbound'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WireError as exc:
        return jsonify({'message': str(exc), 'error': exc.code}), _error_status(exc.code)
    except InAchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InAchs': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inach_forbidden'}), 403
    return jsonify({'InAchs': service.unmatched_snapshot()}), 200


def handle_preview(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inach_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_ingest(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound ACH ingested' if created else 'Inbound ACH already posted',
            'inbound': row.to_dict(),
            'InAchs': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('nacha') or values.get('text')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            purpose=values.get('purpose') or 'other',
            rail=values.get('rail') or '',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=values.get('customer_id') or values.get('owner'),
            internal_account=values.get('account') or values.get('internal_account'),
        )
        return jsonify({
            'message': 'Inbound ACH assigned',
            'inbound': row.to_dict(),
            'InAchs': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InAchService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Inbound ACH released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound ACH rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound ACH returned'
        else:
            raise InAchError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InAchs': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_return(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'cust',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound return requested',
            'inbound': row.to_dict(),
            'InAchs': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InAchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InAchs': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_inach_routes(app, service: InAchService) -> None:
    @app.route('/listInAchs', methods=['POST', 'GET'])
    def list_inachs_route():
        return handle_list(service)

    @app.route('/listUnmatchedInAchs', methods=['POST', 'GET'])
    def list_unmatched_inachs_route():
        return handle_unmatched(service)

    @app.route('/previewInAch', methods=['POST', 'GET'])
    def preview_inach_route():
        return handle_preview(service)

    @app.route('/ingestInAch', methods=['POST', 'GET'])
    def ingest_inach_route():
        return handle_ingest(service)

    @app.route('/ingestInAchFile', methods=['POST', 'GET'])
    def ingest_inach_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInAch', methods=['POST', 'GET'])
    def assign_inach_route():
        return handle_assign(service)

    @app.route('/overrideInAchOfac', methods=['POST', 'GET'])
    def override_inach_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInAch', methods=['POST', 'GET'])
    def release_inach_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInAch', methods=['POST', 'GET'])
    def reject_inach_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInAch', methods=['POST', 'GET'])
    def return_inach_route():
        return _staff_action(service, 'return')

    @app.route('/requestInAchReturn', methods=['POST', 'GET'])
    def request_inach_return_route():
        return handle_request_return(service)

    @app.route('/runDueInAchs', methods=['POST', 'GET'])
    def run_due_inachs_route():
        return handle_run_due(service)
