import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.insdd import (
    EurUsdBook,
    InSddError,
    InSddPolicy,
    InSddService,
    MemoryInSddStore,
    SqliteInSddStore,
    Target2Calendar,
    compose_creditor_identifier,
    compose_iban,
    compose_pacs002,
    compose_pacs004,
    compose_pain008,
    easter_gregorian,
    extract_iban_account,
    iban_check_digit_ok,
    lead_days_for,
    mask_iban,
    message_from_pain,
    normalize_iban,
    parse_pain008,
    split_pain_file,
)

CEST = timezone(timedelta(hours=2))
OUR_BIC = 'KNHADEFFXXX'
CI = compose_creditor_identifier('DE', '09999999999')
DEBTOR_IBAN = compose_iban('DE', '370400440000001001')
UMR = 'UMR-ALICE-1'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CEST).timestamp()


def pain_file(**overrides):
    fields = {
        'scheme_id': 'E2ETEST0000001',
        'amount': '1250.00',
        'scheme': 'core',
        'sequence': 'FRST',
        'receiver_bic': OUR_BIC,
        'debtor_iban': DEBTOR_IBAN,
        'creditor_id': CI,
        'umr': UMR,
        'collection_date': '2024-06-14',
        'creditor_name': 'ACME CORP',
        'debtor_name': 'ADA LOVELACE',
        'memo': 'invoice 42',
    }
    fields.update(overrides)
    return compose_pain008(fields)


class FoundationTests(unittest.TestCase):
    def test_iban_official_and_composed_account(self):
        self.assertTrue(iban_check_digit_ok('DE89370400440532013000'))
        self.assertEqual(normalize_iban('DE89 3704 0044 0532 0130 00'), 'DE89370400440532013000')
        self.assertEqual(extract_iban_account(DEBTOR_IBAN), '1001')
        self.assertEqual(mask_iban(DEBTOR_IBAN), 'DE****1001')
        self.assertEqual(CI, 'DE98ZZZ09999999999')

    def test_pain008_roundtrip_and_file_split(self):
        raw = pain_file()
        parsed = message_from_pain(raw)
        self.assertEqual(parsed['scheme_id'], 'E2ETEST0000001')
        self.assertEqual(parsed['amount'], '1250.00')
        self.assertEqual(parsed['debtor_iban'], DEBTOR_IBAN)
        self.assertEqual(parsed['receiver_bic'], OUR_BIC)
        self.assertEqual(parsed['scheme'], 'core')
        self.assertEqual(parsed['sequence'], 'FRST')
        self.assertEqual(parsed['umr'], UMR)
        self.assertEqual(parsed['creditor_id'], CI)
        second = pain_file(scheme_id='E2ETEST0000002', amount='10.00', sequence='OOFF')
        parts = split_pain_file(raw + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual({item['scheme_id'] for item in parts}, {'E2ETEST0000001', 'E2ETEST0000002'})

    def test_xxe_and_non_sepa_rejected(self):
        with self.assertRaises(InSddError) as ctx:
            split_pain_file('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><a>&xxe;</a>')
        self.assertEqual(ctx.exception.code, 'invalid_pain')
        with self.assertRaises(InSddError) as iban_ctx:
            normalize_iban('GB82WEST12345698765432')
        self.assertEqual(iban_ctx.exception.code, 'not_sepa_country')

    def test_target2_easter_and_lead_days(self):
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        calendar = Target2Calendar()
        self.assertFalse(calendar.is_business_day(datetime(2024, 3, 29).date()))
        self.assertFalse(calendar.is_business_day(datetime(2024, 5, 1).date()))
        self.assertTrue(calendar.is_business_day(datetime(2024, 6, 14).date()))
        self.assertEqual(lead_days_for('core', 'FRST'), 5)
        self.assertEqual(lead_days_for('core', 'RCUR'), 2)
        self.assertEqual(lead_days_for('b2b', 'FRST'), 1)
        self.assertEqual(EurUsdBook().quote(Decimal('1250.00'))['debit_usd'], '1350.00')

    def test_return_messages(self):
        class _Row:
            scheme_id = 'E2ETEST0000001'
            amount_eur = '1250.00'
            return_id = 'RTR1'

        reject = compose_pacs002(_Row(), reason='nsf', receiver_bic=OUR_BIC)
        self.assertIn('AM04', reject)
        refund = compose_pacs004(_Row(), reason='refund', receiver_bic=OUR_BIC)
        self.assertIn('MD06', refund)


class InSddServiceTests(unittest.TestCase):
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

        self.service = InSddService(
            InSddPolicy(
                receiver_bic=OUR_BIC,
                dual_control_threshold=Decimal('10000.00'),
            ),
            MemoryInSddStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lambda account: self.directory.get(str(account)),
            calendar=Target2Calendar(cutoff_hour=16, cutoff_minute=0, tz_offset_hours=2),
            fx=EurUsdBook(Decimal('1.080000')),
        )
        self.mandate = self.service.add_mandate(
            actor='alice',
            actor_type='customer',
            userid='alice',
            values={
                'umr': UMR,
                'creditor_id': CI,
                'creditor_name': 'ACME CORP',
                'scheme': 'core',
                'account': '1001',
            },
        )

    def _ingest(self, **overrides):
        return self.service.ingest(
            actor='teller',
            actor_type='tier1',
            values={'file': pain_file(**overrides)},
        )

    def test_core_happy_path_debits_usd_and_masks_iban(self):
        row, created = self._ingest()
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(row.scheme, 'core')
        self.assertEqual(len(self.debits), 1)
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '1350.00')
        self.assertIn('sdd to', self.debits[0][2])
        payload = row.to_dict()
        self.assertEqual(payload['iban_masked'], 'DE****1001')
        self.assertEqual(payload['debit_usd'], '1350.00')
        self.assertNotIn('debtor_iban', payload)
        self.assertNotIn('beneficiary_account', payload)

    def test_queues_until_collection_date(self):
        row, _created = self._ingest(scheme_id='E2EQUEUED00001', collection_date='2024-06-17')
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.debits, [])
        self.service.run_due('alice')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_duplicate_scheme_id_is_idempotent(self):
        first, created = self._ingest()
        second, again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.debits), 1)

    def test_wrong_receiver_rejected(self):
        with self.assertRaises(InSddError) as ctx:
            self._ingest(receiver_bic='COBADEFFXXX')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        self.assertEqual(self.debits, [])

    def test_unmatched_without_mandate_then_register_posts(self):
        self.service.cancel_mandate(
            mandate_id=self.mandate.mandate_id, actor='alice', actor_type='customer',
        )
        row, _created = self._ingest(scheme_id='E2ENOMAND00001', umr='UMR-NEW-1')
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(row.note, 'mandate_missing')
        self.assertEqual(self.debits, [])
        self.service.add_mandate(
            actor='alice',
            actor_type='customer',
            userid='alice',
            values={
                'umr': 'UMR-NEW-1',
                'creditor_id': CI,
                'creditor_name': 'ACME CORP',
                'scheme': 'core',
                'account': '1001',
            },
        )
        reloaded = self.service.get_inbound(
            inbound_id=row.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(reloaded.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_credit_account_stays_unmatched(self):
        self.directory['1003'] = 'alice'
        other_iban = compose_iban('DE', '370400440000001003')
        row, _created = self._ingest(scheme_id='E2ECC000000001', umr='UMR-CC', debtor_iban=other_iban)
        self.assertEqual(row.status, 'unmatched')
        self.assertIn(row.note, {'credit_not_allowed', 'mandate_missing'})
        self.assertEqual(self.debits, [])
        with self.assertRaises(InSddError) as ctx:
            self.service.add_mandate(
                actor='alice', actor_type='customer', userid='alice',
                values={'umr': 'UMR-CC', 'creditor_id': CI, 'scheme': 'core', 'account': '1003', 'creditor_name': 'ACME'},
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_does_not_debit(self):
        row, _created = self._ingest(scheme_id='E2EOFAC0000001', creditor_name='BLOCKED PERSON')
        self.assertEqual(row.status, 'held')
        self.assertTrue(row.ofac_hit)
        self.assertEqual(self.debits, [])
        posted = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_dual_control_requires_other_employee(self):
        row, _created = self._ingest(scheme_id='E2EDUAL0000001', amount='10000.00')
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(InSddError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_after_cutoff_queues_until_run_due(self):
        self.now[0] = ts(2024, 6, 14, 17, 0)
        row, _created = self._ingest(scheme_id='E2ECUTOFF00001')
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.debits, [])
        self.service.run_due('alice')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_customer_refuse_before_post_and_core_refund(self):
        queued, _created = self._ingest(scheme_id='E2EREFUSE00001', collection_date='2024-06-17')
        returned = self.service.request_return(
            inbound_id=queued.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertTrue(returned.return_id)
        self.assertEqual(self.credits, [])

        posted, _ = self._ingest(scheme_id='E2EREFUND00001')
        self.assertEqual(posted.status, 'posted')
        refunded = self.service.request_refund(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer', reason='md06',
        )
        self.assertEqual(refunded.status, 'returned')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('sdd refund', self.credits[0][2])

        later, _ = self._ingest(scheme_id='E2ELATE0000001', sequence='RCUR')
        self.now[0] = ts(2024, 8, 20, 10, 0)
        with self.assertRaises(InSddError) as ctx:
            self.service.request_refund(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='teller', actor_type='tier1', reason='acct',
        )
        self.assertEqual(staff.status, 'returned')

    def test_b2b_has_no_customer_refund(self):
        self.service.add_mandate(
            actor='alice', actor_type='customer', userid='alice',
            values={'umr': 'UMR-B2B-1', 'creditor_id': CI, 'scheme': 'b2b', 'account': '1001', 'creditor_name': 'ACME'},
        )
        row, _ = self._ingest(scheme_id='E2EB2B00000001', scheme='b2b', umr='UMR-B2B-1', sequence='FRST')
        self.assertEqual(row.status, 'posted')
        with self.assertRaises(InSddError) as ctx:
            self.service.request_refund(
                inbound_id=row.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'scheme_no_refund')

    def test_post_nsf_fails_collection(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        with self.assertRaises(InSddError) as ctx:
            self._ingest(scheme_id='E2ENSF000000001')
        self.assertEqual(ctx.exception.code, 'nsf')
        row = self.service.store.get_by_scheme_id('E2ENSF000000001')
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.return_reason, 'AM04')

    def test_snapshot_and_sqlite_reopen(self):
        self._ingest()
        snap = self.service.snapshot('alice', actor='alice', actor_type='customer')
        self.assertEqual(snap['ytd_posted'], '1350.00')
        self.assertEqual(snap['posted_count'], 1)
        self.assertEqual(snap['inbounds'][0]['iban_masked'], 'DE****1001')
        self.assertNotIn('debtor_iban', snap['inbounds'][0])
        self.assertGreaterEqual(snap['mandate_count'], 1)

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInSddStore(path)
            service = InSddService(
                InSddPolicy(receiver_bic=OUR_BIC),
                store,
                clock=lambda: ts(2024, 6, 14, 11, 0),
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                lookup_fn=lambda account: 'alice',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001}},
                calendar=Target2Calendar(cutoff_hour=16, cutoff_minute=0, tz_offset_hours=2),
            )
            service.add_mandate(
                actor='alice', actor_type='customer', userid='alice',
                values={'umr': UMR, 'creditor_id': CI, 'scheme': 'core', 'account': '1001', 'creditor_name': 'ACME'},
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1', values={'file': pain_file()},
            )
            reloaded = SqliteInSddStore(path).get_by_scheme_id(row.scheme_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, 'posted')
            self.assertEqual(reloaded.userid, 'alice')
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(InSddError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer', values={'file': pain_file()},
            )
        self.assertEqual(ctx.exception.code, 'insdd_forbidden')

    def test_file_ingest_counts(self):
        batch = self.service.ingest_file(
            actor='teller',
            actor_type='tier1',
            text=pain_file() + pain_file(scheme_id='E2EFILE0000002', amount='10.00', sequence='RCUR', umr='UMR-ALICE-1'),
        )
        # second OOFF after FRST last_sequence is empty on first ingest of file;
        # first FRST posts and advances mandate, second OOFF is allowed and posts.
        self.assertEqual(batch['accepted_count'], 2)
        self.assertEqual(batch['error_count'], 0)
        again = self.service.ingest_file(actor='teller', actor_type='tier1', text=pain_file())
        self.assertEqual(again['duplicate_count'], 1)
        self.assertEqual(len(self.debits), 2)

    def test_core_cap(self):
        with self.assertRaises(InSddError) as ctx:
            self._ingest(scheme_id='E2ECAP000000001', amount='15000.01')
        self.assertEqual(ctx.exception.code, 'core_amount_exceeded')

    def test_frst_then_rcur_sequence(self):
        first, _ = self._ingest()
        self.assertEqual(first.status, 'posted')
        second, _ = self._ingest(scheme_id='E2ERCUR0000001', sequence='RCUR')
        self.assertEqual(second.status, 'posted')
        self.assertEqual(len(self.debits), 2)
        third, _ = self._ingest(scheme_id='E2EBADSEQ00001', sequence='FRST')
        self.assertEqual(third.status, 'unmatched')
        self.assertEqual(third.note, 'invalid_sequence')
