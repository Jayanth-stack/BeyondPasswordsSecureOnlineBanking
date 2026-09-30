import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.insepa import (
    EurUsdBook,
    InSepaError,
    InSepaPolicy,
    InSepaService,
    MemoryInSepaStore,
    SqliteInSepaStore,
    Target2Calendar,
    compose_iban,
    compose_pacs004,
    compose_pacs008,
    easter_gregorian,
    extract_iban_account,
    iban_check_digit_ok,
    mask_iban,
    message_from_pacs,
    normalize_bic,
    normalize_iban,
    split_pacs_file,
    target2_holidays,
)

CEST = timezone(timedelta(hours=2))
OUR_BIC = 'KNHADEFFXXX'
SENDER_BIC = 'COBADEFFXXX'
SCHEME_A = 'INST20240615KH0001'
SCHEME_B = 'INST20240615KH0002'
SCHEME_C = 'INST20240615KH0003'
SCT_A = 'SCT20240614KH0001'
BENEFICIARY_IBAN = compose_iban('DE', '370400440000001001')
ORIGINATOR_IBAN = 'DE89370400440532013000'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


def pacs_message(**overrides):
    fields = {
        'scheme': 'sct_inst',
        'scheme_id': SCHEME_A,
        'end_to_end_id': 'E2EINST0001',
        'msg_id': 'MSGINST0001',
        'amount': '1250.00',
        'sender_bic': SENDER_BIC,
        'receiver_bic': OUR_BIC,
        'iban': BENEFICIARY_IBAN,
        'originator_iban': ORIGINATOR_IBAN,
        'originator_name': 'ACME GMBH',
        'beneficiary_name': 'ADA LOVELACE',
        'memo': 'invoice 42',
    }
    fields.update(overrides)
    return compose_pacs008(fields)


class FoundationTests(unittest.TestCase):
    def test_iban_mod97_official_and_composed(self):
        self.assertTrue(iban_check_digit_ok(ORIGINATOR_IBAN))
        self.assertTrue(iban_check_digit_ok(BENEFICIARY_IBAN))
        self.assertEqual(len(BENEFICIARY_IBAN), 22)
        self.assertEqual(extract_iban_account(BENEFICIARY_IBAN), '1001')
        self.assertEqual(mask_iban(BENEFICIARY_IBAN), 'DE****1001')
        self.assertEqual(normalize_iban('de89 3704 0044 0532 0130 00'), ORIGINATOR_IBAN)
        with self.assertRaises(InSepaError) as ctx:
            normalize_iban('US64SVBKUS6S3300969597')
        self.assertEqual(ctx.exception.code, 'not_sepa_country')
        self.assertFalse(iban_check_digit_ok('DE00370400440532013000'))

    def test_bic_iso9362(self):
        self.assertEqual(normalize_bic('KNHADEFF'), OUR_BIC)
        self.assertEqual(normalize_bic('coba-de-ff-xxx'), SENDER_BIC)
        with self.assertRaises(InSepaError) as ctx:
            normalize_bic('KNHAAU2SXXX')
        self.assertEqual(ctx.exception.code, 'invalid_bic')
        with self.assertRaises(InSepaError):
            normalize_bic('KNHADE0FXXX')

    def test_target2_cutoff_and_holidays(self):
        cal = Target2Calendar(cutoff_hour=16, tz_offset_hours=2)
        friday_morning = ts(2024, 6, 14, 15, 0)
        friday_evening = ts(2024, 6, 14, 17, 0)
        saturday = ts(2024, 6, 15, 15, 0)
        self.assertEqual(cal.value_date(friday_morning, scheme='sct').isoformat(), '2024-06-14')
        self.assertFalse(cal.should_queue(friday_morning, scheme='sct'))
        self.assertEqual(cal.value_date(friday_evening, scheme='sct').isoformat(), '2024-06-17')
        self.assertTrue(cal.should_queue(friday_evening, scheme='sct'))
        self.assertTrue(cal.should_queue(saturday, scheme='sct'))
        self.assertFalse(cal.should_queue(saturday, scheme='sct_inst'))
        self.assertEqual(cal.value_date(saturday, scheme='sct_inst').isoformat(), '2024-06-15')
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        self.assertIn(datetime(2024, 3, 29).date(), target2_holidays(2024))
        self.assertIn(datetime(2024, 5, 1).date(), target2_holidays(2024))

    def test_fx_quote(self):
        quote = EurUsdBook(Decimal('1.080000')).quote(Decimal('1250.00'))
        self.assertEqual(quote.amount_eur, '1250.00')
        self.assertEqual(quote.amount_usd, '1350.00')
        self.assertEqual(quote.rate, '1.080000')

    def test_pacs008_roundtrip_and_xxe_rejected(self):
        raw = pacs_message()
        message = message_from_pacs(raw)
        self.assertEqual(message['scheme'], 'sct_inst')
        self.assertEqual(message['scheme_id'], SCHEME_A)
        self.assertEqual(message['amount_eur'], '1250.00')
        self.assertEqual(message['sender_bic'], SENDER_BIC)
        self.assertEqual(message['receiver_bic'], OUR_BIC)
        self.assertEqual(message['beneficiary_account'], '1001')
        self.assertEqual(message['iban'], BENEFICIARY_IBAN)
        with self.assertRaises(InSepaError) as ctx:
            message_from_pacs('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Document>&xxe;</Document>')
        self.assertEqual(ctx.exception.code, 'invalid_pacs')

    def test_split_file_and_pacs004(self):
        first = pacs_message()
        second = pacs_message(scheme_id=SCHEME_B, amount='10.00', end_to_end_id='E2EINST0002', msg_id='MSGINST0002')
        parts = split_pacs_file(first + '\n' + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual(message_from_pacs(parts[1])['amount_eur'], '10.00')

        store = MemoryInSepaStore()
        service = InSepaService(
            InSepaPolicy(receiver_bic=OUR_BIC),
            store,
            clock=lambda: ts(2024, 6, 15, 15, 0),
            lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
            accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            credit_fn=lambda account, amount, remark: 'Success',
        )
        row, _created = service.ingest(actor='teller', actor_type='tier1', values={'file': first})
        returned = service.return_inbound(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1', reason='AC03',
        )
        raw = compose_pacs004(
            returned,
            return_msg_id=returned.return_msg_id,
            reason=returned.return_reason,
            receiver_bic=OUR_BIC,
        )
        self.assertIn(returned.return_msg_id, raw)
        self.assertIn('AC03', raw)
        self.assertIn(SCHEME_A, raw)


class InSepaServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 15, 0)]
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

        self.service = InSepaService(
            InSepaPolicy(
                receiver_bic=OUR_BIC,
                dual_control_threshold=Decimal('10000.00'),
                inst_max=Decimal('100000.00'),
                sct_max=Decimal('20000000.00'),
                fx_rate=Decimal('1.080000'),
            ),
            MemoryInSepaStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=2),
            fx_book=EurUsdBook(Decimal('1.080000')),
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': pacs_message(**overrides)},
        )

    def test_happy_path_posts_usd_credit_and_masks_iban(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(row.amount_eur, '1250.00')
        self.assertEqual(row.amount_usd, '1350.00')
        self.assertEqual(len(self.credits), 1)
        self.assertEqual(self.credits[0], ('1001', '1350.00', 'sct_inst from ACME GMBH'))
        payload = row.to_dict()
        self.assertEqual(payload['beneficiary_last4'], '1001')
        self.assertEqual(payload['iban_masked'], 'DE****1001')
        self.assertEqual(payload['sender_bic'], SENDER_BIC)
        self.assertNotIn('beneficiary_account', payload)
        self.assertNotIn('account_number', payload)
        self.assertNotIn('iban', payload)

    def test_weekend_instant_still_posts(self):
        self.assertEqual(datetime.fromtimestamp(self.now[0], tz=CEST).weekday(), 5)
        row, _created = self._ingest()
        self.assertEqual(row.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_sct_after_cutoff_queues_then_run_due_posts(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        row, created = self._ingest(
            scheme='sct', scheme_id=SCT_A, end_to_end_id='SCTE2E1', msg_id='SCTMSG1',
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(self.credits[0][2], 'sct from ACME GMBH')

    def test_duplicate_scheme_id_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InSepaError) as ctx:
            self._ingest(receiver_bic=SENDER_BIC)
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        self.assertEqual(self.credits, [])

    def test_unmatched_then_assign_posts(self):
        other = compose_iban('DE', '370400440000009999')
        row, _created = self._ingest(iban=other, scheme_id=SCHEME_B, end_to_end_id='E2EMISS', msg_id='MSGMISS')
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
        card_iban = compose_iban('DE', '370400440000001003')
        row, _created = self._ingest(iban=card_iban, scheme_id=SCHEME_B, end_to_end_id='E2ECC', msg_id='MSGCC')
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
        row, _created = self._ingest(amount='10000.00')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(row.amount_usd, '10800.00')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InSepaError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_instant_cap(self):
        with self.assertRaises(InSepaError) as ctx:
            self._ingest(amount='100000.01')
        self.assertEqual(ctx.exception.code, 'sct_inst_amount_exceeded')

    def test_sct_allows_instant_overflow(self):
        self.now[0] = ts(2024, 6, 14, 11, 0)
        row, created = self._ingest(
            scheme='sct', amount='150000.00', scheme_id=SCT_A, end_to_end_id='SCTBIG', msg_id='SCTMSGBIG',
        )
        self.assertTrue(created)
        self.assertEqual(row.scheme, 'sct')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InSepaError) as ctx:
            self._ingest(
                scheme='sct', amount='20000000.01', scheme_id='SCT20240614KH0002',
                end_to_end_id='SCTCAP', msg_id='SCTMSGCAP',
            )
        self.assertEqual(ctx.exception.code, 'sct_amount_exceeded')

    def test_customer_return_instant_window_and_sct_same_day(self):
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
        self.assertIn('sct_inst return', self.debits[0][2])

        later, _ = self._ingest(scheme_id=SCHEME_C, end_to_end_id='E2ELATER', msg_id='MSGLATER')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InSepaError) as ctx:
            self.service.request_return(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='teller', actor_type='tier1', reason='AC03',
        )
        self.assertEqual(staff.status, 'returned')
        self.assertEqual(staff.return_reason, 'AC03')

    def test_sct_customer_return_next_day_closed(self):
        self.now[0] = ts(2024, 6, 14, 11, 0)
        row, _ = self._ingest(scheme='sct', scheme_id=SCT_A, end_to_end_id='SCTE2E1', msg_id='SCTMSG1')
        self.assertEqual(row.status, 'posted')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        with self.assertRaises(InSepaError) as ctx:
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
        with self.assertRaises(InSepaError) as ctx:
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
        self.assertEqual(snap['ytd_posted'], '1350.00')
        self.assertEqual(snap['posted_count'], 1)
        self.assertTrue(snap['clock']['instant'])
        self.assertEqual(snap['inbounds'][0]['beneficiary_last4'], '1001')
        self.assertNotIn('beneficiary_account', snap['inbounds'][0])

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInSepaStore(path)
            service = InSepaService(
                InSepaPolicy(receiver_bic=OUR_BIC),
                store,
                clock=lambda: ts(2024, 6, 15, 15, 0),
                credit_fn=lambda account, amount, remark: 'Success',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': pacs_message()},
            )
            reloaded = SqliteInSepaStore(path).get_by_scheme_id(row.scheme_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
            self.assertEqual(reloaded.amount_usd, '1350.00')
            self.assertEqual(reloaded.iban_masked, 'DE****1001')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InSepaError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': pacs_message()},
            )
        self.assertEqual(ctx.exception.code, 'insepa_forbidden')


if __name__ == '__main__':
    unittest.main()
