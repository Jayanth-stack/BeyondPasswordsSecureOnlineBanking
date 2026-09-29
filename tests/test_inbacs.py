import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inbacs import (
    BankOfEnglandCalendar,
    GbpUsdBook,
    InBacsError,
    InBacsPolicy,
    InBacsService,
    MemoryInBacsStore,
    SqliteInBacsStore,
    compose_arucs,
    compose_gb_iban,
    compose_std18,
    compose_uhl1,
    easter_gregorian,
    extract_iban_destination,
    iban_check_digit_ok,
    mask_iban,
    message_from_std18,
    normalize_sort_code,
    normalize_txn_code,
    split_std18_file,
    uk_bank_holidays,
    vocalink_modulus_ok,
)

BST = timezone(timedelta(hours=1))
OUR_SORT = '200000'
SENDER_SORT = '089999'
SERIAL_A = 'BC20240614SUN0001'
SERIAL_B = 'BC20240614SUN0002'
SERIAL_C = 'BC20240614SUN0003'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BST).timestamp()


def std18_record(**overrides):
    fields = {
        'serial': SERIAL_A,
        'amount': '1250.00',
        'sender_sort': SENDER_SORT,
        'receiver_sort': OUR_SORT,
        'beneficiary_account': '1001',
        'originator_account': '66374958',
        'beneficiary_name': 'ADA LOVELACE',
        'originator_name': 'ACME CORP',
        'txn_code': '99',
        'sun': '123456',
    }
    fields.update(overrides)
    return compose_std18(fields)


class FoundationTests(unittest.TestCase):
    def test_vocalink_official_and_computed(self):
        self.assertTrue(vocalink_modulus_ok('089999', '66374958'))
        self.assertTrue(vocalink_modulus_ok('107999', '88837491'))
        self.assertTrue(vocalink_modulus_ok('200000', '12345679'))
        self.assertFalse(vocalink_modulus_ok('200000', '12345678'))
        self.assertEqual(normalize_sort_code('20-00-00'), '200000')
        self.assertEqual(normalize_txn_code('salary'), '93')
        with self.assertRaises(InBacsError) as ctx:
            normalize_txn_code('17')
        self.assertEqual(ctx.exception.code, 'invalid_txn_code')

    def test_gb_iban_mod97(self):
        iban = compose_gb_iban('NWBK', '200000', '00001001')
        self.assertTrue(iban.startswith('GB'))
        self.assertEqual(len(iban), 22)
        self.assertTrue(iban_check_digit_ok(iban))
        self.assertEqual(extract_iban_destination(iban), ('200000', '00001001'))
        self.assertEqual(mask_iban(iban), 'GB****1001')
        self.assertFalse(iban_check_digit_ok('GB00NWBK00000000000000'))

    def test_boe_calendar_tplus2_and_cutoff(self):
        cal = BankOfEnglandCalendar(cutoff_hour=16, tz_offset_hours=1)
        friday_morning = ts(2024, 6, 14, 11, 0)
        friday_evening = ts(2024, 6, 14, 17, 0)
        saturday = ts(2024, 6, 15, 11, 0)
        tuesday = ts(2024, 6, 18, 10, 0)
        self.assertEqual(cal.input_date(friday_morning).isoformat(), '2024-06-14')
        self.assertEqual(cal.value_date(friday_morning).isoformat(), '2024-06-18')
        self.assertTrue(cal.should_queue(friday_morning))
        self.assertEqual(cal.input_date(friday_evening).isoformat(), '2024-06-17')
        self.assertEqual(cal.value_date(friday_evening).isoformat(), '2024-06-19')
        self.assertEqual(cal.input_date(saturday).isoformat(), '2024-06-17')
        self.assertFalse(cal.should_queue(tuesday, value_date=cal.value_date(friday_morning)))
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        self.assertIn(datetime(2024, 3, 29).date(), uk_bank_holidays(2024))

    def test_fx_quote(self):
        quote = GbpUsdBook(Decimal('1.2500')).quote(Decimal('1250.00'))
        self.assertEqual(quote.amount_gbp, '1250.00')
        self.assertEqual(quote.amount_usd, '1562.50')

    def test_std18_roundtrip(self):
        raw = std18_record()
        self.assertEqual(len(raw), 100)
        message = message_from_std18(raw)
        self.assertEqual(message['serial'], SERIAL_A)
        self.assertEqual(message['amount_gbp'], '1250.00')
        self.assertEqual(message['sender_sort'], SENDER_SORT)
        self.assertEqual(message['txn_code'], '99')
        self.assertEqual(message['beneficiary_account'], '1001')

    def test_split_file_skips_headers_and_contra(self):
        header = compose_uhl1(processing=datetime(2024, 6, 17).date(), sun='123456')
        first = std18_record()
        second = std18_record(serial=SERIAL_B, amount='10.00')
        contra = compose_std18({
            'serial': 'CONTRA001234',
            'amount': '1260.00',
            'sender_sort': OUR_SORT,
            'receiver_sort': OUR_SORT,
            'beneficiary_account': '1001',
            'originator_account': '00001001',
            'originator_name': 'CONTRA',
            'beneficiary_name': 'CONTRA',
        })
        vol = 'VOL1' + (' ' * 76)
        blob = '\n'.join([vol, header, first, contra, second, 'UTL1' + (' ' * 76)])
        parts, processing, sun = split_std18_file(blob)
        self.assertEqual(len(parts), 2)
        self.assertEqual(processing.isoformat(), '2024-06-17')
        self.assertEqual(sun, '123456')
        self.assertEqual(message_from_std18(parts[1])['amount_gbp'], '10.00')

    def test_arucs_return_carries_serial_and_reason(self):
        store = MemoryInBacsStore()
        service = InBacsService(
            InBacsPolicy(receiver_sort=OUR_SORT),
            store,
            clock=lambda: ts(2024, 6, 14, 11, 0),
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            credit_fn=lambda account, amount, remark: 'Success',
        )
        row, _created = service.ingest(
            actor='teller', actor_type='tier1', values={'file': std18_record()},
        )
        returned = service.return_inbound(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1', reason='5',
        )
        raw = compose_arucs(
            returned,
            return_msg_id=returned.return_msg_id,
            reason=returned.return_reason,
            receiver_sort=OUR_SORT,
        )
        self.assertEqual(len(raw), 100)
        self.assertIn(returned.return_msg_id[:8], raw)
        self.assertIn('5', raw[33:37])


class InBacsServiceTests(unittest.TestCase):
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

        self.service = InBacsService(
            InBacsPolicy(
                receiver_sort=OUR_SORT,
                dual_control_threshold=Decimal('10000.00'),
                max_amount=Decimal('250000.00'),
                fx_rate=Decimal('1.2500'),
            ),
            MemoryInBacsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=BankOfEnglandCalendar(cutoff_hour=16, tz_offset_hours=1),
            fx_book=GbpUsdBook(Decimal('1.2500')),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': std18_record(**overrides)},
        )

    def test_happy_path_queues_then_posts_usd_credit_and_masks_account(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'queued')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(row.amount_gbp, '1250.00')
        self.assertEqual(row.amount_usd, '1562.50')
        self.assertEqual(row.value_date, '2024-06-18')
        self.assertEqual(self.credits, [])
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)

        self.now[0] = ts(2024, 6, 18, 10, 0)
        posted = self.service.run_due()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(self.credits, [('1001', '1562.50', 'bacs from ACME CORP')])

    def test_duplicate_serial_is_idempotent(self):
        first, created = self._ingest()
        self.assertTrue(created)
        second, created_again = self._ingest()
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.service.store.list_all()), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InBacsError) as ctx:
            self._ingest(receiver_sort='404000')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')

    def test_unmatched_assign_then_queue(self):
        row, _ = self._ingest(beneficiary_account='9999', serial=SERIAL_B)
        self.assertEqual(row.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=row.inbound_id,
            actor='teller',
            actor_type='tier1',
            customer_id='alice',
            internal_account='1001',
        )
        self.assertEqual(assigned.userid, 'alice')
        self.assertEqual(assigned.status, 'queued')

    def test_credit_account_unmatched(self):
        self.directory['1003'] = 'alice'
        row, _ = self._ingest(beneficiary_account='1003', serial=SERIAL_C)
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(row.note, 'credit_not_allowed')
        self.assertEqual(self.credits, [])

    def test_ofac_hold_then_override_queues(self):
        row, _ = self._ingest(originator_name='BLOCKED PERSON', serial=SERIAL_B)
        self.assertEqual(row.status, 'held')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InBacsError) as ctx:
            self.service.override_ofac(inbound_id=row.inbound_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'inbacs_forbidden')
        released = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(released.status, 'queued')

    def test_dual_control_same_approver_403(self):
        row, _ = self._ingest(amount='9000.00', serial=SERIAL_B)
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(row.amount_usd, '11250.00')
        with self.assertRaises(InBacsError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(
            inbound_id=row.inbound_id, actor='approver', actor_type='tier2',
        )
        self.assertEqual(released.status, 'queued')

    def test_bacs_cap(self):
        with self.assertRaises(InBacsError) as ctx:
            self._ingest(amount='250000.01', serial=SERIAL_B)
        self.assertEqual(ctx.exception.code, 'bacs_amount_exceeded')

    def test_customer_return_before_and_after_post(self):
        row, _ = self._ingest()
        returned = self.service.request_return(
            inbound_id=row.inbound_id, actor='alice', actor_type='customer', reason='cust',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits, [])

        posted_row, _ = self._ingest(serial=SERIAL_B)
        self.now[0] = ts(2024, 6, 18, 10, 0)
        self.service.run_due()
        posted = self.service.store.get(posted_row.inbound_id)
        self.assertEqual(posted.status, 'posted')
        recalled = self.service.request_return(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'returned')
        self.assertEqual(self.debits[-1][2], 'bacs return %s' % SERIAL_B[:12])

        self.now[0] = ts(2024, 6, 14, 11, 0)
        late_row, _ = self._ingest(serial=SERIAL_C)
        self.now[0] = ts(2024, 6, 18, 10, 5)
        self.service.run_due()
        self.assertEqual(self.service.store.get(late_row.inbound_id).status, 'posted')
        self.now[0] = ts(2024, 6, 21, 10, 0)
        with self.assertRaises(InBacsError) as ctx:
            self.service.request_return(
                inbound_id=late_row.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

    def test_nsf_on_return_after_post(self):
        row, _ = self._ingest()
        self.now[0] = ts(2024, 6, 18, 10, 0)
        self.service.run_due()

        def nsf_debit(account, amount, remark):
            return 'Insufficient funds'

        self.service.debit_fn = nsf_debit
        with self.assertRaises(InBacsError) as ctx:
            self.service.return_inbound(
                inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')

    def test_sqlite_reopen(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInBacsStore(path)
            service = InBacsService(
                InBacsPolicy(receiver_sort=OUR_SORT),
                store,
                clock=lambda: ts(2024, 6, 14, 11, 0),
                lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': std18_record()},
            )
            reopened = SqliteInBacsStore(path)
            loaded = reopened.get(row.inbound_id)
            self.assertEqual(loaded.serial, SERIAL_A)
            self.assertEqual(loaded.status, 'queued')
            self.assertIsNone(reopened.get_by_serial('missing-serial-xx'))
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InBacsError) as ctx:
            self.service.ingest(actor='alice', actor_type='customer', values={'file': std18_record()})
        self.assertEqual(ctx.exception.code, 'inbacs_forbidden')


if __name__ == '__main__':
    unittest.main()
