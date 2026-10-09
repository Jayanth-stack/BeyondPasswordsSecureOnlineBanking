import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.pospay import (
    MemoryPosPayStore,
    PosPayError,
    PosPayPolicy,
    PosPayService,
    SqlitePosPayStore,
    compose_amount_field,
    compose_decision_record,
    compose_issue_record,
    compose_presentment_record,
    parse_amount_field,
    parse_issue_record,
    parse_onus,
    parse_presentment_record,
    payees_match,
    split_pospay_file,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_amount_field_roundtrip_and_rejects_xml(self):
        self.assertEqual(compose_amount_field(Decimal('25.00')), '0000002500')
        self.assertEqual(parse_amount_field('0000002500'), Decimal('25.00'))
        with self.assertRaises(PosPayError) as ctx:
            split_pospay_file('<?xml version="1.0"?><issue/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(PosPayError):
            parse_issue_record('<!DOCTYPE issue><issue/>')

    def test_issue_and_presentment_pipe_parse(self):
        issue = compose_issue_record(
            serial='1001', amount=Decimal('25.00'), account='1001',
            payee='Acme Payroll', issue_date='20240601',
        )
        parsed = parse_issue_record(issue)
        self.assertEqual(parsed['serial'], '1001')
        self.assertEqual(parsed['amount'], '25.00')
        self.assertNotIn('1001', parsed['amount_field'])
        present = compose_presentment_record(
            serial='1001', amount=Decimal('25.00'), account='1001',
            payee='ACME, PAYROLL', presentment_id='P1', present_date='20240614',
        )
        shown = parse_presentment_record(present)
        self.assertEqual(shown['presentment_id'], 'P1')
        self.assertTrue(payees_match('Acme Payroll', 'ACME, PAYROLL'))
        self.assertFalse(payees_match('Acme Payroll', 'Other Vendor'))

    def test_onus_and_decision_file_masks_account(self):
        account, serial = parse_onus('1001/7788')
        self.assertEqual(account, '1001')
        self.assertEqual(serial, '7788')
        record = compose_decision_record(
            serial='7788', amount=Decimal('12.00'), account='10019999',
            payee='Acme', decision='return', reason='not_issued', presentment_id='T1',
        )
        self.assertIn('DECN1', record)
        self.assertIn('|A|', record)
        self.assertNotIn('10019999', record)


class PosPayServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def accounts_fn(userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = PosPayPolicy(dual_control_threshold=Decimal('10000.00'), cutoff_hour=14)
        self.service = PosPayService(
            policy,
            MemoryPosPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )

    def _enroll(self, **kwargs):
        return self.service.enroll(
            owner_userid='alice', actor='alice', actor_type='customer',
            account=kwargs.pop('account', '1001'), **kwargs,
        )

    def _issue(self, serial='1001', amount='25.00', payee='Acme Payroll', **kwargs):
        return self.service.add_issue(
            owner_userid='alice', actor='alice', actor_type='customer',
            account=kwargs.pop('account', '1001'),
            serial=serial, amount=amount, payee=payee,
            issue_date=kwargs.pop('issue_date', '20240601'),
            **kwargs,
        )

    def test_snapshot_never_leaks_account_number(self):
        enroll = self._enroll()
        issue = self._issue()
        self.assertNotIn('account', enroll.to_dict())
        self.assertEqual(enroll.to_dict()['account_last4'], '1001')
        self.assertNotIn('account', issue.to_dict())
        item, created = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='25.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(item.status, 'paid')
        self.assertNotIn('account', item.to_dict())
        self.assertEqual(self.debits[0], ('1001', '25.00', 'pospay 1001'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('account', snap['enrollments'][0])
        self.assertNotIn('account', snap['items'][0])

    def test_amount_mismatch_is_exception_until_customer_returns(self):
        self._issue()
        item, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='30.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='p-mis',
        )
        self.assertEqual(item.status, 'exception')
        self.assertEqual(item.reason, 'amount_mismatch')
        self.assertEqual(self.debits, [])
        returned = self.service.decide(
            item_id=item.item_id, actor='alice', actor_type='customer', decision='return',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits, [])

    def test_not_issued_and_payee_mismatch_and_void(self):
        self._issue(serial='55', payee='Acme Payroll')
        missing, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='99', amount='25.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='p-miss',
        )
        self.assertEqual(missing.reason, 'not_issued')
        payee, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='55', amount='25.00',
            payee='Other Vendor', account='1001', customer_id='alice', trace_id='p-payee',
        )
        self.assertEqual(payee.reason, 'payee_mismatch')
        voided = self.service.void_issue(issue_id=self.service.store.find_issue('alice', '1001', '55').issue_id,
                                         actor='alice', actor_type='customer')
        self.assertEqual(voided.status, 'voided')
        presented, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='55', amount='25.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='p-void',
        )
        self.assertEqual(presented.reason, 'voided')

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        self._issue(payee='Blocked Person')
        item, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='25.00',
            payee='Blocked Person', account='1001', customer_id='alice', trace_id='ofac',
        )
        self.assertEqual(item.status, 'held')
        self.assertEqual(self.debits, [])
        with self.assertRaises(PosPayError) as ctx:
            self.service.decide(item_id=item.item_id, actor='alice', actor_type='customer', decision='pay')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(item_id=item.item_id, actor='teller', actor_type='tier1')
        self.assertEqual(released.status, 'paid')
        self.assertEqual(self.debits[0][1], '25.00')

    def test_dual_control_requires_a_different_employee(self):
        self._issue(amount='10000.00')
        item, _ = self.service.ingest(
            actor='maker', actor_type='tier1', serial='1001', amount='10000.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='hv',
        )
        self.assertEqual(item.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(PosPayError) as ctx:
            self.service.release(item_id=item.item_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(item_id=item.item_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'paid')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_credit_account_and_unmatched_then_enroll_rematch(self):
        with self.assertRaises(PosPayError) as ctx:
            self.service.enroll(owner_userid='alice', actor='alice', actor_type='customer', account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        item, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='7', amount='10.00',
            payee='Acme', account='1001', customer_id='alice', trace_id='late-enroll',
        )
        self.assertEqual(item.status, 'unmatched')
        self._issue(serial='7', amount='10.00', payee='Acme')
        rematched = self.service.store.get_item(item.item_id)
        self.assertEqual(rematched.status, 'paid')

    def test_default_return_after_cutoff(self):
        self._issue()
        self.now[0] = ts(2024, 6, 14, 13, 30)
        item, _ = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='40.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='late',
        )
        self.assertEqual(item.status, 'exception')
        self.now[0] = ts(2024, 6, 14, 14, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'returned')
        self.assertEqual(self.debits, [])

    def test_ingest_is_idempotent_by_trace(self):
        self._issue()
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='25.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='dup',
        )
        again, created_again = self.service.ingest(
            actor='teller', actor_type='tier1', serial='1001', amount='25.00',
            payee='Acme Payroll', account='1001', customer_id='alice', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.item_id, again.item_id)
        self.assertEqual(len(self.debits), 1)

    def test_nsf_does_not_mark_issue_paid(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        self._issue()
        with self.assertRaises(PosPayError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1', serial='1001', amount='25.00',
                payee='Acme Payroll', account='1001', customer_id='alice', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        issue = self.service.store.find_issue('alice', '1001', '1001')
        self.assertEqual(issue.status, 'issued')

    def test_file_ingest_and_sqlite_roundtrip_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePosPayStore(path)
            service = PosPayService(
                PosPayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
            )
            service.add_issue(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', serial='88', amount='12.00', payee='Ada Lovelace',
                issue_date='20240601',
            )
            body = compose_presentment_record(
                serial='88', amount=Decimal('12.00'), account='1001',
                payee='Ada Lovelace', presentment_id='FILE1', present_date='20240614',
            )
            items = service.ingest_file(actor='teller', actor_type='tier1', text=body, customer_id='alice')
            self.assertEqual(items[0].status, 'paid')
            reloaded = SqlitePosPayStore(path).get_item(items[0].item_id)
            self.assertEqual(reloaded.account, '1001')
            self.assertNotIn('account', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['account_last4'], '1001')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        self._issue()
        with self.assertRaises(PosPayError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', serial='1001', amount='25.00',
                payee='Acme Payroll', account='1001', customer_id='alice', trace_id='nope',
            )
        self.assertEqual(ctx.exception.code, 'pospay_forbidden')


if __name__ == '__main__':
    unittest.main()
