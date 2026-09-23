import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.rtp import (
    AmountError,
    InstantClock,
    MemoryRtpStore,
    RtpError,
    RtpPolicy,
    RtpService,
    SqliteRtpStore,
    aba_check_digit_ok,
    compose_end_to_end_id,
    compose_pacs008,
    compose_pain013,
    compose_tx_id,
    compose_uetr,
    compute_fee,
    money_str,
    normalize_aba,
    normalize_rail,
    parse_money,
    rfp_expiry,
)
from utility.wire import screen_name

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_rails(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')
        self.assertEqual(normalize_rail('TCH'), 'rtp')
        self.assertEqual(normalize_rail('fed-now'), 'fednow')
        with self.assertRaises(RtpError) as ctx:
            normalize_rail('sepa')
        self.assertEqual(ctx.exception.code, 'invalid_rail')

    def test_reuses_aba_checksum(self):
        self.assertTrue(aba_check_digit_ok('021000021'))
        self.assertEqual(normalize_aba('21000021'), '021000021')
        with self.assertRaises(RtpError) as ctx:
            normalize_aba('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_aba')

    def test_iso20022_ids_and_maps(self):
        self.assertEqual(compose_end_to_end_id('fednow', '20240615', 1), 'FN20240615000001')
        self.assertEqual(compose_end_to_end_id('rtp', '20240615', 2), 'RTP20240615000002')
        self.assertEqual(compose_tx_id('fednow', '20240615', 3), 'FRB20240615000003')
        self.assertEqual(compose_tx_id('rtp', '20240615', 4), 'TCH20240615000004')
        uetr = compose_uetr()
        self.assertEqual(len(uetr), 36)
        pacs = compose_pacs008(
            rail='fednow', amount=Decimal('25.00'), aba='021000021',
            end_to_end_id='FN20240615000001', uetr=uetr, message_id='FDNKONOHA01202406150001',
            created='2024-06-15T11:00:00', creditor_name='Ada Lovelace',
        )
        self.assertEqual(pacs['ClrSys'], 'FDN')
        self.assertEqual(pacs['LclInstrm'], 'FDN')
        self.assertEqual(pacs['ChrgBr'], 'SLEV')
        self.assertEqual(pacs['IntrBkSttlmAmt']['Ccy'], 'USD')
        pain = compose_pain013(
            rail='rtp', amount=Decimal('40.00'), aba='021000021',
            end_to_end_id='RTP20240615000002', message_id='TCHKONOHA01202406150002',
            created='2024-06-15T11:00:00', expiry='2024-06-22', debtor_name='Ally',
        )
        self.assertEqual(pain['LclInstrm'], 'RFP')
        self.assertEqual(pain['ClrSys'], 'TCH')

    def test_fee_per_rail_or_waived(self):
        self.assertEqual(compute_fee(Decimal('100.00'), 'fednow'), Decimal('1.00'))
        self.assertEqual(compute_fee(Decimal('100.00'), 'rtp'), Decimal('0.45'))
        self.assertEqual(compute_fee(Decimal('100.00'), 'fednow', waived=True), Decimal('0.00'))

    def test_instant_clock_is_always_open(self):
        clock = InstantClock(tz_offset_hours=-4)
        saturday = ts(2024, 6, 15, 23, 0)
        snap = clock.snapshot(saturday)
        self.assertFalse(snap['after_cutoff'])
        self.assertTrue(snap['business_day'])
        self.assertEqual(snap['hours'], '24/7')
        self.assertEqual(clock.value_date(saturday).isoformat(), '2024-06-15')
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertEqual(clock.value_date(july4).isoformat(), '2024-07-04')

    def test_rfp_expiry_is_seven_days(self):
        created = ts(2024, 6, 15, 11, 0)
        self.assertEqual(rfp_expiry(created) - created, 7 * 24 * 3600)

    def test_ofac_still_comes_from_wire(self):
        hit = screen_name('Mr Blocked Person LLC')
        self.assertTrue(hit.hit)


class RtpServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 23, 0)]
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

        self.service = RtpService(
            RtpPolicy(fednow_fee=Decimal('1.00'), rtp_fee=Decimal('0.45'), dual_control_threshold=Decimal('10000.00')),
            MemoryRtpStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=InstantClock(tz_offset_hours=-4),
        )

    def _add(self, nickname='Chase Checking', name='Ada Lovelace', **kwargs):
        return self.service.add_counterparty(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            aba=kwargs.pop('aba', '021000021'),
            account_number=kwargs.pop('account_number', '77881234'),
            street=kwargs.pop('street', '1 Federal St'),
            city=kwargs.pop('city', 'New York'),
            state=kwargs.pop('state', 'NY'),
            postal=kwargs.pop('postal', '10004'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_number(self):
        party = self._add()
        payload = party.to_dict()
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['account_last4'], '1234')
        snap = self.service.snapshot('alice')
        self.assertNotIn('account_number', snap['counterparties'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='100.00', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertNotIn('account_number', payment.to_dict())
        self.assertEqual(payment.status, 'completed')
        self.assertTrue(payment.uetr)
        self.assertTrue(payment.end_to_end_id.startswith('FN20240615'))
        self.assertTrue(payment.tx_id.startswith('FRB20240615'))
        self.assertEqual(self.debits[0], ('1001', '100.00', 'fednow to Chase Checking'))
        self.assertEqual(self.debits[1][1], '1.00')

    def test_weekend_and_holiday_still_complete_instantly(self):
        party = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='40.00', rail='rtp', trace_id='sat',
        )
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.rail, 'rtp')
        self.assertEqual(payment.fee, '0.45')
        self.assertTrue(payment.end_to_end_id.startswith('RTP'))
        self.assertEqual(len(self.debits), 2)

    def test_same_trace_is_idempotent(self):
        party = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len([row for row in self.debits if row[1] == '40.00']), 1)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        party = self._add(nickname='Blocked', name='Blocked Person')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(RtpError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '80.00')

    def test_dual_control_requires_a_different_employee(self):
        party = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            counterparty_id=party.counterparty_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(RtpError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_credit_account_pause_and_archive(self):
        party = self._add()
        with self.assertRaises(RtpError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                counterparty_id=party.counterparty_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_counterparty_status(
            counterparty_id=party.counterparty_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(RtpError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                counterparty_id=party.counterparty_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'counterparty_paused')
        closed = self.service.set_counterparty_status(
            counterparty_id=party.counterparty_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(RtpError) as ctx:
            self.service.set_counterparty_status(
                counterparty_id=party.counterparty_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_nsf_does_not_assign_uetr(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        party = self._add()
        with self.assertRaises(RtpError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                counterparty_id=party.counterparty_id, amount='30.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].uetr, '')
        self.assertEqual(ctx.exception.extra['payment'].end_to_end_id, '')

    def test_completed_is_irrevocable(self):
        party = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='60.00', trace_id='c1',
        )
        with self.assertRaises(RtpError) as ctx:
            self.service.recall_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')
        with self.assertRaises(RtpError) as ctx:
            self.service.cancel_payment(payment_id=payment.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')
        with self.assertRaises(RtpError) as ctx:
            self.service.complete_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

    def test_customer_can_cancel_pending_not_completed(self):
        party = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            counterparty_id=party.counterparty_id, amount='10000.00', trace_id='q1',
        )
        cancelled = self.service.cancel_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')

    def test_request_for_payment_credits_on_staff_accept(self):
        party = self._add(nickname='Payroll Co')
        request, created = self.service.request_payment(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='250.00', rail='rtp', trace_id='rfp-1',
        )
        self.assertTrue(created)
        self.assertEqual(request.status, 'requested')
        self.assertTrue(request.end_to_end_id.startswith('RTP'))
        self.assertEqual(self.credits, [])
        again, created_again = self.service.request_payment(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='250.00', rail='rtp', trace_id='rfp-1',
        )
        self.assertFalse(created_again)
        self.assertEqual(again.request_id, request.request_id)
        accepted = self.service.accept_request(
            request_id=request.request_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(accepted.status, 'accepted')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'rfp from Payroll Co'))
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_collected'], '250.00')
        self.assertNotIn('account_number', snap['requests'][0])

    def test_rfp_ofac_and_expiry(self):
        party = self._add(nickname='Blocked', name='Blocked Person')
        request, _ = self.service.request_payment(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=party.counterparty_id, amount='20.00', trace_id='rfp-ofac',
        )
        self.assertEqual(request.status, 'held')
        self.assertEqual(request.end_to_end_id, '')
        with self.assertRaises(RtpError) as ctx:
            self.service.accept_request(request_id=request.request_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        opened = self.service.override_rfp_ofac(
            request_id=request.request_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(opened.status, 'requested')
        self.assertTrue(opened.end_to_end_id)

        live = self._add(nickname='Vendor', account_number='44556677')
        expiring, _ = self.service.request_payment(
            owner_userid='alice', actor='alice', actor_type='customer',
            counterparty_id=live.counterparty_id, amount='15.00', trace_id='rfp-exp',
        )
        self.now[0] = expiring.expires_at + 1
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'expired')
        with self.assertRaises(RtpError) as ctx:
            self.service.accept_request(request_id=expiring.request_id, actor='teller', actor_type='tier1')
        self.assertIn(ctx.exception.code, {'already_expired', 'not_acceptable'})

    def test_sqlite_roundtrip_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteRtpStore(path)
            service = RtpService(
                RtpPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=InstantClock(tz_offset_hours=-4),
            )
            party = service.add_counterparty(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', aba='021000021',
                account_number='99887766', street='1 Federal St', city='New York',
                state='NY', postal='10004', default_account='1001',
            )
            reloaded = SqliteRtpStore(path).get_counterparty(party.counterparty_id)
            self.assertEqual(reloaded.account_number, '99887766')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['account_last4'], '7766')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
