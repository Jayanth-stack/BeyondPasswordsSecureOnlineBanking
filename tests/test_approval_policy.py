import os
import unittest
from decimal import Decimal

from utility.approval_policy import (
    ACTION_DENY,
    ACTION_ESCALATE,
    ACTION_EXECUTE,
    ACTION_RECORD_FIRST,
    Actor,
    AmountError,
    ApprovalPolicy,
    first_approval_remark,
    parse_amount,
    parse_first_approver,
    pending_visible_to,
    set_policy,
)


def customer(user_id='alice'):
    return Actor(user_id=user_id, role='customer', usertype='customer')


def teller(user_id='emp1', tier=1):
    return Actor(user_id=user_id, role='employee', tier=tier,
                 usertype='tier' + str(tier) if tier in (1, 2) else 'admin')


class ParseAmountTests(unittest.TestCase):
    def test_accepts_two_decimal_money(self):
        self.assertEqual(parse_amount('1000.00'), Decimal('1000.00'))
        self.assertEqual(parse_amount(250), Decimal('250'))

    def test_rejects_negative(self):
        with self.assertRaises(AmountError):
            parse_amount(-1)

    def test_rejects_scientific_notation(self):
        with self.assertRaises(AmountError):
            parse_amount('1e3')

    def test_rejects_too_many_decimals(self):
        with self.assertRaises(AmountError):
            parse_amount('10.001')

    def test_allows_zero(self):
        self.assertEqual(parse_amount(0), Decimal('0'))


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ApprovalPolicy()

    def test_at_threshold_stays_tier1_single_approval(self):
        req = self.policy.classify('transfer', '1000.00')
        self.assertEqual(req.required_tier, 1)
        self.assertEqual(req.required_approvals, 1)
        self.assertFalse(req.dual_control)
        self.assertEqual(req.queue_message(), 'Request to be approved by tier1 employee')

    def test_above_threshold_is_tier2_dual_control(self):
        req = self.policy.classify('transfer', '1000.01')
        self.assertEqual(req.required_tier, 2)
        self.assertEqual(req.required_approvals, 2)
        self.assertTrue(req.dual_control)
        self.assertTrue(req.requires_bank_escalation)
        self.assertEqual(req.queue_message(), 'Request to be approved by tier2 employee')

    def test_deposit_uses_same_thresholds(self):
        req = self.policy.classify('deposit', 2500)
        self.assertEqual(req.required_tier, 2)
        self.assertTrue(req.dual_control)

    def test_independent_dual_control_threshold(self):
        policy = ApprovalPolicy(
            tier2_threshold=Decimal('1000'),
            dual_control_threshold=Decimal('10000'),
        )
        mid = policy.classify('transfer', 5000)
        self.assertEqual(mid.required_tier, 2)
        self.assertEqual(mid.required_approvals, 1)
        high = policy.classify('transfer', 10000.01)
        self.assertEqual(high.required_approvals, 2)


class CustomerReviewTests(unittest.TestCase):
    def setUp(self):
        self.policy = ApprovalPolicy()

    def test_customer_executes_small_fund_request(self):
        decision = self.policy.review(
            customer(), 'fund_request', 500, expected_role='customer'
        )
        self.assertEqual(decision.action, ACTION_EXECUTE)
        self.assertTrue(decision.allowed)

    def test_customer_escalates_high_value(self):
        decision = self.policy.review(
            customer(), 'fund_request', 1500, expected_role='customer'
        )
        self.assertEqual(decision.action, ACTION_ESCALATE)
        self.assertEqual(decision.message, 'Request Sent to Tier2 employee')

    def test_employee_cannot_use_customer_channel(self):
        decision = self.policy.review(
            teller(), 'fund_request', 500, expected_role='customer'
        )
        self.assertEqual(decision.error, 'not_customer')
        self.assertEqual(decision.status_code, 403)


class EmployeeReviewTests(unittest.TestCase):
    def setUp(self):
        self.policy = ApprovalPolicy()

    def test_tier1_executes_small_amount_alone(self):
        decision = self.policy.review(
            teller('emp1', 1), 'transfer', 1000, expected_role='employee'
        )
        self.assertEqual(decision.action, ACTION_EXECUTE)

    def test_tier1_cannot_solo_execute_high_value(self):
        decision = self.policy.review(
            teller('emp1', 1), 'transfer', 1500, expected_role='employee'
        )
        self.assertEqual(decision.action, ACTION_RECORD_FIRST)
        self.assertEqual(decision.message, 'Awaiting second approval')

    def test_tier2_also_records_first_on_high_value(self):
        decision = self.policy.review(
            teller('sup1', 2), 'transfer', 1500, expected_role='employee'
        )
        self.assertEqual(decision.action, ACTION_RECORD_FIRST)

    def test_second_distinct_tier2_executes(self):
        decision = self.policy.review(
            teller('sup2', 2), 'transfer', 1500,
            first_approver_id='emp1', expected_role='employee'
        )
        self.assertEqual(decision.action, ACTION_EXECUTE)

    def test_same_employee_cannot_be_both_approvers(self):
        decision = self.policy.review(
            teller('emp1', 2), 'transfer', 1500,
            first_approver_id='emp1', expected_role='employee'
        )
        self.assertEqual(decision.error, 'same_approver')
        self.assertEqual(decision.status_code, 403)
        self.assertEqual(decision.action, ACTION_DENY)

    def test_tier1_cannot_be_the_checker(self):
        decision = self.policy.review(
            teller('emp2', 1), 'transfer', 1500,
            first_approver_id='emp1', expected_role='employee'
        )
        self.assertEqual(decision.error, 'insufficient_tier')

    def test_customer_cannot_use_employee_channel(self):
        decision = self.policy.review(
            customer(), 'transfer', 100, expected_role='employee'
        )
        self.assertEqual(decision.error, 'not_employee')

    def test_unauthenticated_is_401(self):
        decision = self.policy.review(
            Actor(user_id='', role='anonymous'), 'transfer', 100
        )
        self.assertEqual(decision.status_code, 401)

    def test_tier1_cannot_solo_high_value_when_dual_control_disabled_for_band(self):
        policy = ApprovalPolicy(
            tier2_threshold=Decimal('1000'),
            dual_control_threshold=Decimal('999999'),
        )
        denied = policy.review(
            teller('emp1', 1), 'transfer', 1500, expected_role='employee'
        )
        self.assertEqual(denied.error, 'insufficient_tier')
        allowed = policy.review(
            teller('sup1', 2), 'transfer', 1500, expected_role='employee'
        )
        self.assertEqual(allowed.action, ACTION_EXECUTE)


class ActorTests(unittest.TestCase):
    def test_from_session_customer(self):
        actor = Actor.from_mapping({'userid': 'alice', 'usertype': 'customer'})
        self.assertTrue(actor.is_customer)
        self.assertFalse(actor.is_employee)

    def test_from_session_tier1_infers_tier(self):
        actor = Actor.from_mapping({'userid': 'e1', 'usertype': 'tier1'})
        self.assertEqual(actor.tier, 1)
        self.assertTrue(actor.is_employee)

    def test_emp_tier_overrides_usertype(self):
        actor = Actor.from_mapping({'userid': 'e1', 'usertype': 'employee', 'emp_tier': 2})
        self.assertEqual(actor.tier, 2)

    def test_none_string_tier_falls_back_to_usertype(self):
        actor = Actor.from_mapping({'userid': 'e1', 'usertype': 'tier2', 'emp_tier': 'None'})
        self.assertEqual(actor.tier, 2)


class FirstApproverRemarkTests(unittest.TestCase):
    def test_round_trip(self):
        remark = first_approval_remark('emp9')
        self.assertEqual(parse_first_approver(remark), 'emp9')

    def test_unrelated_remark_is_not_an_approval(self):
        self.assertIsNone(parse_first_approver('Request Denied'))
        self.assertIsNone(parse_first_approver(''))
        self.assertIsNone(parse_first_approver(None))


class PendingQueueFilterTests(unittest.TestCase):
    def setUp(self):
        self.policy = ApprovalPolicy()
        # transaction_no, from, to, approver1, approver2, amount, deposit, status, remark
        self.small = (11, 1001, 1002, '-1', 1, 50, 0, 1, '')
        self.large = (12, 1001, 1002, '-1', 2, 2500, 0, 1, '')
        self.large_first = (13, 1001, 1002, '-1', 2, 2500, 0, 1, first_approval_remark('emp1'))
        self.customer_request = (14, 2001, 1001, 'alice', None, 80, 0, 1, '')
        self.rows = [self.small, self.large, self.large_first, self.customer_request]

    def test_tier1_sees_small_and_unsigned_high_value_as_maker(self):
        visible = pending_visible_to(teller('emp1', 1), self.rows, self.policy)
        ids = [row[0] for row in visible]
        self.assertEqual(ids, [11, 12])

    def test_first_approver_does_not_see_own_pending_checker_item(self):
        visible = pending_visible_to(teller('emp1', 2), self.rows, self.policy)
        ids = [row[0] for row in visible]
        self.assertEqual(ids, [11, 12])

    def test_other_tier2_sees_item_needing_second_approval(self):
        visible = pending_visible_to(teller('sup2', 2), self.rows, self.policy)
        ids = [row[0] for row in visible]
        self.assertEqual(ids, [11, 12, 13])

    def test_unescalated_customer_fund_requests_stay_off_teller_queue(self):
        visible = pending_visible_to(teller('sup2', 2), self.rows, self.policy)
        ids = [row[0] for row in visible]
        self.assertNotIn(14, ids)


class EnvConfigTests(unittest.TestCase):
    def tearDown(self):
        set_policy(None)
        os.environ.pop('APPROVAL_TIER2_THRESHOLD', None)
        os.environ.pop('APPROVAL_DUAL_CONTROL_THRESHOLD', None)

    def test_from_env(self):
        os.environ['APPROVAL_TIER2_THRESHOLD'] = '250'
        os.environ['APPROVAL_DUAL_CONTROL_THRESHOLD'] = '500'
        policy = ApprovalPolicy.from_env()
        self.assertEqual(policy.classify('transfer', 251).required_tier, 2)
        self.assertEqual(policy.classify('transfer', 499).required_approvals, 1)
        self.assertEqual(policy.classify('transfer', 501).required_approvals, 2)


if __name__ == '__main__':
    unittest.main()
