"""Inbound FedNow / TCH RTP receive posting from an operator file.

Staff ingest incoming ISO 20022 pacs.008 instant credits (FedNow or The
Clearing House RTP). Matched credits post immediately — these rails are
24/7/365 with no Fedwire cutoff queue. Independent of outbound Fedwire
(PR #73), inbound Fedwire (PR #90), FedNow/RTP origination (PR #83),
SEPA / SWIFT / UK Pay origination, ACH linking (PR #68), and bill-pay
ACH (PR #66). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- ISO 20022 pacs.008 parse / compose / multi-document file split
- UETR uniqueness (idempotent ingest)
- Dual-rail selection (fednow vs rtp) + per-rail amount caps
- Instant 24/7/365 clock (no weekend / cutoff queue)
- Receiver-ABA acceptance (this bank)
- Account-directory lookup (creditor account → customer)
- Incoming credit posting + pacs.004 return with ISO reason codes
- OFAC-style originator screening (reused)
- Dual-control release for high-value inbound credits

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Originator and beneficiary account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    AccountError,
    AmountError,
    ScreenResult,
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
    normalize_source,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

IN_HELD = 'held'
IN_UNMATCHED = 'unmatched'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_PENDING, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING})

RAIL_FEDNOW = 'fednow'
RAIL_RTP = 'rtp'
RAILS = frozenset({RAIL_FEDNOW, RAIL_RTP})
RAIL_ALIASES = {
    'fednow': RAIL_FEDNOW, 'fed': RAIL_FEDNOW, 'fn': RAIL_FEDNOW,
    'fdn': RAIL_FEDNOW, 'fdnow': RAIL_FEDNOW, 'fedn': RAIL_FEDNOW,
    'federalreserve': RAIL_FEDNOW, 'frb': RAIL_FEDNOW,
    'rtp': RAIL_RTP, 'tch': RAIL_RTP, 'tchrpt': RAIL_RTP, 'tchrp': RAIL_RTP,
    'clearinghouse': RAIL_RTP, 'theclearinghouse': RAIL_RTP,
}
CLR_SYS_ALIASES = {
    'fdn': RAIL_FEDNOW, 'fdnow': RAIL_FEDNOW, 'fednow': RAIL_FEDNOW,
    'fedn': RAIL_FEDNOW, 'fn': RAIL_FEDNOW,
    'rtp': RAIL_RTP, 'tch': RAIL_RTP, 'tchrpt': RAIL_RTP,
}

RETURN_REASONS = frozenset({
    'AC01', 'AC03', 'AC04', 'AC06', 'AM04', 'BE01', 'CUST', 'DUPL', 'MS03', 'RR04',
})
RETURN_ALIASES = {
    'acct': 'AC03', 'account': 'AC03', 'unknown': 'AC03', 'no_account': 'AC03',
    'closed': 'AC04', 'blocked': 'AC06', 'incorrect': 'AC01',
    'nsf': 'AM04', 'nsfr': 'AM04', 'insufficient': 'AM04',
    'name': 'BE01', 'mismatch': 'BE01', 'beneficiary': 'BE01',
    'cust': 'CUST', 'customer': 'CUST', 'requested': 'CUST',
    'dup': 'DUPL', 'duplicate': 'DUPL',
    'ofac': 'RR04', 'sanction': 'RR04', 'sanctions': 'RR04', 'regulatory': 'RR04',
    'other': 'MS03', 'unspecified': 'MS03',
}

PACS008_NS = 'urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08'
PACS004_NS = 'urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09'
DEFAULT_STORE_PATH = 'SystemLogs/inrtp.sqlite'
DEFAULT_RECEIVER_ABA = '021000021'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
_UETR = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)
_UETR32 = re.compile(r'^[0-9a-fA-F]{32}$')
_DOCTYPE = re.compile(r'<!DOCTYPE|<!ENTITY', re.I)
_SPLIT = re.compile(r'(?=(?:\s*<\?xml|\s*<Document|\s*<pacs\.008))', re.I)
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60


class InRtpError(ValueError):
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


def _local(tag: str) -> str:
    if '}' in tag:
        return tag.rsplit('}', 1)[-1]
    return tag


def _find(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    wanted = set(names)
    if el is None:
        return None
    if _local(el.tag) in wanted:
        return el
    for child in el.iter():
        if _local(child.tag) in wanted:
            return child
    return None


def _text(el: Optional[ET.Element], *names: str, default: str = '') -> str:
    found = _find(el, *names) if names else el
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_xml_safe(text: Any) -> ET.Element:
    """Parse ISO 20022 XML. DOCTYPE / ENTITY are rejected (XXE)."""
    raw = str(text or '')
    if _DOCTYPE.search(raw):
        raise InRtpError('invalid_pacs', 'XML entities are not allowed.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InRtpError('invalid_pacs', 'ISO 20022 XML is not well-formed.') from exc


def normalize_rail(value: Any, *, default: str = RAIL_FEDNOW) -> str:
    text = re.sub(r'[^a-z0-9]', '', str(value or default).strip().lower())
    mapped = RAIL_ALIASES.get(text, CLR_SYS_ALIASES.get(text, text))
    if mapped not in RAILS:
        raise InRtpError('invalid_rail', 'Rail must be fednow or rtp.')
    return mapped


def normalize_uetr(value: Any) -> str:
    text = str(value or '').strip()
    compact = re.sub(r'[^0-9a-fA-F]', '', text)
    if _UETR32.match(compact) and not _UETR.match(text):
        text = '%s-%s-%s-%s-%s' % (
            compact[0:8], compact[8:12], compact[12:16], compact[16:20], compact[20:32],
        )
    if not _UETR.match(text):
        raise InRtpError('invalid_uetr', 'UETR must be an RFC 4122 UUID.')
    return text.lower()


def compose_uetr() -> str:
    return str(uuid.uuid4())


def normalize_end_to_end_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or ''))
    if not (1 <= len(text) <= 35):
        raise InRtpError('invalid_end_to_end_id', 'EndToEndId must be 1-35 characters.')
    return text


def normalize_msg_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9./-]', '', str(value or '').strip())
    if not text:
        raise InRtpError('invalid_msg_id', 'MsgId is required.')
    return text[:35]


def compose_end_to_end_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'E2E', sequence))[:35]


def compose_msg_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'MSG', sequence))[:35]


def compose_tx_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'TX', sequence))[:35]


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    mapped = RETURN_ALIASES.get(text.lower(), text)
    if mapped not in RETURN_REASONS:
        raise InRtpError('invalid_reason', 'Unknown inbound return reason.')
    return mapped


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise InRtpError('invalid_currency', 'Inbound FedNow/RTP credits must be USD.')
    return text


class InstantClock:
    """24/7/365 instant-rail clock. Never queues for cutoff, weekend, or holiday."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz = timezone(timedelta(hours=tz_offset_hours))

    def at(self, now: float) -> datetime:
        return datetime.fromtimestamp(float(now), tz=self.tz)

    def cycle_date(self, now: float) -> str:
        return self.at(now).strftime('%Y%m%d')

    def iso_date(self, now: float) -> str:
        return self.at(now).date().isoformat()

    def snapshot(self, now: float) -> Dict[str, Any]:
        stamp = self.at(now)
        return {
            'instant': True,
            'business_day': True,
            'after_cutoff': False,
            'rail_hours': '24x7',
            'value_date': stamp.date().isoformat(),
            'cycle_date': stamp.strftime('%Y%m%d'),
            'local_time': stamp.strftime('%H:%M'),
        }


def parse_pacs008(text: Any) -> Dict[str, str]:
    """Parse one pacs.008 document into a tag map (local names)."""
    root = parse_xml_safe(text)
    if _local(root.tag) not in {'Document', 'FIToFICstmrCdtTrf', 'pacs.008.001.08'}:
        if _find(root, 'FIToFICstmrCdtTrf', 'CdtTrfTxInf') is None:
            raise InRtpError('invalid_pacs', 'Document is not a pacs.008 credit transfer.')
    return root


def split_pacs_file(text: Any) -> List[str]:
    """Split an operator file into ISO 20022 documents."""
    raw = str(text or '')
    parts = [part.strip() for part in _SPLIT.split(raw) if part.strip()]
    documents = []
    for part in parts:
        if '<Document' in part or 'FIToFICstmrCdtTrf' in part or 'pacs.008' in part:
            documents.append(part)
    if not documents and raw.strip():
        if '<CdtTrfTxInf' in raw or 'UETR' in raw:
            documents.append(raw.strip())
    return documents


def _escape(value: Any) -> str:
    return (
        str(value or '')
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def compose_pacs008(fields: Dict[str, Any]) -> str:
    """Compose a minimal pacs.008.001.08 credit transfer."""
    rail = normalize_rail(fields.get('rail') or RAIL_FEDNOW)
    clr = 'FDN' if rail == RAIL_FEDNOW else 'RTP'
    amount = parse_money(fields.get('amount'))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<FIToFICstmrCdtTrf>'
        '<GrpHdr>'
        '<MsgId>%s</MsgId>'
        '<CreDtTm>%s</CreDtTm>'
        '<NbOfTxs>1</NbOfTxs>'
        '<SttlmInf><SttlmMtd>CLRG</SttlmMtd><ClrSys><Cd>%s</Cd></ClrSys></SttlmInf>'
        '</GrpHdr>'
        '<CdtTrfTxInf>'
        '<PmtId>'
        '<InstrId>%s</InstrId>'
        '<EndToEndId>%s</EndToEndId>'
        '<TxId>%s</TxId>'
        '<UETR>%s</UETR>'
        '</PmtId>'
        '<IntrBkSttlmAmt Ccy="USD">%s</IntrBkSttlmAmt>'
        '<InstgAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstdAgt>'
        '<Dbtr><Nm>%s</Nm></Dbtr>'
        '<DbtrAcct><Id><Othr><Id>%s</Id></Othr></Id></DbtrAcct>'
        '<Cdtr><Nm>%s</Nm></Cdtr>'
        '<CdtrAcct><Id><Othr><Id>%s</Id></Othr></Id></CdtrAcct>'
        '<RmtInf><Ustrd>%s</Ustrd></RmtInf>'
        '</CdtTrfTxInf>'
        '</FIToFICstmrCdtTrf>'
        '</Document>'
    ) % (
        PACS008_NS,
        _escape(fields.get('msg_id') or 'MSG1'),
        _escape(fields.get('created') or '2024-06-14T15:00:00Z'),
        clr,
        _escape(fields.get('instr_id') or fields.get('tx_id') or 'INSTR1'),
        _escape(fields.get('end_to_end_id') or 'E2E1'),
        _escape(fields.get('tx_id') or 'TX1'),
        _escape(fields.get('uetr')),
        money_str(amount),
        _escape(fields.get('sender_aba')),
        _escape(fields.get('receiver_aba')),
        _escape(fields.get('originator_name') or 'ORIGINATOR'),
        _escape(fields.get('originator_account') or ''),
        _escape(fields.get('beneficiary_name') or 'BENEFICIARY'),
        _escape(fields.get('beneficiary_account')),
        _escape(fields.get('memo') or ''),
    )


def compose_pacs004(
    row: 'InboundInstant',
    *,
    return_msg_id: str,
    reason: str,
    receiver_aba: str,
) -> str:
    """pacs.004 payment return of an inbound instant credit."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<PmtRtr>'
        '<GrpHdr><MsgId>%s</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>'
        '<TxInf>'
        '<OrgnlEndToEndId>%s</OrgnlEndToEndId>'
        '<OrgnlUETR>%s</OrgnlUETR>'
        '<OrgnlTxId>%s</OrgnlTxId>'
        '<RtrdIntrBkSttlmAmt Ccy="USD">%s</RtrdIntrBkSttlmAmt>'
        '<InstgAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstdAgt>'
        '<RtrRsnInf><Rsn><Cd>%s</Cd></Rsn></RtrRsnInf>'
        '</TxInf>'
        '</PmtRtr>'
        '</Document>'
    ) % (
        PACS004_NS,
        _escape(return_msg_id),
        _escape(row.end_to_end_id),
        _escape(row.uetr),
        _escape(row.tx_id),
        row.amount,
        _escape(receiver_aba),
        _escape(row.sender_aba),
        _escape(reason),
    )


def _member_ids(root: ET.Element) -> Tuple[str, str]:
    instg = _find(root, 'InstgAgt')
    instd = _find(root, 'InstdAgt')
    sender = _text(instg, 'MmbId') or _text(instg, 'ClrSysMmbId')
    receiver = _text(instd, 'MmbId') or _text(instd, 'ClrSysMmbId')
    return sender, receiver


def _account_id(el: Optional[ET.Element]) -> str:
    if el is None:
        return ''
    for child in el.iter():
        if _local(child.tag) in {'Id', 'IBAN'} and child.text and child.text.strip():
            return child.text.strip()
    return ''


def message_from_pacs(text: Any) -> Dict[str, Any]:
    """Reusable pacs.008 → inbound field map."""
    root = parse_pacs008(text)
    uetr_raw = _text(root, 'UETR')
    if not uetr_raw:
        raise InRtpError('invalid_uetr', 'pacs.008 is missing UETR.')
    amount_el = _find(root, 'IntrBkSttlmAmt')
    if amount_el is None:
        amount_el = _find(root, 'InstdAmt')
    if amount_el is None or not (amount_el.text or '').strip():
        raise InRtpError('invalid_amount', 'Settlement amount is required.')
    normalize_currency(amount_el.get('Ccy') or amount_el.get('ccy') or 'USD')
    amount = parse_money(amount_el.text)
    sender_raw, receiver_raw = _member_ids(root)
    try:
        sender_aba = normalize_aba(sender_raw)
        receiver_aba = normalize_aba(receiver_raw)
        account = normalize_external_account(_account_id(_find(root, 'CdtrAcct')))
    except WireError as exc:
        raise InRtpError(exc.code, exc.message) from exc
    clr = _text(root, 'Cd') or _text(_find(root, 'ClrSys'), 'Prtry')
    try:
        e2e = normalize_end_to_end_id(_text(root, 'EndToEndId'))
        msg_id = normalize_msg_id(_text(root, 'MsgId'))
    except InRtpError:
        raise
    originator = _text(_find(root, 'Dbtr'), 'Nm') or 'ORIGINATOR'
    beneficiary = _text(_find(root, 'Cdtr'), 'Nm') or 'BENEFICIARY'
    return {
        'uetr': normalize_uetr(uetr_raw),
        'end_to_end_id': e2e,
        'msg_id': msg_id,
        'tx_id': _text(root, 'TxId') or e2e,
        'rail': normalize_rail(clr or RAIL_FEDNOW),
        'amount': money_str(amount),
        'sender_aba': sender_aba,
        'receiver_aba': receiver_aba,
        'beneficiary_account': account,
        'originator_account': _account_id(_find(root, 'DbtrAcct')),
        'beneficiary_name': beneficiary,
        'originator_name': originator,
        'memo': normalize_note(_text(root, 'Ustrd') or '', limit=140),
        'raw': str(text or ''),
    }


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """JSON operator payload or XML file → inbound field map."""
    blob = values.get('file') or values.get('pacs') or values.get('xml') or values.get('raw')
    if blob:
        return message_from_pacs(blob)
    uetr = values.get('uetr')
    if not uetr:
        raise InRtpError('invalid_uetr', 'UETR is required.')
    amount = parse_money(values.get('amount'))
    normalize_currency(values.get('currency') or values.get('ccy') or 'USD')
    try:
        sender_aba = normalize_aba(values.get('sender_aba') or values.get('sender'))
        receiver_aba = normalize_aba(values.get('receiver_aba') or values.get('receiver'))
        account = normalize_external_account(values.get('beneficiary_account') or values.get('account'))
    except WireError as exc:
        raise InRtpError(exc.code, exc.message) from exc
    e2e_raw = values.get('end_to_end_id') or values.get('e2e') or values.get('endtoendid')
    msg_raw = values.get('msg_id') or values.get('msgid') or ('MSG' + re.sub(r'[^A-Za-z0-9]', '', str(uetr))[:12])
    originator = str(values.get('originator_name') or values.get('originator') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    return {
        'uetr': normalize_uetr(uetr),
        'end_to_end_id': normalize_end_to_end_id(e2e_raw or ('E2E' + re.sub(r'[^A-Za-z0-9]', '', str(uetr))[:16])),
        'msg_id': normalize_msg_id(msg_raw),
        'tx_id': str(values.get('tx_id') or values.get('txid') or '').strip() or normalize_end_to_end_id(
            e2e_raw or ('TX' + re.sub(r'[^A-Za-z0-9]', '', str(uetr))[:16])
        ),
        'rail': normalize_rail(values.get('rail') or RAIL_FEDNOW),
        'amount': money_str(amount),
        'sender_aba': sender_aba,
        'receiver_aba': receiver_aba,
        'beneficiary_account': account,
        'originator_account': str(values.get('originator_account') or '').strip(),
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'originator_name': originator or 'ORIGINATOR',
        'memo': normalize_note(values.get('memo') or values.get('remittance') or '', limit=140),
        'raw': '',
    }


@dataclass
class InRtpPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('1000000.00')
    fednow_max: Decimal = Decimal('500000.00')
    rtp_max: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_hours: int = -4
    source_id: str = 'KONOHA01'
    receiver_aba: str = DEFAULT_RECEIVER_ABA
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )

    def rail_max(self, rail: str) -> Decimal:
        return self.rtp_max if rail == RAIL_RTP else self.fednow_max

    @classmethod
    def from_env(cls) -> 'InRtpPolicy':
        extra = _env_list('INRTP_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INRTP_RECEIVER_ABA') or DEFAULT_RECEIVER_ABA
        try:
            receiver = normalize_aba(receiver)
        except WireError:
            receiver = DEFAULT_RECEIVER_ABA
        return cls(
            enabled=_env_bool('INRTP_ENABLED', True),
            customer_view=_env_bool('INRTP_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INRTP_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INRTP_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INRTP_MAX', 240)),
            min_amount=_env_money('INRTP_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('INRTP_MAX_AMOUNT', '1000000.00'),
            fednow_max=_env_money('INRTP_FEDNOW_MAX', '500000.00'),
            rtp_max=_env_money('INRTP_RTP_MAX', '1000000.00'),
            dual_control_threshold=_env_money('INRTP_DUAL_CONTROL', '10000.00'),
            tz_offset_hours=_env_int('INRTP_TZ_OFFSET', -4),
            source_id=normalize_source(os.environ.get('INRTP_SOURCE', 'KONOHA01')),
            receiver_aba=receiver,
            customer_return_seconds=max(60, _env_int('INRTP_RETURN_WINDOW', CUSTOMER_RETURN_SECONDS)),
            watchlist=watch,
        )


@dataclass
class InboundInstant:
    inbound_id: str
    uetr: str
    end_to_end_id: str
    msg_id: str
    tx_id: str
    rail: str
    userid: str
    internal_account: str
    amount: str
    sender_aba: str
    receiver_aba: str
    originator_name: str
    originator_account_last4: str
    beneficiary_name: str
    beneficiary_account: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_msg_id: str
    return_reason: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'uetr': self.uetr,
            'end_to_end_id': self.end_to_end_id,
            'msg_id': self.msg_id,
            'tx_id': self.tx_id,
            'rail': self.rail,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'sender_aba': self.sender_aba,
            'receiver_aba': self.receiver_aba,
            'originator_name': self.originator_name,
            'originator_last4': self.originator_account_last4,
            'beneficiary_name': self.beneficiary_name,
            'beneficiary_last4': last4(self.beneficiary_account),
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_msg_id': self.return_msg_id,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'pending_release': self.status == IN_PENDING,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone(row: InboundInstant) -> InboundInstant:
    return InboundInstant(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundInstant:
    return InboundInstant(
        inbound_id=row['inbound_id'],
        uetr=row['uetr'],
        end_to_end_id=row['end_to_end_id'] or '',
        msg_id=row['msg_id'] or '',
        tx_id=row['tx_id'] or '',
        rail=row['rail'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        sender_aba=row['sender_aba'],
        receiver_aba=row['receiver_aba'],
        originator_name=row['originator_name'],
        originator_account_last4=row['originator_account_last4'] or '',
        beneficiary_name=row['beneficiary_name'],
        beneficiary_account=row['beneficiary_account'],
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_msg_id=row['return_msg_id'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
    )


class MemoryInRtpStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundInstant] = {}
        self._by_uetr: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundInstant) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_uetr[row.uetr] = row.inbound_id

    def update(self, row: InboundInstant) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InRtpError('inbound_not_found', 'Inbound instant payment not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_uetr[row.uetr] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundInstant]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_uetr(self, uetr: str) -> Optional[InboundInstant]:
        with self._lock:
            inbound_id = self._by_uetr.get(uetr)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundInstant]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundInstant]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundInstant]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundInstant]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInRtpStore:
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
                    uetr TEXT NOT NULL UNIQUE,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    msg_id TEXT NOT NULL DEFAULT '',
                    tx_id TEXT NOT NULL DEFAULT '',
                    rail TEXT NOT NULL,
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    sender_aba TEXT NOT NULL,
                    receiver_aba TEXT NOT NULL,
                    originator_name TEXT NOT NULL,
                    originator_account_last4 TEXT NOT NULL DEFAULT '',
                    beneficiary_name TEXT NOT NULL,
                    beneficiary_account TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT 'other',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_msg_id TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT ''
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

    def _write(self, conn: sqlite3.Connection, row: InboundInstant) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, uetr, end_to_end_id, msg_id, tx_id, rail, userid,
                internal_account, amount, sender_aba, receiver_aba, originator_name,
                originator_account_last4, beneficiary_name, beneficiary_account,
                purpose, memo, status, value_date, actor, releaser, ofac_hit,
                ofac_match, return_msg_id, return_reason, created_at, updated_at,
                posted_at, returned_at, note, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.uetr, row.end_to_end_id, row.msg_id, row.tx_id,
                row.rail, row.userid, row.internal_account, row.amount, row.sender_aba,
                row.receiver_aba, row.originator_name, row.originator_account_last4,
                row.beneficiary_name, row.beneficiary_account, row.purpose, row.memo,
                row.status, row.value_date, row.actor, row.releaser, int(row.ofac_hit),
                row.ofac_match, row.return_msg_id, row.return_reason, row.created_at,
                row.updated_at, row.posted_at, row.returned_at, row.note, row.batch_id,
            ),
        )

    def put(self, row: InboundInstant) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundInstant) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InRtpError('inbound_not_found', 'Inbound instant payment not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundInstant]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_uetr(self, uetr: str) -> Optional[InboundInstant]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE uetr = ?', (uetr,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundInstant]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundInstant]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundInstant]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundInstant]:
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


class InRtpService:
    def __init__(
        self,
        policy: InRtpPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        instant_clock: Optional[InstantClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.instant_clock = instant_clock or InstantClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InRtpError('inrtp_disabled', 'Inbound instant payments are disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InRtpError('inrtp_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InRtpError('inrtp_forbidden', 'Customers cannot view inbound instant payments.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InRtpError('inrtp_forbidden', 'Customers cannot request inbound returns.')

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
            raise InRtpError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InRtpError('credit_not_allowed', 'Credit accounts cannot receive inbound instant payments.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        if dollars < self.policy.min_amount:
            raise InRtpError('amount_out_of_range', 'Amount is outside the allowed range.')
        cap = self.policy.rail_max(rail)
        if dollars > cap:
            code = 'rtp_amount_exceeded' if rail == RAIL_RTP else 'fednow_amount_exceeded'
            raise InRtpError(code, 'Amount exceeds the %s cap.' % rail)
        if dollars > self.policy.max_amount:
            raise InRtpError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_aba: str) -> None:
        if receiver_aba != self.policy.receiver_aba:
            raise InRtpError('wrong_receiver', 'Message is not addressed to this bank.')

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundInstant:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InRtpError('inbound_not_found', 'Inbound instant payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InRtpError('inrtp_forbidden', 'Not allowed to view this inbound instant payment.')
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
                'uetr': message['uetr'],
                'end_to_end_id': message['end_to_end_id'],
                'msg_id': message['msg_id'],
                'rail': message['rail'],
                'amount': message['amount'],
                'sender_aba': message['sender_aba'],
                'receiver_aba': message['receiver_aba'],
                'originator_name': message['originator_name'],
                'beneficiary_name': message['beneficiary_name'],
                'beneficiary_last4': last4(message['beneficiary_account']),
            },
            'matched_userid': userid or '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.instant_clock.snapshot(now),
        }

    def _evaluate_status(self, *, userid: str, account: str, dollars: Decimal, ofac: ScreenResult) -> str:
        if not userid or not account:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(dollars):
            return IN_PENDING
        return IN_POSTED

    def _credit(self, row: InboundInstant) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = '%s from %s' % (row.rail, row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundInstant) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = '%s return %s' % (row.rail, row.uetr[:12])
        result = self.debit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundInstant) -> InboundInstant:
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
        raise InRtpError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundInstant, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars, message['rail'])
        self._assert_receiver(message['receiver_aba'])
        existing = self.store.get_by_uetr(message['uetr'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InRtpError('inbound_limit', 'Inbound instant payment limit reached.')
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
        ofac = self._screen(message['originator_name'])
        now = float(self.clock())
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            dollars=dollars,
            ofac=ofac,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        purpose = normalize_purpose(values.get('purpose') or 'other')
        try:
            originator_name = normalize_legal_name(message['originator_name'])
        except WireError:
            originator_name = normalize_party(message['originator_name'])[:80] or 'ORIGINATOR'
        try:
            beneficiary_name = normalize_legal_name(message['beneficiary_name'])
        except WireError:
            beneficiary_name = normalize_party(message['beneficiary_name'])[:80] or 'BENEFICIARY'
        row = InboundInstant(
            inbound_id=uuid.uuid4().hex,
            uetr=message['uetr'],
            end_to_end_id=message['end_to_end_id'],
            msg_id=message['msg_id'],
            tx_id=message['tx_id'],
            rail=message['rail'],
            userid=userid or '',
            internal_account=account,
            amount=money_str(dollars),
            sender_aba=message['sender_aba'],
            receiver_aba=message['receiver_aba'],
            originator_name=originator_name,
            originator_account_last4=last4(message.get('originator_account')),
            beneficiary_name=beneficiary_name,
            beneficiary_account=message['beneficiary_account'],
            purpose=purpose,
            memo=message['memo'],
            status=status,
            value_date=self.instant_clock.iso_date(now),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_msg_id='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
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
        messages = split_pacs_file(text)
        if not messages:
            raise InRtpError('invalid_pacs', 'Operator file has no pacs.008 documents.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for raw in messages:
            try:
                values: Dict[str, Any] = {'file': raw, 'purpose': purpose, 'batch_id': batch_id}
                if rail:
                    values['rail'] = rail
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values=values,
                    batch_id=batch_id,
                )
                payload = row.to_dict()
                if created:
                    accepted.append(payload)
                else:
                    duplicates.append(payload)
            except InRtpError as exc:
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
    ) -> InboundInstant:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InRtpError('not_assignable', 'Only unmatched inbound payments can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InRtpError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.beneficiary_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        dollars = parse_money(row.amount)
        status = self._evaluate_status(userid=owner, account=account, dollars=dollars, ofac=ofac)
        row.status = status
        if status == IN_POSTED:
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def override_ofac(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InboundInstant:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InRtpError('not_overridable', 'Only OFAC-held inbound payments can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        dollars = parse_money(row.amount)
        status = self._evaluate_status(
            userid=row.userid,
            account=row.internal_account,
            dollars=dollars,
            ofac=ScreenResult(False, '', 0),
        )
        row.status = status
        if status == IN_POSTED:
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def release(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
    ) -> InboundInstant:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InRtpError('not_releasable', 'Inbound payment is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InRtpError('same_approver', 'A different employee must release this inbound payment.')
        row.releaser = str(actor)
        row.updated_at = float(self.clock())
        row.status = IN_POSTED
        self.store.update(row)
        return self._try_post(row)

    def reject(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundInstant:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InRtpError('not_rejectable', 'Inbound payment cannot be rejected.')
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
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundInstant:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'CUST',
        note: Any = '',
    ) -> InboundInstant:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InRtpError('inrtp_forbidden', 'Not allowed to return this inbound payment.')
        return self._return(row, actor=actor, reason=reason or 'CUST', note=note, force_window=False)

    def _assign_return_id(self, row: InboundInstant) -> None:
        seq = self.store.next_sequence()
        row.return_msg_id = compose_msg_id(self.policy.source_id + 'R', seq)

    def _return(
        self,
        row: InboundInstant,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundInstant:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InRtpError('already_returned', 'Inbound payment is already returned or rejected.')
        code = normalize_return_reason(reason, default='CUST')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            self._assign_return_id(row)
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InRtpError('not_returnable', 'Inbound payment cannot be returned.')
        posted_at = float(row.posted_at or row.created_at)
        if not force_window and (now - posted_at) > self.policy.customer_return_seconds:
            raise InRtpError('return_window_closed', '24-hour exception-return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InRtpError('nsf', 'Insufficient funds to return this inbound payment.', inbound=row)
        if classified != 'ok':
            raise InRtpError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self._assign_return_id(row)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundInstant]:
        """Instant rails do not queue. Hook kept for snapshot / staff refresh."""
        _ = userid
        return []

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InRtpError('inrtp_forbidden', 'Not allowed to view this inbound book.')
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
            'fednow_max': money_str(self.policy.fednow_max),
            'rtp_max': money_str(self.policy.rtp_max),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.instant_clock.snapshot(now),
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


_SERVICE: Optional[InRtpService] = None


def set_service(service: Optional[InRtpService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InRtpService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INRTP_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInRtpStore()
    path = os.environ.get('INRTP_DB', DEFAULT_STORE_PATH)
    return SqliteInRtpStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    instant_clock: Optional[InstantClock] = None,
) -> InRtpService:
    if store is None:
        store = default_store()
    return InRtpService(
        InRtpPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        instant_clock=instant_clock,
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
        'inrtp_forbidden': 403,
        'inrtp_disabled': 403,
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
        'invalid_uetr': 400,
        'invalid_pacs': 400,
        'invalid_rail': 400,
        'invalid_currency': 400,
        'invalid_end_to_end_id': 400,
        'invalid_msg_id': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'fednow_amount_exceeded': 400,
        'rtp_amount_exceeded': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InRtpError) -> Dict[str, Any]:
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
    except InRtpError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InRtps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inrtp_forbidden'}), 403
    return jsonify({'InRtps': service.unmatched_snapshot()}), 200


def handle_preview(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inrtp_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_ingest(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound instant payment ingested' if created else 'Inbound instant payment already posted',
            'inbound': row.to_dict(),
            'InRtps': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('pacs') or values.get('xml') or values.get('text')
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


def handle_assign(service: InRtpService):
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
            'message': 'Inbound instant payment assigned',
            'inbound': row.to_dict(),
            'InRtps': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InRtpService, action: str):
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
            message = 'Inbound instant payment released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound instant payment rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound instant payment returned'
        else:
            raise InRtpError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InRtps': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InRtpService):
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
            reason=values.get('reason') or 'CUST',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound return requested',
            'inbound': row.to_dict(),
            'InRtps': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InRtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InRtps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_inrtp_routes(app, service: InRtpService) -> None:
    @app.route('/listInRtps', methods=['POST', 'GET'])
    def list_inrtps_route():
        return handle_list(service)

    @app.route('/listUnmatchedInRtps', methods=['POST', 'GET'])
    def list_unmatched_inrtps_route():
        return handle_unmatched(service)

    @app.route('/previewInRtp', methods=['POST', 'GET'])
    def preview_inrtp_route():
        return handle_preview(service)

    @app.route('/ingestInRtp', methods=['POST', 'GET'])
    def ingest_inrtp_route():
        return handle_ingest(service)

    @app.route('/ingestInRtpFile', methods=['POST', 'GET'])
    def ingest_inrtp_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInRtp', methods=['POST', 'GET'])
    def assign_inrtp_route():
        return handle_assign(service)

    @app.route('/overrideInRtpOfac', methods=['POST', 'GET'])
    def override_inrtp_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInRtp', methods=['POST', 'GET'])
    def release_inrtp_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInRtp', methods=['POST', 'GET'])
    def reject_inrtp_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInRtp', methods=['POST', 'GET'])
    def return_inrtp_route():
        return _staff_action(service, 'return')

    @app.route('/requestInRtpReturn', methods=['POST', 'GET'])
    def request_inrtp_return_route():
        return handle_request_return(service)

    @app.route('/runDueInRtps', methods=['POST', 'GET'])
    def run_due_inrtps_route():
        return handle_run_due(service)
