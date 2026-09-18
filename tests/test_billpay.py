import os
import tempfile
import unittest
from datetime import datetime, timezone

from utility.billpay import (
    AmountError,
    BillPayError,
    BillPayPolicy,
    BillPayService,
    MemoryBillPayStore,
    SqliteBillPayStore,
    add_calendar_months,
    advance_past,
    money_str,
    next_occurrence,
    occurrence_id_for,
    parse_money,
)


JAN31 = datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc).timestamp()


class RecurrenceEngineTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_monthly_end_of_month_clamps(self):
        feb = next_occurrence(JAN31, 'monthly')
        landed = datetime.fromtimestamp(feb, tz=timezone.utc)
        self.assertEqual((landed.year, landed.month, landed.day), (2024, 2, 29))

    def test_add_calendar_months_across_year(self):
        stamp = datetime(2023, 11, 30, tzinfo=timezone.utc).timestamp()
        landed = datetime.fromtimestamp(add_calendar_months(stamp, 2), tz=timezone.utc)
        self.assertEqual((landed.year, landed.month, landed.day), (2024, 1, 30))

    def test_advance_past_skips_missed_weeks(self):
        start = 1_700_000_000.0
        now = start + 21 * 86400 + 1
        nxt = advance_past(start, 'weekly', now)
        self.assertGreater(nxt, now)
        self.assertEqual(int((nxt - start) / 86400) % 7, 0)


class BillPayServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_718_409_600.0]
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
                'checkin': {'Account': 1001, 'Balance': 500},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        self.service = BillPayService(
            BillPayPolicy(),
            MemoryBillPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
        )

    def _add(self, nickname='Power Co', default='1001', **kwargs):
        return self.service.add_biller(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            default_from_account=default,
            **kwargs,
        )

    def test_pay_bill_debits_checking_and_is_idempotent(self):
        biller = self._add(category='utility', routing_last4='0210', account_last4='7788')
        payment, created = self.service.pay_bill(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='75.00', trace_id='bill-1',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'sent')
        self.assertEqual(self.debits, [('1001', '75.00', 'bill pay to Power Co')])
        again, created_again = self.service.pay_bill(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='75.00', trace_id='bill-1',
        )
        self.assertFalse(created_again)
        self.assertEqual(again.payment_id, payment.payment_id)
        self.assertEqual(len(self.debits), 1)

    def test_credit_account_cannot_originate(self):
        with self.assertRaises(BillPayError) as ctx:
            self._add(default='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_foreign_account_rejected(self):
        with self.assertRaises(BillPayError) as ctx:
            self._add(default='9999')
        self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_duplicate_nickname(self):
        self._add()
        with self.assertRaises(BillPayError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'biller_duplicate')

    def test_pause_blocks_pay_unless_forced(self):
        biller = self._add()
        self.service.set_biller_status(
            biller_id=biller.biller_id, actor='alice', actor_type='customer', status='pause',
        )
        with self.assertRaises(BillPayError) as ctx:
            self.service.pay_bill(
                owner_userid='alice', actor='alice', actor_type='customer',
                biller_id=biller.biller_id, amount='10',
            )
        self.assertEqual(ctx.exception.code, 'biller_paused')
        payment, created = self.service.pay_bill(
            owner_userid='alice', actor='teller', actor_type='tier1',
            biller_id=biller.biller_id, amount='10', force=True, trace_id='forced',
        )
        self.assertTrue(created)
        self.assertEqual(payment.status, 'sent')

    def test_nsf_does_not_mark_sent(self):
        biller = self._add()

        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(BillPayError) as ctx:
            self.service.pay_bill(
                owner_userid='alice', actor='alice', actor_type='customer',
                biller_id=biller.biller_id, amount='40', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        stored = self.service.store.get_payment_by_trace('nsf-1')
        self.assertEqual(stored.status, 'nsf')
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd'], '0.00')

    def test_schedule_and_run_due_once_catch_up(self):
        biller = self._add()
        start = self.now[0] + 120
        instruction = self.service.schedule_bill_pay(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='50.00', interval='monthly', start_at=start,
        )
        self.assertEqual(instruction.status, 'active')
        posted = self.service.run_due(owner_userid='alice', actor='alice', actor_type='customer')
        self.assertEqual(posted, [])
        self.now[0] = start + 40 * 86400
        posted = self.service.run_due(owner_userid='alice', actor='alice', actor_type='customer')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].amount, '50.00')
        self.assertEqual(posted[0].occurrence_id, occurrence_id_for(instruction.instruction_id, start))
        reloaded = self.service.store.get_instruction(instruction.instruction_id)
        self.assertGreater(reloaded.next_run, self.now[0])
        again = self.service.run_due(owner_userid='alice', actor='alice', actor_type='customer')
        self.assertEqual(again, [])
        self.assertEqual(len(self.debits), 1)

    def test_staff_return_credits_and_settle_blocks_return(self):
        biller = self._add()
        payment, _ = self.service.pay_bill(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='20.00', trace_id='ret-1',
        )
        with self.assertRaises(BillPayError) as ctx:
            self.service.return_outbound(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'billpay_forbidden')
        returned = self.service.return_outbound(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', reason='unauthorized',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.credits, [('1001', '20.00', 'bill pay returned from Power Co')])
        payment2, _ = self.service.pay_bill(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='9.00', trace_id='set-1',
        )
        settled = self.service.settle_outbound(
            payment_id=payment2.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(settled.status, 'settled')
        with self.assertRaises(BillPayError) as ctx:
            self.service.return_outbound(
                payment_id=payment2.payment_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'already_settled')

    def test_archive_cancels_open_instructions(self):
        biller = self._add()
        instruction = self.service.schedule_bill_pay(
            owner_userid='alice', actor='alice', actor_type='customer',
            biller_id=biller.biller_id, amount='12.00', interval='weekly',
            start_at=self.now[0] + 120,
        )
        self.service.set_biller_status(
            biller_id=biller.biller_id, actor='alice', actor_type='customer', status='archive',
        )
        reloaded = self.service.store.get_instruction(instruction.instruction_id)
        self.assertEqual(reloaded.status, 'cancelled')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteBillPayStore(path)
            service = BillPayService(
                BillPayPolicy(), store, clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 0},
                    'savings': 'None',
                    'credit': 'None',
                },
            )
            biller = service.add_biller(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Rent', default_from_account='1001', category='rent',
            )
            payment, _ = service.pay_bill(
                owner_userid='alice', actor='alice', actor_type='customer',
                biller_id=biller.biller_id, amount='12.00', trace_id='sql-1',
            )
            reloaded = SqliteBillPayStore(path)
            found = reloaded.get_biller(biller.biller_id)
            self.assertIsNotNone(found)
            self.assertEqual(found.nickname, 'Rent')
            posted = reloaded.get_payment_by_trace('sql-1')
            self.assertEqual(posted.payment_id, payment.payment_id)
            self.assertEqual(posted.amount, '12.00')
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
