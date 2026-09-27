import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inrtp import (
    InRtpError,
    InRtpPolicy,
    InRtpService,
    InstantClock,
    MemoryInRtpStore,
    SqliteInRtpStore,
    compose_pacs004,
    compose_pacs008,
    compose_uetr,
    message_from_pacs,
    normalize_rail,
    normalize_uetr,
    parse_xml_safe,
    split_pacs_file,
)

ET = timezone(timedelta(hours=-4))
OUR_ABA = '021000021'
SENDER_ABA = '026009593'
UETR_A = '550e8400-e29b-41d4-a716-446655440000'
UETR_B = '550e8400-e29b-41d4-a716-446655440001'
UETR_C = '550e8400-e29b-41d4-a716-446655440002'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def pacs_message(**overrides):
    fields = {
        'uetr': UETR_A,
        'end_to_end_id': 'E2E20240614001',
        'msg_id': 'MSG20240614001',
        'tx_id': 'TX20240614001',
        'rail': 'fednow',
        'amount': '1250.00',
        'sender_aba': SENDER_ABA,
        'receiver_aba': OUR_ABA,
        'beneficiary_account': '1001',
        'originator_account': '999888777',
        'beneficiary_name': 'ADA LOVELACE',
        'originator_name': 'ACME CORP',
        'memo': 'INVOICE 88',
    }
    fields.update(overrides)
    return compose_pacs008(fields)


class FoundationTests(unittest.TestCase):
    def test_pacs_roundtrip_and_uetr(self):
        raw = pacs_message()
        message = message_from_pacs(raw)
        self.assertEqual(message['uetr'], UETR_A)
        self.assertEqual(message['amount'], '1250.00')
        self.assertEqual(message['sender_aba'], SENDER_ABA)
        self.assertEqual(message['rail'], 'fednow')
        self.assertEqual(normalize_uetr('550E8400E29B41D4A716446655440000'), UETR_A)
        self.assertEqual(normalize_rail('FDN'), 'fednow')
        self.assertEqual(normalize_rail('TCH'), 'rtp')
        with self.assertRaises(InRtpError) as ctx:
            normalize_uetr('not-a-uuid')
        self.assertEqual(ctx.exception.code, 'invalid_uetr')

    def test_split_file_keeps_each_uetr(self):
        first = pacs_message()
        second = pacs_message(uetr=UETR_B, amount='10.00', end_to_end_id='E2E2', msg_id='MSG2')
        parts = split_pacs_file(first + '\n' + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual(message_from_pacs(parts[1])['amount'], '10.00')

    def test_xxe_rejected(self):
        with self.assertRaises(InRtpError) as ctx:
            parse_xml_safe('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Document>&xxe;</Document>')
        self.assertEqual(ctx.exception.code, 'invalid_pacs')

    def test_return_pacs004_carries_original_uetr(self):
        store = MemoryInRtpStore()
        service = InRtpService(
            InRtpPolicy(receiver_aba=OUR_ABA),
            store,
            clock=lambda: ts(2024, 6, 14, 11, 0),
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            credit_fn=lambda account, amount, remark: 'Success',
        )
        row, _created = service.ingest(
            actor='teller', actor_type='tier1', values={'file': pacs_message()},
        )
        returned = service.return_inbound(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1', reason='AC03',
        )
        raw = compose_pacs004(
            returned,
            return_msg_id=returned.return_msg_id,
            reason=returned.return_reason,
            receiver_aba=OUR_ABA,
        )
        self.assertIn(UETR_A, raw)
        self.assertIn('AC03', raw)
        self.assertIn('PmtRtr', raw)


class InRtpServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 23, 0)]
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

        self.service = InRtpService(
            InRtpPolicy(
                receiver_aba=OUR_ABA,
                dual_control_threshold=Decimal('10000.00'),
                fednow_max=Decimal('500000.00'),
                rtp_max=Decimal('1000000.00'),
            ),
            MemoryInRtpStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            instant_clock=InstantClock(tz_offset_hours=-4),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': pacs_message(**overrides)},
        )

    def test_happy_path_posts_credit_and_masks_account(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '1250.00')
        self.assertIn('fednow from', self.credits[0][2])
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)

    def test_weekend_night_still_posts_instantly(self):
        # Saturday 23:00 — Fedwire would queue; instant rails post now.
        self.assertEqual(datetime.fromtimestamp(self.now[0], tz=ET).weekday(), 5)
        row, _created = self._ingest()
        self.assertEqual(row.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.service.run_due('alice'), [])

    def test_duplicate_uetr_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InRtpError) as ctx:
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
        row, _created = self._ingest(originator_name='MR BLOCKED PERSON LLC')
        self.assertEqual(row.status, 'held')
        self.assertTrue(row.ofac_hit)
        self.assertEqual(self.credits, [])
        posted = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_other_employee(self):
        row, _created = self._ingest(amount='15000.00')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InRtpError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_fednow_cap(self):
        with self.assertRaises(InRtpError) as ctx:
            self._ingest(amount='500000.01')
        self.assertEqual(ctx.exception.code, 'fednow_amount_exceeded')

    def test_rtp_cap_allows_fednow_overflow(self):
        row, created = self._ingest(rail='rtp', amount='600000.00', uetr=UETR_B)
        self.assertTrue(created)
        self.assertEqual(row.rail, 'rtp')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InRtpError) as ctx:
            self._ingest(rail='rtp', amount='1000000.01', uetr=UETR_C)
        self.assertEqual(ctx.exception.code, 'rtp_amount_exceeded')

    def test_customer_return_before_post_and_after_post(self):
        held, _created = self._ingest(originator_name='OFAC TESTNAME', uetr=UETR_B)
        returned = self.service.request_return(
            inbound_id=held.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertTrue(returned.return_msg_id)
        self.assertEqual(self.debits, [])

        posted, _ = self._ingest()
        self.assertEqual(posted.status, 'posted')
        same_window = self.service.request_return(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(same_window.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        self.assertIn('fednow return', self.debits[0][2])

        later, _ = self._ingest(uetr=UETR_C, end_to_end_id='E2ELATER', msg_id='MSGLATER')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InRtpError) as ctx:
            self.service.request_return(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='teller', actor_type='tier1', reason='AC03',
        )
        self.assertEqual(staff.status, 'returned')
        self.assertEqual(staff.return_reason, 'AC03')

    def test_return_nsf(self):
        row, _created = self._ingest()

        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(InRtpError) as ctx:
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
        self.assertTrue(snap['clock']['instant'])
        self.assertFalse(snap['clock']['after_cutoff'])
        self.assertEqual(snap['inbounds'][0]['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', snap['inbounds'][0])

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInRtpStore(path)
            service = InRtpService(
                InRtpPolicy(receiver_aba=OUR_ABA),
                store,
                clock=lambda: ts(2024, 6, 15, 23, 0),
                credit_fn=lambda account, amount, remark: 'Success',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': pacs_message()},
            )
            reloaded = SqliteInRtpStore(path).get_by_uetr(row.uetr)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InRtpError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': pacs_message()},
            )
        self.assertEqual(ctx.exception.code, 'inrtp_forbidden')

    def test_file_ingest_counts(self):
        batch = self.service.ingest_file(
            actor='teller',
            actor_type='tier1',
            text=pacs_message() + pacs_message(uetr=UETR_B, end_to_end_id='E2EB', msg_id='MSGB'),
        )
        self.assertEqual(batch['accepted_count'], 2)
        self.assertEqual(batch['error_count'], 0)
        again = self.service.ingest_file(actor='teller', actor_type='tier1', text=pacs_message())
        self.assertEqual(again['duplicate_count'], 1)
        self.assertEqual(len(self.credits), 2)

    def test_compose_uetr_is_uuid(self):
        generated = compose_uetr()
        self.assertEqual(normalize_uetr(generated), generated)
        self.assertNotEqual(generated, compose_uetr())
