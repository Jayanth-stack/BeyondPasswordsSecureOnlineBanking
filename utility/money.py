"""Canonical money amounts for banking operations.

Existing routes parse amounts with float() and only reject negatives, so
'abc', '', NaN, and extra fractional digits either 500 or silently round.
This module is the single parser for user-facing money fields: Decimal,
two-cent precision, no silent rounding, no scientific-notation surprises.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Optional

TWOPLACES = Decimal('0.01')
DEFAULT_MIN = Decimal('0.01')
DEFAULT_MAX = Decimal('1000000000.00')

# Integers, optional fraction up to 2 digits. No exponent form.
_AMOUNT_RE = re.compile(r'^[+-]?(?:\d+)(?:\.\d{1,2})?$')


class AmountError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_amount'):
        super().__init__(message)
        self.code = code


def _from_string(text: str) -> Decimal:
    stripped = text.strip()
    if not stripped:
        raise AmountError('Enter a valid amount', 'invalid_amount')
    if stripped[0] in '+-':
        rest = stripped[1:]
    else:
        rest = stripped
    if rest.startswith('.'):
        # ".5" is ambiguous in a banking form; require a leading digit.
        raise AmountError('Enter a valid amount', 'invalid_amount')
    if not _AMOUNT_RE.match(stripped):
        raise AmountError('Enter a valid amount', 'invalid_amount')
    try:
        value = Decimal(stripped)
    except InvalidOperation as exc:
        raise AmountError('Enter a valid amount', 'invalid_amount') from exc
    return value


def parse_amount(
    value: Any,
    *,
    min_amount: Decimal = DEFAULT_MIN,
    max_amount: Decimal = DEFAULT_MAX,
    allow_zero: bool = False,
) -> Decimal:
    """Parse a user-supplied amount into a quantized Decimal.

    Rejects None, bools, NaN/Inf, more than two decimal places, negatives,
    and values outside [min_amount, max_amount]. Zero is rejected unless
    allow_zero=True (min_amount is then treated as 0).
    """
    if value is None or isinstance(value, bool):
        raise AmountError('Enter a valid amount', 'invalid_amount')

    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise AmountError('Enter a valid amount', 'invalid_amount')
        # Round-trip through a 2-dp string so 10.1 stays 10.10, not binary noise.
        amount = _from_string(format(value, '.2f'))
    elif isinstance(value, str):
        amount = _from_string(value)
    else:
        raise AmountError('Enter a valid amount', 'invalid_amount')

    if not amount.is_finite():
        raise AmountError('Enter a valid amount', 'invalid_amount')
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise AmountError('Amount cannot have more than two decimal places', 'invalid_amount')
    try:
        amount = amount.quantize(TWOPLACES)
    except InvalidOperation as exc:
        raise AmountError('Enter a valid amount', 'invalid_amount') from exc

    if amount < 0:
        raise AmountError('Enter a valid amount', 'invalid_amount')

    floor = Decimal('0.00') if allow_zero else min_amount
    if amount < floor:
        raise AmountError('Enter a valid amount', 'invalid_amount')
    if amount > max_amount:
        raise AmountError('Amount exceeds the allowed limit', 'amount_too_large')
    return amount


def canonical_amount(value: Any, **kwargs) -> str:
    """Fixed two-decimal string used in fingerprints and receipts."""
    return format(parse_amount(value, **kwargs), 'f')


def try_canonical_amount(value: Any) -> Optional[str]:
    try:
        return canonical_amount(value)
    except AmountError:
        return None
