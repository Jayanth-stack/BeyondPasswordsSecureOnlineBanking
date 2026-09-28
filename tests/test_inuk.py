import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inuk import (
    BankOfEnglandCalendar,
    GbpUsdBook,
    InUkError,
    InUkPolicy,
    InUkService,
    MemoryInUkStore,
    SqliteInUkStore,
    compose_gb_iban,
    compose_pacs004,
    compose_pacs008,
    easter_gregorian,
    extract_iban_destination,
    iban_check_digit_ok,
    mask_iban,
    message_from_pacs,
    normalize_scheme,
    normalize_sort_code,
    parse_xml_safe,
    split_pacs_file,
    uk_bank_holidays,
    vocalink_modulus_ok,
)

BST = timezone(timedelta(hours=1))
OUR_SORT = '200000'
SENDER_SORT = '089999'
SCHEME_A = 'FP20240614E2E0001'
SCHEME_B = 'FP20240614E2E0002'
SCHEME_C = 'FP20240614E2E0003'
CHAPS_A = 'CH20240614REF0001'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BST).timestamp()


def pacs_message(**overrides):
    fields = {
        'scheme_id': SCHEME_A,
        'end_to_end_id': 'E2E20240614001',
        'msg_id': 'MSG20240614001',
        'scheme': 'fps',
        'amount': '1250.00',
        'sender_sort': SENDER_SORT,
        'receiver_sort': OUR_SORT,
        'beneficiary_account': '1001',
        'originator_account': '66374958',
        'beneficiary_name': 'ADA LOVELACE',
        'originator_name': 'ACME CORP',
        'memo': 'INVOICE 88',
    }
    fields.update(overrides)
    return compose_pacs008(fields)


class FoundationTests(unittest.TestCase):
    def test_vocalink_official_and_computed(self):
        self.assertTrue(vocalink_modulus_ok('089999', '66374958'))
        self.assertTrue(vocalink_modulus_ok('107999', '88837491'))
        self.assertTrue(vocalink_modulus_ok('200000', '12345679'))
        self.assertFalse(vocalink_modulus_ok('200000', '12345678'))
        self.assertEqual(normalize_sort_code('20-00-00'), '200000')
        self.assertEqual(normalize_scheme('FASTER'), 'fps')
        self.assertEqual(normalize_scheme('CHP'), 'chaps')

    def test_gb_iban_mod97(self):
        iban = compose_gb_iban('NWBK', '200000', '00001001')
        self.assertTrue(iban.startswith('GB'))
        self.assertEqual(len(iban), 22)
        self.assertTrue(iban_check_digit_ok(iban))
        self.assertEqual(extract_iban_destination(iban), ('200000', '00001001'))
        self.assertEqual(mask_iban(iban), 'GB****1001')
        self.assertFalse(iban_check_digit_ok('GB00NWBK00000000000000'))

    def test_boe_calendar_chaps_cutoff_fps_instant(self):
        cal = BankOfEnglandCalendar(cutoff_hour=17, tz_offset_hours=1)
        friday_evening = ts(2024, 6, 14, 18, 0)
        saturday = ts(2024, 6, 15, 11, 0)
        monday = ts(2024, 6, 17, 10, 0)
        self.assertTrue(cal.should_queue(friday_evening, scheme='chaps'))
        self.assertFalse(cal.should_queue(friday_evening, scheme='fps'))
        self.assertTrue(cal.should_queue(saturday, scheme='chaps'))
        self.assertFalse(cal.should_queue(monday, scheme='chaps'))
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        self.assertIn(datetime(2024, 3, 29).date(), uk_bank_holidays(2024))

    def test_fx_quote(self):
        quote = GbpUsdBook(Decimal('1.2500')).quote(Decimal('1250.00'))
        self.assertEqual(quote.amount_gbp, '1250.00')
        self.assertEqual(quote.amount_usd, '1562.50')

    def test_pacs_roundtrip(self):
        raw = pacs_message()
        message = message_from_pacs(raw)
        self.assertEqual(message['scheme_id'], SCHEME_A)
        self.assertEqual(message['amount_gbp'], '1250.00')
        self.assertEqual(message['sender_sort'], SENDER_SORT)
        self.assertEqual(message['scheme'], 'fps')
        self.assertEqual(message['beneficiary_account'], '1001')

    def test_split_file_keeps_each_scheme_id(self):
        first = pacs_message()
        second = pacs_message(scheme_id=SCHEME_B, amount='10.00', end_to_end_id='E2E2', msg_id='MSG2')
        parts = split_pacs_file(first + '\n' + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual(message_from_pacs(parts[1])['amount_gbp'], '10.00')

    def test_xxe_rejected(self):
        with self.assertRaises(InUkError) as ctx:
            parse_xml_safe('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Document>&xxe;</Document>')
        self.assertEqual(ctx.exception.code, 'invalid_pacs')

    def test_return_pacs004_carries_original_scheme_id(self):
        store = MemoryInUkStore()
        service = InUkService(
            InUkPolicy(receiver_sort=OUR_SORT),
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
            receiver_sort=OUR_SORT,
        )
        self.assertIn(SCHEME_A, raw)
        self.assertIn('AC03', raw)
        self.assertIn('PmtRtr', raw)
        self.assertIn('GBP', raw)


class InUkServiceTests(unittest.TestCase):
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

        self.service = InUkService(
            InUkPolicy(
                receiver_sort=OUR_SORT,
                dual_control_threshold=Decimal('10000.00'),
                fps_max=Decimal('1000000.00'),
                chaps_max=Decimal('20000000.00'),
                fx_rate=Decimal('1.2500'),
            ),
            MemoryInUkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=BankOfEnglandCalendar(cutoff_hour=17, tz_offset_hours=1),
            fx_book=GbpUsdBook(Decimal('1.2500')),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': pacs_message(**overrides)},
        )

    def test_happy_path_posts_usd_credit_and_masks_account(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(row.amount_gbp, '1250.00')
        self.assertEqual(row.amount_usd, '1562.50')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '1562.50')
        self.assertIn('fps from', self.credits[0][2])
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertEqual(payload['sender_sort'], '08-99-99')
        self.assertEqual(payload['iban_masked'], '')
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('iban', payload)

    def test_weekend_night_fps_still_posts_instantly(self):
        self.assertEqual(datetime.fromtimestamp(self.now[0], tz=BST).weekday(), 5)
        row, _created = self._ingest()
        self.assertEqual(row.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_chaps_after_cutoff_queues_then_run_due_posts(self):
        self.now[0] = ts(2024, 6, 14, 18, 0)
        row, created = self._ingest(scheme='chaps', scheme_id=CHAPS_A, end_to_end_id='CHE2E1', msg_id='CHMSG1')
        self.assertTrue(created)
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('chaps from', self.credits[0][2])

    def test_duplicate_scheme_id_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InUkError) as ctx:
            self._ingest(receiver_sort=SENDER_SORT)
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
        # £8000 * 1.25 = $10,000.00 exactly, at the dual-control threshold.
        row, _created = self._ingest(amount='8000.00')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(row.amount_usd, '10000.00')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InUkError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_fps_cap(self):
        with self.assertRaises(InUkError) as ctx:
            self._ingest(amount='1000000.01')
        self.assertEqual(ctx.exception.code, 'fps_amount_exceeded')

    def test_chaps_allows_fps_overflow(self):
        self.now[0] = ts(2024, 6, 14, 11, 0)
        row, created = self._ingest(
            scheme='chaps', amount='1500000.00', scheme_id=CHAPS_A, end_to_end_id='CHE2EBIG', msg_id='CHMSGBIG',
        )
        self.assertTrue(created)
        self.assertEqual(row.scheme, 'chaps')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InUkError) as ctx:
            self._ingest(
                scheme='chaps', amount='20000000.01', scheme_id='CH20240614REF0002',
                end_to_end_id='CHE2ECAP', msg_id='CHMSGCAP',
            )
        self.assertEqual(ctx.exception.code, 'chaps_amount_exceeded')

    def test_customer_return_fps_window_and_chaps_same_day(self):
        held, _created = self._ingest(originator_name='OFAC TESTNAME', scheme_id=SCHEME_B)
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
        self.assertIn('fps return', self.debits[0][2])

        later, _ = self._ingest(scheme_id=SCHEME_C, end_to_end_id='E2ELATER', msg_id='MSGLATER')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InUkError) as ctx:
            self.service.request_return(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='teller', actor_type='tier1', reason='AC03',
        )
        self.assertEqual(staff.status, 'returned')
        self.assertEqual(staff.return_reason, 'AC03')

    def test_chaps_customer_return_next_day_closed(self):
        self.now[0] = ts(2024, 6, 14, 11, 0)
        row, _ = self._ingest(scheme='chaps', scheme_id=CHAPS_A, end_to_end_id='CHE2E1', msg_id='CHMSG1')
        self.assertEqual(row.status, 'posted')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InUkError) as ctx:
            self.service.request_return(
                inbound_id=row.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

    def test_return_nsf(self):
        row, _created = self._ingest()

        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(InUkError) as ctx:
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
        self.assertEqual(snap['ytd_posted'], '1562.50')
        self.assertEqual(snap['posted_count'], 1)
        self.assertTrue(snap['clock']['instant'])
        self.assertEqual(snap['inbounds'][0]['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', snap['inbounds'][0])

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInUkStore(path)
            service = InUkService(
                InUkPolicy(receiver_sort=OUR_SORT),
                store,
                clock=lambda: ts(2024, 6, 14, 11, 0),
                credit_fn=lambda account, amount, remark: 'Success',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': pacs_message()},
            )
            reloaded = SqliteInUkStore(path).get_by_scheme_id(row.scheme_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
            self.assertEqual(reloaded.amount_usd, '1562.50')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InUkError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': pacs_message()},
            )
        self.assertEqual(ctx.exception.code, 'inuk_forbidden')

    def test_file_ingest_counts(self):
        batch = self.service.ingest_file(
            actor='teller',
            actor_type='tier1',
            text=pacs_message() + pacs_message(scheme_id=SCHEME_B, end_to_end_id='E2EB', msg_id='MSGB'),
        )
        self.assertEqual(batch['accepted_count'], 2)
        self.assertEqual(batch['error_count'], 0)
        again = self.service.ingest_file(actor='teller', actor_type='tier1', text=pacs_message())
        self.assertEqual(again['duplicate_count'], 1)
        self.assertEqual(len(self.credits), 2)

    def test_iban_beneficiary_maps_to_internal_account(self):
        iban = compose_gb_iban('NWBK', OUR_SORT, '00001001')
        row, created = self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={
                'scheme_id': 'FP20240614IBAN001',
                'end_to_end_id': 'E2EIBAN',
                'msg_id': 'MSGIBAN',
                'scheme': 'fps',
                'amount': '20.00',
                'sender_sort': SENDER_SORT,
                'receiver_sort': OUR_SORT,
                'iban': iban,
                'originator_name': 'PAYROLL LTD',
                'beneficiary_name': 'ADA LOVELACE',
                'originator_account': '66374958',
            },
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.internal_account, '1001')
        self.assertEqual(row.iban_masked, 'GB****1001')
        self.assertEqual(self.credits[0][1], '25.00')
