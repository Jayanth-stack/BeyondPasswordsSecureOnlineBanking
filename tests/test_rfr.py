import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.rfr import (
    FedNowClock,
    MemoryRfrStore,
    RfrError,
    RfrPolicy,
    RfrService,
    SqliteRfrStore,
    compose_rfr1,
    compose_rfr_ref,
    mask_rfr_ref,
    normalize_decline,
    normalize_e2e,
    normalize_message_type,
    normalize_reason,
    normalize_requestor_aba,
    parse_rfr1,
    split_rfr1_file,
)
from utility.wire import aba_check_digit_ok

ET = timezone(timedelta(hours=-4))
ABA = '021000021'
ACCOUNT = '1001'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_aba_e2e_reason_and_rfr_ref(self):
        self.assertTrue(aba_check_digit_ok(ABA))
        self.assertEqual(normalize_requestor_aba('21000021'), ABA)
        with self.assertRaises(RfrError) as ctx:
            normalize_requestor_aba('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_aba')
        self.assertEqual(normalize_e2e('fn-pay-77881234'), 'FN-PAY-77881234')
        with self.assertRaises(RfrError) as ctx:
            normalize_e2e('RFI20240614000001')
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        with self.assertRaises(RfrError) as ctx:
            normalize_e2e('RFR20240614000001')
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        with self.assertRaises(RfrError) as ctx:
            normalize_e2e('20240614KONOHA01000001')
        self.assertEqual(ctx.exception.code, 'invalid_e2e')
        self.assertEqual(normalize_reason('fraud'), 'FRAD')
        self.assertEqual(normalize_reason('duplicate'), 'DUPL')
        self.assertEqual(normalize_decline('legal'), 'LEGL')
        with self.assertRaises(RfrError) as ctx:
            normalize_reason('chargeback')
        self.assertEqual(ctx.exception.code, 'invalid_reason')
        self.assertEqual(compose_rfr_ref('20240614', 1), 'RFR20240614000001')
        self.assertEqual(mask_rfr_ref('RFR20240614000001'), 'RFR****0001')
        with self.assertRaises(RfrError) as ctx:
            compose_rfr_ref('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_message_type_rejects_rfi_payments_and_rfp(self):
        self.assertEqual(normalize_message_type('rfr'), 'camt.056')
        self.assertEqual(normalize_message_type('pacs.004'), 'pacs.004')
        self.assertEqual(normalize_message_type('camt.029'), 'camt.029')
        for rejected in ('camt.026', 'camt.028', 'pacs.008', 'pain.013', 'mt199'):
            with self.assertRaises(RfrError) as ctx:
                normalize_message_type(rejected)
            self.assertEqual(ctx.exception.code, 'invalid_message')

    def test_rfr1_parse_rejects_xml(self):
        line = compose_rfr1(
            message_type='camt.056', e2e='FNPAY77881234', reason='DUPL',
            aba=ABA, account=ACCOUNT, amount='25.00', name='Chase Bank',
            reference='req-1', status='open',
        )
        parsed = parse_rfr1(line)
        self.assertEqual(parsed['account'], ACCOUNT)
        self.assertEqual(parsed['e2e'], 'FNPAY77881234')
        with self.assertRaises(RfrError) as ctx:
            parse_rfr1('<?xml version="1.0"?><rfr/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(RfrError):
            split_rfr1_file('<!DOCTYPE html>')
        lines = split_rfr1_file(line + '\n\n' + line)
        self.assertEqual(len(lines), 2)
        with self.assertRaises(RfrError) as ctx:
            parse_rfr1(compose_rfr1(
                message_type='camt.056', e2e='FNPAY77881234', reason='DUPL',
                aba=ABA, account='****1001',
            ))
        self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_fednow_clock_is_24_7_not_fedwire(self):
        clock = FedNowClock(tz_offset_hours=-4)
        sunday = ts(2024, 6, 16, 2, 0)
        weekday_fed_closed = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_open(sunday))
        self.assertTrue(clock.is_open(weekday_fed_closed))
        snap = clock.snapshot(weekday_fed_closed)
        self.assertTrue(snap['always_open'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], 'none')
        self.assertEqual(clock.cycle_date(sunday), '20240616')


class RfrServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
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

        policy = RfrPolicy(dual_control_threshold=Decimal('2500.00'))
        self.service = RfrService(
            policy,
            MemoryRfrStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=FedNowClock(tz_offset_hours=-4),
        )

    def _add(self, nickname='Payroll DDA', name='Ada Lovelace', **kwargs):
        return self.service.add_key(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def _ingest(self, e2e='FNPAY77881234', account='1001', reason='DUPL', name='Chase Bank', **kwargs):
        return self.service.ingest_inquiry(
            actor='teller',
            actor_type='tier1',
            e2e=e2e,
            account=account,
            aba=ABA,
            reason=reason,
            amount=kwargs.pop('amount', '25.00'),
            requestor_name=name,
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_secrets(self):
        key = self._add()
        payload = key.to_dict()
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['last4'], '1001')
        self.assertIn('****', payload['account_masked'])
        inquiry, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(inquiry.status, 'open')
        dumped = inquiry.to_dict()
        self.assertNotIn('e2e_id', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('requestor_aba', dumped)
        self.assertNotIn('rfr_ref', dumped)
        self.assertTrue(dumped['e2e_masked'].startswith('E2E****'))
        posted, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id,
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(posted.message_type, 'pacs.004')
        self.assertTrue(posted.rfr_ref.startswith('RFR'))
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '25.00')
        self.assertTrue(self.debits[0][2].startswith('fednow return '))
        snap = self.service.snapshot('alice')
        self.assertNotIn(inquiry.e2e_id, str(snap))
        self.assertNotIn(posted.rfr_ref, str(snap))
        self.assertNotIn(ABA, str(snap))
        self.assertEqual(snap['ytd_returned'], '25.00')

    def test_ingest_is_idempotent_by_e2e(self):
        first, created = self._ingest(e2e='FNDUP00000001')
        again, created_again = self._ingest(e2e='FNDUP00000001')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inquiry_id, again.inquiry_id)

    def test_weekend_and_after_fed_cutoff_still_posts_return(self):
        self.now[0] = ts(2024, 6, 16, 2, 0)
        self._add()
        inquiry, _ = self._ingest(e2e='FNSUN00000001')
        posted, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id,
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_frad_dual_control_requires_a_different_employee(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNFRAD0000001', reason='FRAD')
        pending, _ = self.service.accept(
            owner_userid='alice', actor='maker', actor_type='tier1',
            inquiry_id=inquiry.inquiry_id,
        )
        self.assertEqual(pending.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(RfrError) as ctx:
            self.service.release_inquiry(inquiry_id=pending.inquiry_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_inquiry(
            inquiry_id=pending.inquiry_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(released.message_type, 'pacs.004')
        self.assertNotIn(released.rfr_ref, released.to_dict())
        self.assertEqual(self.debits[0][1], '25.00')

    def test_high_value_also_needs_dual_control(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNHIGH0000001', reason='DUPL', amount='2500.00')
        pending, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id,
        )
        self.assertEqual(pending.status, 'pending_release')
        self.assertEqual(self.debits, [])

    def test_ofac_hold_blocks_accept_until_staff_override(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNOFAC0000001', name='Blocked Person')
        self.assertEqual(inquiry.status, 'held')
        self.assertTrue(inquiry.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(RfrError) as ctx:
            self.service.accept(
                owner_userid='alice', actor='alice', actor_type='customer',
                inquiry_id=inquiry.inquiry_id,
            )
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        opened = self.service.override_ofac(
            inquiry_id=inquiry.inquiry_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(opened.status, 'open')
        posted, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=opened.inquiry_id,
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.debits[0][1], '25.00')

    def test_decline_posts_camt029_without_debit(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNDECL0000001')
        declined, created = self.service.decline(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id, decline_code='LEGL',
        )
        self.assertTrue(created)
        self.assertEqual(declined.status, 'declined')
        self.assertEqual(declined.message_type, 'camt.029')
        self.assertTrue(declined.rfr_ref.startswith('RFR'))
        self.assertEqual(self.debits, [])
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['declined_count'], 1)
        self.assertEqual(snap['ytd_returned'], '0.00')

    def test_credit_account_and_pause(self):
        with self.assertRaises(RfrError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        key = self._add()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        inquiry, _ = self._ingest(e2e='FNPAUSE000001')
        self.assertEqual(inquiry.status, 'unmatched')

    def test_nsf_does_not_assign_rfr_ref(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        self._add()
        inquiry, _ = self._ingest(e2e='FNNSF00000001')
        with self.assertRaises(RfrError) as ctx:
            self.service.accept(
                owner_userid='alice', actor='alice', actor_type='customer',
                inquiry_id=inquiry.inquiry_id,
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['inquiry'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['inquiry'].rfr_ref, '')

    def test_customer_reverse_window_and_cancel_open(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNREV00000001')
        posted, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id,
        )
        reversed_row = self.service.reverse_inquiry(
            inquiry_id=posted.inquiry_id, actor='alice', actor_type='customer',
            customer_request=True,
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.credits[0][1], '25.00')

        later, _ = self._ingest(e2e='FNLATER000001')
        posted_later, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=later.inquiry_id,
        )
        self.now[0] = ts(2024, 6, 16, 16, 0)
        with self.assertRaises(RfrError) as ctx:
            self.service.reverse_inquiry(
                inquiry_id=posted_later.inquiry_id, actor='alice', actor_type='customer',
                customer_request=True,
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

        pending, _ = self._ingest(e2e='FNCANCEL00001')
        cancelled = self.service.cancel_inquiry(
            inquiry_id=pending.inquiry_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')

    def test_unmatched_claim_correspondent_and_export_masks(self):
        corr = self.service.add_correspondent(
            actor='teller', actor_type='tier1', nickname='Chase NY',
            legal_name='JPMorgan Chase', aba=ABA,
        )
        dumped = corr.to_dict()
        self.assertNotIn('aba', dumped)
        self.assertEqual(dumped['aba_masked'], '****0021')
        inquiry, created = self._ingest(e2e='FNCLAIM000001', account='5555')
        self.assertTrue(created)
        self.assertEqual(inquiry.status, 'unmatched')
        self.assertEqual(self.debits, [])
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=inquiry.inquiry_id, default_account='1001',
            nickname='Payroll DDA', legal_name='Ada Lovelace',
        )
        self.assertEqual(claimed.status, 'open')
        self.assertEqual(claimed.userid, 'alice')
        posted, _ = self.service.accept(
            owner_userid='alice', actor='alice', actor_type='customer',
            inquiry_id=claimed.inquiry_id,
        )
        exported = self.service.export_file(actor='teller', actor_type='tier2', userid='alice')
        self.assertIn('RFR1|', exported)
        self.assertNotIn(posted.e2e_id, exported)
        self.assertNotIn(ABA, exported)
        self.assertNotIn(posted.rfr_ref, exported)

    def test_open_request_expires_after_ten_days(self):
        self._add()
        inquiry, _ = self._ingest(e2e='FNTTL00000001')
        self.now[0] = ts(2024, 6, 25, 16, 0)
        expired = self.service.run_due('alice')
        self.assertEqual(expired[0].status, 'expired')
        self.assertEqual(expired[0].inquiry_id, inquiry.inquiry_id)
        self.assertEqual(self.debits, [])

    def test_duplicate_key_and_sqlite_roundtrip(self):
        self._add()
        with self.assertRaises(RfrError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteRfrStore(path)
            service = RfrService(
                RfrPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=FedNowClock(tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', default_account='1001',
            )
            reloaded = SqliteRfrStore(path).get_key(key.key_id)
            self.assertEqual(reloaded.default_account, '1001')
            self.assertNotIn('fingerprint', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['last4'], '1001')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
