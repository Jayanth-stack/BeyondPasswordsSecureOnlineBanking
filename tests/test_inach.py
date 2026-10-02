import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inach import (
    InAchError,
    InAchPolicy,
    InAchService,
    MemoryInAchStore,
    SqliteInAchStore,
    compose_amount_field,
    compose_nacha,
    compose_return_nacha,
    compose_trace,
    message_from_nacha,
    normalize_trace,
    parse_amount_field,
    split_nacha_file,
    split_nacha_records,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))
OUR_ABA = '021000021'
SENDER_ABA = '026009593'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def nacha_file(**overrides):
    fields = {
        'trace': compose_trace(SENDER_ABA, 42),
        'amount': '1250.00',
        'sender_aba': SENDER_ABA,
        'receiver_aba': OUR_ABA,
        'beneficiary_account': '1001',
        'beneficiary_name': 'ADA LOVELACE',
        'originator_name': 'ACME CORP',
        'sec': 'PPD',
        'rail': 'sameday',
        'txn_code': '22',
        'creation_date': '240614',
        'effective_date': '240614',
        'sender_name': 'BANK OF AMERICA',
        'receiver_name': 'KONOHA BANK',
    }
    fields.update(overrides)
    return compose_nacha(fields)


class FoundationTests(unittest.TestCase):
    def test_nacha_roundtrip_and_amount_field(self):
        raw = nacha_file()
        records = split_nacha_records(raw)
        self.assertTrue(all(len(item) == 94 for item in records))
        self.assertEqual(len(records) % 10, 0)
        parsed = message_from_nacha(raw)
        self.assertEqual(parsed['trace'], compose_trace(SENDER_ABA, 42))
        self.assertEqual(parsed['amount'], '1250.00')
        self.assertEqual(parsed['receiver_aba'], OUR_ABA)
        self.assertEqual(parsed['sender_aba'], SENDER_ABA)
        self.assertEqual(parsed['beneficiary_account'], '1001')
        self.assertEqual(parsed['rail'], 'sameday')
        self.assertEqual(parsed['sec'], 'PPD')
        self.assertEqual(compose_amount_field(Decimal('1250.00')), '0000125000')
        self.assertEqual(parse_amount_field('0000125000'), Decimal('1250.00'))

    def test_split_file_keeps_each_trace(self):
        first = nacha_file()
        second = nacha_file(trace=compose_trace(SENDER_ABA, 43), amount='10.00', rail='standard')
        parts = split_nacha_file(first + '\n' + second)
        self.assertGreaterEqual(len(parts), 2)
        traces = {item['trace'] for item in parts}
        self.assertIn(compose_trace(SENDER_ABA, 42), traces)
        self.assertIn(compose_trace(SENDER_ABA, 43), traces)

    def test_trace_and_xml_rejected(self):
        self.assertEqual(normalize_trace('026009590000042'), compose_trace(SENDER_ABA, 42))
        with self.assertRaises(InAchError) as ctx:
            normalize_trace('short')
        self.assertEqual(ctx.exception.code, 'invalid_trace')
        with self.assertRaises(InAchError) as xml_ctx:
            split_nacha_records('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><a>&xxe;</a>')
        self.assertEqual(xml_ctx.exception.code, 'invalid_nacha')

    def test_debit_txn_rejected(self):
        raw = nacha_file(txn_code='27')
        with self.assertRaises(InAchError) as ctx:
            message_from_nacha(raw)
        self.assertEqual(ctx.exception.code, 'invalid_txn_code')

    def test_return_nacha_is_addenda_99(self):
        store = MemoryInAchStore()
        service = InAchService(
            InAchPolicy(receiver_aba=OUR_ABA),
            store,
            clock=lambda: ts(2024, 6, 14, 11, 0),
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            credit_fn=lambda account, amount, remark: 'Success',
            calendar=WireCalendar(cutoff_hour=16, cutoff_minute=45, tz_offset_hours=-4),
        )
        row, _created = service.ingest(
            actor='teller', actor_type='tier1', values={'file': nacha_file()},
        )
        returned = service.return_inbound(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1', reason='acct',
        )
        raw = compose_return_nacha(
            returned,
            return_trace=returned.return_trace,
            reason=returned.return_reason,
            receiver_aba=OUR_ABA,
        )
        records = split_nacha_records(raw)
        addenda = [item for item in records if item.startswith('799')]
        self.assertEqual(len(addenda), 1)
        self.assertEqual(addenda[0][3:6], 'R03')
        entries = [item for item in records if item.startswith('6')]
        self.assertEqual(entries[0][1:3], '21')


class InAchServiceTests(unittest.TestCase):
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

        self.service = InAchService(
            InAchPolicy(
                receiver_aba=OUR_ABA,
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryInAchStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=WireCalendar(cutoff_hour=16, cutoff_minute=45, tz_offset_hours=-4),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': nacha_file(**overrides)},
        )

    def test_sameday_happy_path_posts_credit_and_masks_account(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(row.rail, 'sameday')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '1250.00')
        self.assertIn('ach from', self.credits[0][2])
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)

    def test_standard_queues_until_tplus1(self):
        row, _created = self._ingest(rail='standard', trace=compose_trace(SENDER_ABA, 7))
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.credits, [])
        self.service.run_due('alice')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_duplicate_trace_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InAchError) as ctx:
            self._ingest(receiver_aba=SENDER_ABA)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        self.assertEqual(self.credits, [])

    def test_unmatched_then_assign_posts(self):
        row, _created = self._ingest(beneficiary_account='404404404')
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
        row, _created = self._ingest(beneficiary_account='1003')
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(row.note, 'credit_not_allowed')
        self.assertEqual(self.credits, [])

    def test_ofac_hold_does_not_credit(self):
        row, _created = self._ingest(originator_name='BLOCKED PERSON')
        self.assertEqual(row.status, 'held')
        self.assertTrue(row.ofac_hit)
        self.assertEqual(self.credits, [])
        posted = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_other_employee(self):
        row, _created = self._ingest(amount='15000.00', trace=compose_trace(SENDER_ABA, 88))
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InAchError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_sameday_after_cutoff_queues_until_run_due(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        row, _created = self._ingest(trace=compose_trace(SENDER_ABA, 91))
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
        queued, _created = self._ingest(rail='standard', trace=compose_trace(SENDER_ABA, 11))
        returned = self.service.request_return(
            inbound_id=queued.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertTrue(returned.return_trace)
        self.assertEqual(self.debits, [])

        posted, _ = self._ingest(trace=compose_trace(SENDER_ABA, 12))
        self.assertEqual(posted.status, 'posted')
        same_window = self.service.request_return(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(same_window.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        self.assertIn('ach return', self.debits[0][2])

        later, _ = self._ingest(trace=compose_trace(SENDER_ABA, 13))
        self.now[0] = ts(2024, 6, 19, 10, 0)
        with self.assertRaises(InAchError) as ctx:
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
        with self.assertRaises(InAchError) as ctx:
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
            store = SqliteInAchStore(path)
            service = InAchService(
                InAchPolicy(receiver_aba=OUR_ABA),
                store,
                clock=lambda: ts(2024, 6, 14, 11, 0),
                credit_fn=lambda account, amount, remark: 'Success',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
                calendar=WireCalendar(cutoff_hour=16, cutoff_minute=45, tz_offset_hours=-4),
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': nacha_file()},
            )
            reloaded = SqliteInAchStore(path).get_by_trace(row.trace)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InAchError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': nacha_file()},
            )
        self.assertEqual(ctx.exception.code, 'inach_forbidden')

    def test_file_ingest_counts(self):
        batch = self.service.ingest_file(
            actor='teller',
            actor_type='tier1',
            text=nacha_file() + '\n' + nacha_file(trace=compose_trace(SENDER_ABA, 77), amount='10.00'),
        )
        self.assertEqual(batch['accepted_count'], 2)
        self.assertEqual(batch['error_count'], 0)
        again = self.service.ingest_file(actor='teller', actor_type='tier1', text=nacha_file())
        self.assertEqual(again['duplicate_count'], 1)
        self.assertEqual(len(self.credits), 2)

    def test_sameday_cap(self):
        with self.assertRaises(InAchError) as ctx:
            self._ingest(amount='1000000.01', trace=compose_trace(SENDER_ABA, 55), rail='sameday')
        self.assertEqual(ctx.exception.code, 'sameday_amount_exceeded')
