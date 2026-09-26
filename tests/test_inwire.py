import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inwire import (
    InWireError,
    InWirePolicy,
    InWireService,
    MemoryInWireStore,
    SqliteInWireStore,
    compose_amount_tag,
    compose_faim,
    compose_return_faim,
    message_from_faim,
    normalize_imad,
    parse_amount_tag,
    parse_di_tag,
    parse_faim,
    split_faim_file,
)
from utility.wire import WireCalendar, compose_imad

ET = timezone(timedelta(hours=-4))
OUR_ABA = '021000021'
SENDER_ABA = '026009593'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def faim_message(**overrides):
    fields = {
        '1100': compose_imad('20240614', 'BOFAUS3N', 42),
        '1110': compose_imad('20240614', 'FRBNY001', 7),
        '1500': '10',
        '2000': compose_amount_tag(Decimal('1250.00')),
        '3100': SENDER_ABA + 'BANK OF AMERICA',
        '3400': OUR_ABA + 'KONOHA BANK',
        '3600': '1001',
        '3700': '999888777',
        '4200': 'ADA LOVELACE*1 MAIN*BOSTON*MA*02101',
        '5000': 'ACME CORP*1 BROADWAY*NEW YORK*NY*10004',
        '6000': 'INVOICE 88',
    }
    fields.update(overrides)
    return compose_faim(fields)


class FoundationTests(unittest.TestCase):
    def test_faim_roundtrip_and_amount_tag(self):
        raw = faim_message()
        fields = parse_faim(raw)
        self.assertEqual(fields['1100'], '20240614BOFAUS3N000042')
        self.assertEqual(parse_amount_tag(fields['2000']), Decimal('1250.00'))
        self.assertEqual(compose_amount_tag(Decimal('1250.00')), '000000125000')
        sender, name = parse_di_tag(fields['3100'])
        self.assertEqual(sender, SENDER_ABA)
        self.assertIn('BANK', name)
        self.assertEqual(compose_faim(fields), raw)

    def test_split_file_keeps_each_imad(self):
        first = faim_message()
        second = faim_message(**{'1100': compose_imad('20240614', 'BOFAUS3N', 43), '2000': compose_amount_tag(Decimal('10.00'))})
        parts = split_faim_file(first + '\n' + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual(message_from_faim(parts[1])['amount'], '10.00')

    def test_imad_and_wrong_receiver(self):
        self.assertEqual(normalize_imad('2024-06-14 BOFAUS3N 000042'), '20240614BOFAUS3N000042')
        with self.assertRaises(InWireError) as ctx:
            normalize_imad('short')
        self.assertEqual(ctx.exception.code, 'invalid_imad')

    def test_return_faim_is_type_16(self):
        store = MemoryInWireStore()
        service = InWireService(
            InWirePolicy(receiver_aba=OUR_ABA),
            store,
            clock=lambda: ts(2024, 6, 14, 11, 0),
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            credit_fn=lambda account, amount, remark: 'Success',
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )
        row, _created = service.ingest(
            actor='teller', actor_type='tier1', values={'file': faim_message()},
        )
        returned = service.return_inbound(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1', reason='acct',
        )
        raw = compose_return_faim(
            returned,
            return_imad=returned.return_imad,
            reason=returned.return_reason,
            receiver_aba=OUR_ABA,
            source='KONOHA01',
        )
        parsed = parse_faim(raw)
        self.assertEqual(parsed['1500'], '16')
        self.assertTrue(parsed['1100'].startswith('20240614'))


class InWireServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
        self.debits = []
        self.credits = []
        self.directory = {'1001': 'alice', '1002': 'alice'}

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

        self.service = InWireService(
            InWirePolicy(
                receiver_aba=OUR_ABA,
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryInWireStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': faim_message(**overrides)},
        )

    def test_happy_path_posts_credit_and_masks_account(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '1250.00')
        self.assertIn('inwire from', self.credits[0][2])
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)

    def test_duplicate_imad_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InWireError) as ctx:
            self._ingest(**{'3400': SENDER_ABA + 'SOME OTHER BANK'})
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        self.assertEqual(self.credits, [])

    def test_unmatched_then_assign_posts(self):
        row, _created = self._ingest(**{'3600': '404404404'})
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(self.credits, [])
        posted = self.service.assign(
            inbound_id=row.inbound_id,
            actor='teller',
            actor_type='tier1',
            customer_id='alice',
            internal_account='1001',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_credit_account_stays_unmatched(self):
        self.directory['1003'] = 'alice'
        row, _created = self._ingest(**{'3600': '1003'})
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(row.note, 'credit_not_allowed')
        self.assertEqual(self.credits, [])

    def test_ofac_hold_does_not_credit(self):
        row, _created = self._ingest(**{'5000': 'MR BLOCKED PERSON LLC'})
        self.assertEqual(row.status, 'held')
        self.assertTrue(row.ofac_hit)
        self.assertEqual(self.credits, [])
        posted = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_other_employee(self):
        row, _created = self._ingest(**{'2000': compose_amount_tag(Decimal('15000.00'))})
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InWireError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_after_cutoff_queues_until_run_due(self):
        self.now[0] = ts(2024, 6, 14, 17, 30)
        row, _created = self._ingest()
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.credits, [])
        self.service.run_due('alice')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_customer_return_before_post_and_after_post(self):
        self.now[0] = ts(2024, 6, 14, 17, 30)
        queued, _created = self._ingest()
        returned = self.service.request_return(
            inbound_id=queued.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertTrue(returned.return_imad)
        self.assertEqual(self.debits, [])

        self.now[0] = ts(2024, 6, 14, 11, 0)
        posted, _ = self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': faim_message(**{'1100': compose_imad('20240614', 'BOFAUS3N', 99)})},
        )
        self.assertEqual(posted.status, 'posted')
        same_day = self.service.request_return(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(same_day.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        self.assertIn('inwire return', self.debits[0][2])

        later, _ = self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': faim_message(**{'1100': compose_imad('20240614', 'BOFAUS3N', 100)})},
        )
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InWireError) as ctx:
            self.service.request_return(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='teller', actor_type='tier1', reason='acct',
        )
        self.assertEqual(staff.status, 'returned')

    def test_return_nsf(self):
        row, _created = self._ingest()

        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(InWireError) as ctx:
            self.service.request_return(
                inbound_id=row.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(self.service.get_inbound(
            inbound_id=row.inbound_id, actor='alice', actor_type='customer',
        ).status, 'posted')

    def test_snapshot_and_sqlite_reopen(self):
        self._ingest()
        snap = self.service.snapshot('alice', actor='alice', actor_type='customer')
        self.assertEqual(snap['ytd_posted'], '1250.00')
        self.assertEqual(snap['posted_count'], 1)
        self.assertEqual(snap['inbounds'][0]['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', snap['inbounds'][0])

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInWireStore(path)
            service = InWireService(
                InWirePolicy(receiver_aba=OUR_ABA),
                store,
                clock=lambda: ts(2024, 6, 14, 11, 0),
                credit_fn=lambda account, amount, remark: 'Success',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
                calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': faim_message()},
            )
            reloaded = SqliteInWireStore(path).get_by_imad(row.imad)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InWireError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': faim_message()},
            )
        self.assertEqual(ctx.exception.code, 'inwire_forbidden')

    def test_file_ingest_counts(self):
        batch = self.service.ingest_file(
            actor='teller',
            actor_type='tier1',
            text=faim_message() + faim_message(**{'1100': compose_imad('20240614', 'BOFAUS3N', 77)}),
        )
        self.assertEqual(batch['accepted_count'], 2)
        self.assertEqual(batch['error_count'], 0)
        again = self.service.ingest_file(actor='teller', actor_type='tier1', text=faim_message())
        self.assertEqual(again['duplicate_count'], 1)
        self.assertEqual(len(self.credits), 2)
