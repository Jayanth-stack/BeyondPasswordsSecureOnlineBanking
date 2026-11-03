import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.noc import (
    AchClock,
    MemoryNocStore,
    NocError,
    NocPolicy,
    NocService,
    SqliteNocStore,
    compose_noc1,
    compose_noc_ref,
    mask_noc_ref,
    normalize_change,
    normalize_message_type,
    normalize_rdfi_aba,
    normalize_refuse,
    normalize_trace,
    parse_noc1,
    split_noc1_file,
)
from utility.wire import aba_check_digit_ok

ET = timezone(timedelta(hours=-4))
ABA = '021000021'
DEST = '555566667777'
FUNDING = '1001'
TRACE = '021000010000001'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_aba_trace_change_and_noc_ref(self):
        self.assertTrue(aba_check_digit_ok(ABA))
        self.assertEqual(normalize_rdfi_aba('21000021'), ABA)
        with self.assertRaises(NocError) as ctx:
            normalize_rdfi_aba('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_aba')
        self.assertEqual(normalize_trace(TRACE), TRACE)
        with self.assertRaises(NocError) as ctx:
            normalize_trace('NOC20240614000001')
        self.assertEqual(ctx.exception.code, 'invalid_trace')
        with self.assertRaises(NocError) as ctx:
            normalize_trace('RFR20240614000001')
        self.assertEqual(ctx.exception.code, 'invalid_trace')
        with self.assertRaises(NocError) as ctx:
            normalize_trace('20240614KONOHA01000001')
        self.assertEqual(ctx.exception.code, 'invalid_trace')
        self.assertEqual(normalize_change('account'), 'C01')
        self.assertEqual(normalize_change('routing'), 'C02')
        self.assertEqual(normalize_refuse('misrouted'), 'C61')
        with self.assertRaises(NocError) as ctx:
            normalize_change('chargeback')
        self.assertEqual(ctx.exception.code, 'invalid_reason')
        self.assertEqual(compose_noc_ref('20240614', 1), 'NOC20240614000001')
        self.assertEqual(mask_noc_ref('NOC20240614000001'), 'NOC****0001')
        with self.assertRaises(NocError) as ctx:
            compose_noc_ref('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_message_type_rejects_payments_rfr_and_rfi(self):
        self.assertEqual(normalize_message_type('noc'), 'COR')
        self.assertEqual(normalize_message_type('CORA'), 'CORA')
        self.assertEqual(normalize_message_type('refuse'), 'CORR')
        for rejected in ('PPD', 'CCD', 'WEB', 'pacs.008', 'camt.056', 'camt.026'):
            with self.assertRaises(NocError) as ctx:
                normalize_message_type(rejected)
            self.assertEqual(ctx.exception.code, 'invalid_message')

    def test_noc1_parse_rejects_xml(self):
        line = compose_noc1(
            message_type='COR', trace=TRACE, change_code='C01',
            aba=ABA, account=DEST, corrected='888899990000', name='Chase Bank',
        )
        parsed = parse_noc1(line)
        self.assertEqual(parsed['account'], DEST)
        self.assertEqual(parsed['trace'], TRACE)
        with self.assertRaises(NocError) as ctx:
            parse_noc1('<?xml version="1.0"?><noc/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(NocError):
            split_noc1_file('<!DOCTYPE html>')
        lines = split_noc1_file(line + '\n\n' + line)
        self.assertEqual(len(lines), 2)
        with self.assertRaises(NocError) as ctx:
            parse_noc1(compose_noc1(
                message_type='COR', trace=TRACE, change_code='C01',
                aba=ABA, account='****7777',
            ))
        self.assertEqual(ctx.exception.code, 'invalid_account')

    def test_ach_clock_uses_1645_not_fedwire_or_fednow(self):
        clock = AchClock(tz_offset_hours=-4)
        friday_open = ts(2024, 6, 14, 11, 0)
        after_ach_before_fed = ts(2024, 6, 14, 16, 50)
        sunday = ts(2024, 6, 16, 11, 0)
        juneteenth = ts(2024, 6, 19, 11, 0)
        self.assertTrue(clock.is_open(friday_open))
        self.assertFalse(clock.is_open(after_ach_before_fed))
        self.assertFalse(clock.is_open(sunday))
        self.assertFalse(clock.is_open(juneteenth))
        snap = clock.snapshot(after_ach_before_fed)
        self.assertFalse(snap['always_open'])
        self.assertTrue(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '16:45')
        self.assertEqual(clock.cycle_date(sunday), '20240617')


class NocServiceTests(unittest.TestCase):
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

        policy = NocPolicy(fee_amount=Decimal('0.20'))
        self.service = NocService(
            policy,
            MemoryNocStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=AchClock(tz_offset_hours=-4),
        )

    def _add(self, nickname='Payroll dest', name='Ada Lovelace', **kwargs):
        return self.service.add_key(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            dest_aba=kwargs.pop('dest_aba', ABA),
            dest_account=kwargs.pop('dest_account', DEST),
            funding_account=kwargs.pop('funding_account', FUNDING),
            **kwargs,
        )

    def _ingest(self, trace=TRACE, account=DEST, aba=ABA, change_code='C01', name='Chase Bank', **kwargs):
        return self.service.ingest_notice(
            actor='teller',
            actor_type='tier1',
            trace=trace,
            account=account,
            aba=aba,
            change_code=change_code,
            rdfi_name=name,
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_secrets(self):
        key = self._add()
        payload = key.to_dict()
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('dest_account', payload)
        self.assertNotIn('dest_aba', payload)
        self.assertEqual(payload['last4'], '7777')
        self.assertIn('****', payload['account_masked'])
        notice, created = self._ingest(corrected='888899990000')
        self.assertTrue(created)
        self.assertEqual(notice.status, 'open')
        dumped = notice.to_dict()
        self.assertNotIn('trace_id', dumped)
        self.assertNotIn('fingerprint', dumped)
        self.assertNotIn('dest_aba', dumped)
        self.assertNotIn('noc_ref', dumped)
        self.assertNotIn('corrected_data', dumped)
        self.assertTrue(dumped['trace_masked'].startswith('TRC****'))
        posted, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=notice.notice_id,
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(posted.message_type, 'CORA')
        self.assertTrue(posted.noc_ref.startswith('NOC'))
        self.assertEqual(self.debits[0][0], FUNDING)
        self.assertEqual(self.debits[0][1], '0.20')
        self.assertTrue(self.debits[0][2].startswith('noc fee '))
        reloaded = self.service.store.get_key(key.key_id)
        self.assertEqual(reloaded.dest_account, '888899990000')
        snap = self.service.snapshot('alice')
        self.assertNotIn(notice.trace_id, str(snap))
        self.assertNotIn(posted.noc_ref, str(snap))
        self.assertNotIn(ABA, str(snap))
        self.assertNotIn('888899990000', str(snap))
        self.assertEqual(snap['ytd_fees'], '0.20')
        self.assertEqual(snap['applied_count'], 1)

    def test_ingest_is_idempotent_by_trace(self):
        first, created = self._ingest(trace='021000010000099', corrected='111122223333')
        again, created_again = self._ingest(trace='021000010000099', corrected='111122223333')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.notice_id, again.notice_id)

    def test_weekend_and_after_ach_cutoff_queues_apply(self):
        self._add()
        self.now[0] = ts(2024, 6, 16, 11, 0)
        notice, _ = self._ingest(trace='021000010000088', corrected='111122223333')
        queued, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=notice.notice_id,
        )
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 16, 50)
        weekday, _ = self._ingest(trace='021000010000077', corrected='222233334444')
        after, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=weekday.notice_id,
        )
        self.assertEqual(after.status, 'queued')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 11, 0)
        due = self.service.run_due('alice')
        self.assertTrue(any(row.status == 'posted' for row in due))
        self.assertEqual(len(self.debits), 2)

    def test_routing_change_dual_control_requires_a_different_employee(self):
        key = self._add()
        notice, _ = self._ingest(
            trace='021000010000066', change_code='C02', corrected='011401533',
        )
        pending, _ = self.service.apply(
            owner_userid='alice', actor='maker', actor_type='tier1',
            notice_id=notice.notice_id,
        )
        self.assertEqual(pending.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(NocError) as ctx:
            self.service.release_notice(notice_id=pending.notice_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_notice(
            notice_id=pending.notice_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(released.message_type, 'CORA')
        self.assertNotIn(released.noc_ref, released.to_dict())
        self.assertEqual(self.debits[0][1], '0.20')
        reloaded = self.service.store.get_key(key.key_id)
        self.assertEqual(reloaded.dest_aba, '011401533')

    def test_ofac_hold_blocks_apply_until_staff_override(self):
        self._add()
        notice, _ = self._ingest(trace='021000010000055', name='Blocked Person', corrected='333344445555')
        self.assertEqual(notice.status, 'held')
        self.assertTrue(notice.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(NocError) as ctx:
            self.service.apply(
                owner_userid='alice', actor='alice', actor_type='customer',
                notice_id=notice.notice_id,
            )
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        opened = self.service.override_ofac(
            notice_id=notice.notice_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(opened.status, 'open')
        posted, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=opened.notice_id,
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.debits[0][1], '0.20')

    def test_refuse_posts_corr_without_debit_or_mutation(self):
        key = self._add()
        original = key.dest_account
        notice, _ = self._ingest(trace='021000010000044', corrected='999900001111')
        refused, created = self.service.refuse(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=notice.notice_id, refuse_code='C65',
        )
        self.assertTrue(created)
        self.assertEqual(refused.status, 'refused')
        self.assertEqual(refused.message_type, 'CORR')
        self.assertTrue(refused.noc_ref.startswith('NOC'))
        self.assertEqual(self.debits, [])
        self.assertEqual(self.service.store.get_key(key.key_id).dest_account, original)
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['refused_count'], 1)
        self.assertEqual(snap['ytd_fees'], '0.00')

    def test_credit_account_and_pause(self):
        with self.assertRaises(NocError) as ctx:
            self._add(funding_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        key = self._add()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        notice, _ = self._ingest(trace='021000010000033', corrected='121212121212')
        self.assertEqual(notice.status, 'unmatched')

    def test_nsf_does_not_assign_noc_ref_or_mutate(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        key = self._add()
        notice, _ = self._ingest(trace='021000010000022', corrected='444455556666')
        with self.assertRaises(NocError) as ctx:
            self.service.apply(
                owner_userid='alice', actor='alice', actor_type='customer',
                notice_id=notice.notice_id,
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['inquiry'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['inquiry'].noc_ref, '')
        self.assertEqual(self.service.store.get_key(key.key_id).dest_account, DEST)

    def test_customer_reverse_restores_destination_and_cancel_open(self):
        key = self._add()
        notice, _ = self._ingest(trace='021000010000011', corrected='777788889999')
        posted, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=notice.notice_id,
        )
        self.assertEqual(self.service.store.get_key(key.key_id).dest_account, '777788889999')
        reversed_row = self.service.reverse_notice(
            notice_id=posted.notice_id, actor='alice', actor_type='customer',
            customer_request=True,
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.credits[0][1], '0.20')
        self.assertEqual(self.service.store.get_key(key.key_id).dest_account, DEST)

        later, _ = self._ingest(trace='021000010000012', corrected='101010101010')
        posted_later, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=later.notice_id,
        )
        self.now[0] = ts(2024, 6, 16, 11, 0)
        with self.assertRaises(NocError) as ctx:
            self.service.reverse_notice(
                notice_id=posted_later.notice_id, actor='alice', actor_type='customer',
                customer_request=True,
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

        current = self.service.store.get_key(key.key_id)
        pending, _ = self._ingest(
            trace='021000010000013', account=current.dest_account, aba=current.dest_aba,
            corrected='202020202020',
        )
        cancelled = self.service.cancel_notice(
            notice_id=pending.notice_id, actor='alice', actor_type='customer',
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
        notice, created = self._ingest(trace='021000010000014', account='999988887777', corrected='121234345656')
        self.assertTrue(created)
        self.assertEqual(notice.status, 'unmatched')
        self.assertEqual(self.debits, [])
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=notice.notice_id, funding_account=FUNDING,
            nickname='Payroll dest', legal_name='Ada Lovelace',
        )
        self.assertEqual(claimed.status, 'open')
        self.assertEqual(claimed.userid, 'alice')
        posted, _ = self.service.apply(
            owner_userid='alice', actor='alice', actor_type='customer',
            notice_id=claimed.notice_id,
        )
        exported = self.service.export_file(actor='teller', actor_type='tier2', userid='alice')
        self.assertIn('NOC1|', exported)
        self.assertNotIn(posted.trace_id, exported)
        self.assertNotIn(ABA, exported)
        self.assertNotIn(posted.noc_ref, exported)
        self.assertNotIn('121234345656', exported)

    def test_open_notice_expires_after_six_days(self):
        self._add()
        notice, _ = self._ingest(trace='021000010000015', corrected='303030303030')
        self.now[0] = ts(2024, 6, 21, 11, 0)
        expired = self.service.run_due('alice')
        self.assertEqual(expired[0].status, 'expired')
        self.assertEqual(expired[0].notice_id, notice.notice_id)
        self.assertEqual(self.debits, [])

    def test_duplicate_key_and_sqlite_roundtrip(self):
        self._add()
        with self.assertRaises(NocError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteNocStore(path)
            service = NocService(
                NocPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=AchClock(tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', dest_aba=ABA,
                dest_account=DEST, funding_account=FUNDING,
            )
            reloaded = SqliteNocStore(path).get_key(key.key_id)
            self.assertEqual(reloaded.dest_account, DEST)
            self.assertNotIn('fingerprint', reloaded.to_dict())
            self.assertNotIn('dest_account', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['last4'], '7777')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
