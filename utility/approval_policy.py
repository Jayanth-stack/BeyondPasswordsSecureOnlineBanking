"""Amount-threshold and dual-control approval policy.

Existing bank code routed `amount > 1000` to a single tier-2 employee. This
module is the reusable replacement: classify an operation by amount, then
decide whether an actor may execute, must wait for a second distinct
approver, or must escalate from a customer to the bank.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import os
from typing import Any, List, Optional, Sequence


OPERATION_TRANSFER = 'transfer'
OPERATION_DEPOSIT = 'deposit'
OPERATION_FUND_REQUEST = 'fund_request'
OPERATION_WITHDRAWAL = 'withdrawal'
OPERATION_CHEQUE_ISSUE = 'cheque_issue'

ACTION_EXECUTE = 'execute'
ACTION_RECORD_FIRST = 'record_first'
ACTION_ESCALATE = 'escalate'
ACTION_DENY = 'deny'
ACTION_QUEUE = 'queue'

FIRST_APPROVAL_PREFIX = 'FIRST:'

DEFAULT_TIER2_THRESHOLD = Decimal('1000')
DEFAULT_DUAL_CONTROL_THRESHOLD = Decimal('1000')

# CREATE TABLE Transactions column order
TXN_NO = 0
TXN_FROM = 1
TXN_TO = 2
TXN_APPROVER1 = 3
TXN_APPROVER2 = 4
TXN_AMOUNT = 5
TXN_DEPOSIT = 6
TXN_STATUS = 7
TXN_REMARK = 8

EMPLOYEE_USERTYPES = frozenset({'admin', 'employee', 'tier1', 'tier2'})


class AmountError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_amount'):
        super().__init__(message)
        self.code = code


def parse_amount(value: Any) -> Decimal:
    """Parse a money amount for policy comparison.

    Matches the historical `float(amount) > 1000` exclusive threshold, but
    rejects junk that `float()` would coerce (`inf`, empty, scientific
    notation). Two decimal places max; no silent rounding.
    """
    if value is None or isinstance(value, bool):
        raise AmountError('Enter a valid amount')
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise AmountError('Enter a valid amount')
        amount = Decimal(str(value))
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or raw[0] in 'eE' or any(c in raw for c in 'eE'):
            raise AmountError('Enter a valid amount')
        try:
            amount = Decimal(raw)
        except InvalidOperation as exc:
            raise AmountError('Enter a valid amount') from exc
    else:
        raise AmountError('Enter a valid amount')

    if not amount.is_finite():
        raise AmountError('Enter a valid amount')
    if amount < 0:
        raise AmountError('Enter a valid amount', 'non_positive_amount')

    quantized = amount.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    if amount != quantized:
        raise AmountError('Amount cannot have more than two decimal places', 'too_many_decimals')
    if amount > Decimal('1000000000000'):
        raise AmountError('Amount exceeds maximum', 'amount_too_large')
    return amount


def parse_first_approver(remark: Any) -> Optional[str]:
    if not remark or not isinstance(remark, str):
        return None
    if not remark.startswith(FIRST_APPROVAL_PREFIX):
        return None
    actor_id = remark[len(FIRST_APPROVAL_PREFIX):].strip()
    return actor_id or None


def first_approval_remark(employee_id: str) -> str:
    return FIRST_APPROVAL_PREFIX + str(employee_id)


def _threshold_from_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise AmountError('Invalid approval threshold configuration') from exc
    if value < 0 or not value.is_finite():
        raise AmountError('Invalid approval threshold configuration')
    return value


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    tier: Optional[int] = None
    usertype: str = ''

    @property
    def is_employee(self) -> bool:
        return self.role == 'employee' and self.tier is not None and self.tier >= 1

    @property
    def is_customer(self) -> bool:
        return self.role == 'customer'

    @classmethod
    def from_mapping(cls, mapping: Any) -> 'Actor':
        if mapping is None:
            return cls(user_id='', role='anonymous', usertype='')
        user_id = str(mapping.get('userid') or mapping.get('customer_id') or '')
        usertype = str(mapping.get('usertype') or '')
        if usertype == 'customer':
            return cls(user_id=user_id, role='customer', usertype=usertype)
        if usertype in EMPLOYEE_USERTYPES:
            tier = mapping.get('emp_tier')
            if tier in (None, '', 'None'):
                tier = {
                    'tier1': 1,
                    'tier2': 2,
                    'admin': 3,
                    'employee': 1,
                }.get(usertype, 1)
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                tier = 1
            return cls(user_id=user_id, role='employee', tier=tier, usertype=usertype)
        if not user_id:
            return cls(user_id='', role='anonymous', usertype=usertype)
        return cls(user_id=user_id, role='unknown', usertype=usertype)


@dataclass(frozen=True)
class Requirement:
    required_tier: int
    required_approvals: int
    maker_tier: int = 1
    distinct_approvers: bool = True
    operation: str = OPERATION_TRANSFER
    amount: Optional[Decimal] = None

    @property
    def dual_control(self) -> bool:
        return self.required_approvals >= 2

    @property
    def requires_bank_escalation(self) -> bool:
        """Customer cannot self-execute; bank employees must take over."""
        return self.required_tier >= 2 or self.dual_control

    def queue_message(self) -> str:
        # Preserve the historical string so existing dashboards keep working.
        return 'Request to be approved by tier' + str(self.required_tier) + ' employee'


@dataclass(frozen=True)
class ApprovalDecision:
    allowed: bool
    action: str
    status_code: int
    message: str
    error: Optional[str] = None
    requirement: Optional[Requirement] = None

    def flask_payload(self) -> dict:
        payload = {
            'message': self.message,
            'action': self.action,
        }
        if self.error:
            payload['error'] = self.error
        if self.requirement is not None:
            payload['required_tier'] = self.requirement.required_tier
            payload['required_approvals'] = self.requirement.required_approvals
        return payload


def flask_error(decision: Optional[ApprovalDecision]):
    """Return a Flask (json, status) tuple when the actor may not proceed."""
    if decision is None or (decision.allowed and decision.action != ACTION_DENY):
        return None
    try:
        from flask import jsonify
    except ImportError:
        return decision.flask_payload(), decision.status_code
    return jsonify(decision.flask_payload()), decision.status_code


class ApprovalPolicy:
    def __init__(
        self,
        tier2_threshold: Decimal = DEFAULT_TIER2_THRESHOLD,
        dual_control_threshold: Decimal = DEFAULT_DUAL_CONTROL_THRESHOLD,
        maker_tier: int = 1,
    ):
        self.tier2_threshold = Decimal(tier2_threshold)
        self.dual_control_threshold = Decimal(dual_control_threshold)
        self.maker_tier = int(maker_tier)

    @classmethod
    def from_env(cls) -> 'ApprovalPolicy':
        return cls(
            tier2_threshold=_threshold_from_env(
                'APPROVAL_TIER2_THRESHOLD', DEFAULT_TIER2_THRESHOLD
            ),
            dual_control_threshold=_threshold_from_env(
                'APPROVAL_DUAL_CONTROL_THRESHOLD', DEFAULT_DUAL_CONTROL_THRESHOLD
            ),
            maker_tier=int(os.getenv('APPROVAL_MAKER_TIER', '1')),
        )

    def classify(self, operation: str, amount: Any) -> Requirement:
        parsed = parse_amount(amount)
        required_tier = 2 if parsed > self.tier2_threshold else 1
        required_approvals = 2 if parsed > self.dual_control_threshold else 1
        return Requirement(
            required_tier=required_tier,
            required_approvals=required_approvals,
            maker_tier=self.maker_tier,
            distinct_approvers=required_approvals >= 2,
            operation=operation,
            amount=parsed,
        )

    def review(
        self,
        actor: Actor,
        operation: str,
        amount: Any,
        first_approver_id: Optional[str] = None,
        expected_role: Optional[str] = None,
    ) -> ApprovalDecision:
        try:
            requirement = self.classify(operation, amount)
        except AmountError as exc:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=400,
                message=str(exc),
                error=exc.code,
            )

        if actor.role in ('anonymous', '') or not actor.user_id:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=401,
                message='Not logged In',
                error='unauthenticated',
                requirement=requirement,
            )

        if expected_role == 'employee' and not actor.is_employee:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=403,
                message='Not authorized to approve transactions',
                error='not_employee',
                requirement=requirement,
            )
        if expected_role == 'customer' and not actor.is_customer:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=403,
                message='Not authorized to approve this request',
                error='not_customer',
                requirement=requirement,
            )

        if actor.is_customer:
            return self._review_customer(actor, requirement)

        if not actor.is_employee:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=403,
                message='Not authorized to approve transactions',
                error='not_employee',
                requirement=requirement,
            )

        return self._review_employee(actor, requirement, first_approver_id)

    def _review_customer(self, actor: Actor, requirement: Requirement) -> ApprovalDecision:
        if requirement.requires_bank_escalation:
            return ApprovalDecision(
                allowed=True,
                action=ACTION_ESCALATE,
                status_code=200,
                message='Request Sent to Tier2 employee',
                requirement=requirement,
            )
        return ApprovalDecision(
            allowed=True,
            action=ACTION_EXECUTE,
            status_code=200,
            message='done',
            requirement=requirement,
        )

    def _review_employee(
        self,
        actor: Actor,
        requirement: Requirement,
        first_approver_id: Optional[str],
    ) -> ApprovalDecision:
        if first_approver_id and first_approver_id == actor.user_id:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=403,
                message='A different employee must provide the second approval',
                error='same_approver',
                requirement=requirement,
            )

        if requirement.required_approvals <= 1:
            if actor.tier < requirement.required_tier:
                return ApprovalDecision(
                    allowed=False,
                    action=ACTION_DENY,
                    status_code=403,
                    message='Not authorized to approve this amount',
                    error='insufficient_tier',
                    requirement=requirement,
                )
            return ApprovalDecision(
                allowed=True,
                action=ACTION_EXECUTE,
                status_code=200,
                message='done',
                requirement=requirement,
            )

        if not first_approver_id:
            if actor.tier < requirement.maker_tier:
                return ApprovalDecision(
                    allowed=False,
                    action=ACTION_DENY,
                    status_code=403,
                    message='Not authorized to approve this amount',
                    error='insufficient_tier',
                    requirement=requirement,
                )
            return ApprovalDecision(
                allowed=True,
                action=ACTION_RECORD_FIRST,
                status_code=200,
                message='Awaiting second approval',
                requirement=requirement,
            )

        if actor.tier < requirement.required_tier:
            return ApprovalDecision(
                allowed=False,
                action=ACTION_DENY,
                status_code=403,
                message='Not authorized to complete dual-control approval',
                error='insufficient_tier',
                requirement=requirement,
            )
        return ApprovalDecision(
            allowed=True,
            action=ACTION_EXECUTE,
            status_code=200,
            message='done',
            requirement=requirement,
        )


def pending_visible_to(
    actor: Actor,
    rows: Sequence[Sequence[Any]],
    policy: Optional[ApprovalPolicy] = None,
) -> List[Any]:
    """Filter pending Transactions rows to those the actor can act on."""
    policy = policy or get_policy()
    visible: List[Any] = []
    for row in rows:
        if not row:
            continue
        amount = row[TXN_AMOUNT] if len(row) > TXN_AMOUNT else None
        deposit = row[TXN_DEPOSIT] if len(row) > TXN_DEPOSIT else 0
        remark = row[TXN_REMARK] if len(row) > TXN_REMARK else ''
        assigned_tier = row[TXN_APPROVER2] if len(row) > TXN_APPROVER2 else None
        # Customer fund requests leave approver2 NULL until they escalate.
        # Those stay on the customer queue, matching historical listing.
        if assigned_tier in (None, '', 0) and not parse_first_approver(remark):
            continue
        operation = OPERATION_DEPOSIT if deposit else OPERATION_TRANSFER
        decision = policy.review(
            actor, operation, amount, parse_first_approver(remark), expected_role='employee'
        )
        if decision.action in (ACTION_EXECUTE, ACTION_RECORD_FIRST):
            visible.append(row)
    return visible


_policy: Optional[ApprovalPolicy] = None


def get_policy() -> ApprovalPolicy:
    global _policy
    if _policy is None:
        _policy = ApprovalPolicy.from_env()
    return _policy


def set_policy(policy: Optional[ApprovalPolicy]) -> None:
    global _policy
    _policy = policy


def classify_amount(operation: str, amount: Any) -> Requirement:
    return get_policy().classify(operation, amount)
