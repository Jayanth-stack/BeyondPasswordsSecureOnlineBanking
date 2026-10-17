import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.gpi import (
    GpiError,
    GpiPolicy,
    GpiService,
    InstantClock,
    MemoryGpiStore,
    SqliteGpiStore,
    Target2Calendar,
    compose_gpi,
    compose_mt199_reply,
    compose_trn,
    compose_uetr,
    compose_message_id,
    easter_gregorian,
    mask_bic,
    mask_trn,
    mask_uetr,
    normalize_bic,
    normalize_message_id,
    normalize_trn,
    normalize_uetr,
    parse_gpi,
    parse_mt199,
    split_gpi_file,
    target2_holidays,
)
from utility.wire import AmountError, parse_money, screen_name

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


SAMPLE_UETR = 'A1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
SAMPLE_TRN = 'GPI20240614000001'
SAMPLE_BIC = 'DEUTDEFFXXX'
HOME_BIC = 'KNHAUS33XXX'


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_bic(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(normalize_bic('deutdeff'), SAMPLE_BIC)
        self.assertEqual(mask_bic(HOME_BIC), 'KNHA****XXX')
        with self.assertRaises(GpiError) as ctx:
            normalize_bic('DEUT0EFFXXX')
        self.assertEqual(ctx.exception.code, 'invalid_bic')
        with self.assertRaises(GpiError):
            normalize_bic('not-a-bic')

    def test_uetr_and_trn(self):
        self.assertEqual(compose_trn('20240614', 1), SAMPLE_TRN)
        self.assertEqual(normalize_trn(SAMPLE_TRN), SAMPLE_TRN)
        self.assertEqual(mask_trn(SAMPLE_TRN), 'GPI202*****0001')
        self.assertEqual(normalize_uetr(SAMPLE_UETR), SAMPLE_UETR)
        self.assertEqual(mask_uetr(SAMPLE_UETR), 'A1B2C3D4***B5C6')
        generated = compose_uetr()
        self.assertEqual(normalize_uetr(generated), generated)
        with self.assertRaises(GpiError) as ctx:
            normalize_uetr('not-a-uuid')
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        with self.assertRaises(GpiError):
            normalize_trn('INV20240614000001')

    def test_message_ids(self):
        self.assertEqual(compose_message_id('gpi', '20240614', 2), 'TRCK20240614000002')
        self.assertEqual(compose_message_id('mt199', '20240614', 3), 'INQY20240614000003')
        self.assertEqual(compose_message_id('mt199r', '20240614', 4), 'N19920240614000004')
        self.assertEqual(normalize_message_id('trck-20240614-000002'), 'TRCK20240614000002')
        with self.assertRaises(GpiError):
            normalize_message_id('TXST20240614000001')

    def test_gpi_roundtrip_and_xml_reject(self):
        record = compose_gpi(
            message_id='TRCK20240614000001', uetr=SAMPLE_UETR, trn=SAMPLE_TRN,
            status='ACSC', amount=Decimal('25.00'), rail='gpi', party_name='Ada Lovelace',
            party_bic=SAMPLE_BIC, cycle_date='20240614',
        )
        parsed = parse_gpi(record)
        self.assertEqual(parsed['network_status'], 'ACSC')
        self.assertEqual(parsed['amount'], Decimal('25.00'))
        self.assertEqual(parsed['uetr'], SAMPLE_UETR)
        with self.assertRaises(GpiError) as ctx:
            parse_gpi('<?xml version="1.0"?><pacs:Pacs002></pacs:Pacs002>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(GpiError):
            split_gpi_file('<!DOCTYPE foo>', header='GPI1')

    def test_mt199_parse_and_reply_masks(self):
        record = '|'.join([
            'MT199', 'INQY20240614000001', SAMPLE_UETR, SAMPLE_TRN, '40.00', 'USD',
            'gpi', 'FROZ', 'Blocked Person', SAMPLE_BIC, HOME_BIC,
        ])
        parsed = parse_mt199(record)
        self.assertEqual(parsed['reason'], 'FROZ')
        self.assertEqual(parsed['kind'], 'mt199')

        class Row:
            resolution_id = 'N19920240614000009'
            uetr = SAMPLE_UETR
            trn = SAMPLE_TRN
            reason = 'FROZ'
            rail = 'gpi'
            receiver_bic = HOME_BIC
            message_id = 'INQY20240614000001'

        composed = compose_mt199_reply(Row(), resolution='CNFM')
        self.assertTrue(composed.startswith('MT199R|'))
        self.assertIn(mask_uetr(SAMPLE_UETR), composed)
        self.assertNotIn(SAMPLE_UETR, composed)

    def test_instant_clock_24_7_and_target2(self):
        clock = InstantClock(tz_offset_hours=-4)
        friday_after = ts(2024, 6, 14, 18, 0)
        snap = clock.snapshot(friday_after)
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertFalse(snap['after_cutoff'])
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertTrue(clock.is_business_day(datetime(2024, 7, 4).date()))
        self.assertEqual(clock.cycle_date(july4), '20240704')
        self.assertEqual(easter_gregorian(2024), datetime(2024, 3, 31).date())
        self.assertIn(datetime(2024, 3, 29).date(), target2_holidays(2024))
        calendar = Target2Calendar(cutoff_hour=16, tz_offset_hours=-4)
        self.assertTrue(calendar.is_after_cutoff(ts(2024, 6, 14, 16, 1)))
        self.assertFalse(calendar.is_business_day(datetime(2024, 3, 29).date()))

    def test_ofac_phrase_not_single_token(self):
        hit = screen_name('Mr Blocked Person LLC')
        self.assertTrue(hit.hit)
        self.assertFalse(screen_name('SMITH', watchlist=('SMITH JR',)).hit)


class GpiServiceTests(unittest.TestCase):
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

        policy = GpiPolicy(dual_control_threshold=Decimal('10000.00'))
        self.service = GpiService(
            policy,
            MemoryGpiStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=Target2Calendar(cutoff_hour=16, tz_offset_hours=-4),
        )

    def _enroll(self, **kwargs):
        return self.service.add_enrollment(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Payroll'),
            direction=kwargs.pop('direction', 'outbound'),
            rail=kwargs.pop('rail', 'gpi'),
            amount=kwargs.pop('amount', '25.00'),
            account=kwargs.pop('account', '1001'),
            counterparty_name=kwargs.pop('counterparty_name', 'Ada Lovelace'),
            counterparty_bic=kwargs.pop('counterparty_bic', SAMPLE_BIC),
            uetr=kwargs.pop('uetr', SAMPLE_UETR),
            trn=kwargs.pop('trn', SAMPLE_TRN),
            **kwargs,
        )

    def _tracker(self, **overrides):
        body = {
            'message_id': 'TRCK20240614000001',
            'uetr': SAMPLE_UETR,
            'trn': SAMPLE_TRN,
            'network_status': 'ACSC',
            'amount': Decimal('25.00'),
            'currency': 'USD',
            'rail': 'gpi',
            'counterparty_name': 'Ada Lovelace',
            'counterparty_bic': SAMPLE_BIC,
            'receiver_bic': HOME_BIC,
            'cycle_date': '20240614',
            'kind': 'gpi',
            'direction': 'outbound',
        }
        body.update(overrides)
        return body

    def _mt199(self, **overrides):
        body = {
            'message_id': 'INQY20240614000001',
            'uetr': SAMPLE_UETR,
            'trn': SAMPLE_TRN,
            'amount': Decimal('25.00'),
            'currency': 'USD',
            'rail': 'gpi',
            'reason': 'NARR',
            'counterparty_name': 'Ada Lovelace',
            'counterparty_bic': SAMPLE_BIC,
            'receiver_bic': HOME_BIC,
            'kind': 'mt199',
            'direction': 'inbound',
        }
        body.update(overrides)
        return body

    def test_snapshot_never_leaks_uetr_or_account(self):
        enroll = self._enroll()
        payload = enroll.to_dict()
        self.assertNotIn('uetr', payload)
        self.assertNotIn('trn', payload)
        self.assertNotIn('account', payload)
        self.assertEqual(payload['account_last4'], '1001')
        self.assertEqual(payload['uetr_masked'], mask_uetr(SAMPLE_UETR))
        case, created = self.service.ingest_gpi(
            actor='teller', actor_type='tier1', message=self._tracker(),
        )
        self.assertTrue(created)
        dumped = case.to_dict()
        self.assertNotIn('uetr', dumped)
        self.assertNotIn('account', dumped)
        self.assertEqual(dumped['status'], 'settled')
        snap = self.service.snapshot('alice')
        self.assertNotIn(SAMPLE_UETR, str(snap))
        self.assertEqual(snap['cases'][0]['status'], 'settled')

    def test_credit_destination_rejected_on_enroll(self):
        with self.assertRaises(GpiError) as ctx:
            self._enroll(account='1003', nickname='Card')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        case, created = self.service.ingest_gpi(
            actor='teller', actor_type='tier1', message=self._tracker(),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'unmatched')
        with self.assertRaises(GpiError) as ctx:
            self.service.assign_case(
                case_id=case.case_id, actor='teller', actor_type='tier1',
                owner_userid='alice', account='1003', nickname='Card',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_duplicate_tracker_is_idempotent(self):
        self._enroll()
        first, created = self.service.ingest_gpi(
            actor='teller', actor_type='tier1', message=self._tracker(),
        )
        self.assertTrue(created)
        second, created = self.service.ingest_gpi(
            actor='teller', actor_type='tier1', message=self._tracker(),
        )
        self.assertFalse(created)
        self.assertEqual(first.case_id, second.case_id)

    def test_wrong_receiver_rejected(self):
        self._enroll()
        with self.assertRaises(GpiError) as ctx:
            self.service.ingest_gpi(
                actor='teller', actor_type='tier1',
                message=self._tracker(receiver_bic='CHASUS33XXX'),
            )
        self.assertEqual(ctx.exception.code, 'wrong_receiver')

    def test_unmatched_then_assign_and_ofac_hold(self):
        case, created = self.service.ingest_gpi(
            actor='teller', actor_type='tier1',
            message=self._tracker(counterparty_name='Blocked Person'),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'unmatched')
        assigned = self.service.assign_case(
            case_id=case.case_id, actor='teller', actor_type='tier1',
            owner_userid='alice', account='1001', nickname='Held pay',
        )
        self.assertEqual(assigned.status, 'held')
        with self.assertRaises(GpiError) as ctx:
            self.service.override_ofac(
                case_id=case.case_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'gpi_forbidden')
        cleared = self.service.override_ofac(
            case_id=case.case_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(cleared.status, 'settled')
        self.assertTrue(cleared.ofac_overridden)

    def test_customer_query_credits_outbound(self):
        self._enroll()
        case, _ = self.service.ingest_gpi(
            actor='teller', actor_type='tier1', message=self._tracker(),
        )
        queried = self.service.request_query(
            case_id=case.case_id, actor='alice', actor_type='customer', reason='NARR',
        )
        self.assertEqual(queried.status, 'query_requested')
        self.assertTrue(queried.message_id.startswith('INQY'))
        accepted = self.service.accept_query(
            case_id=case.case_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(accepted.status, 'resolved_accepted')
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '25.00')
        self.assertTrue(self.credits[0][2].startswith('gpi query '))
        self.assertTrue(accepted.resolution_id.startswith('N199'))

    def test_inbound_query_debits_and_window(self):
        self._enroll(direction='inbound', nickname='In pay')
        self.service.ingest_gpi(actor='teller', actor_type='tier1', message=self._tracker())
        case, created = self.service.ingest_mt199(
            actor='teller', actor_type='tier1', message=self._mt199(),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'query_inbound')
        accepted = self.service.accept_query(
            case_id=case.case_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(accepted.status, 'resolved_accepted')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '25.00')

        later = SAMPLE_UETR[:-1] + '1'
        later_trn = 'GPI20240614000002'
        self._enroll(
            nickname='Late', uetr=later, trn=later_trn, amount='10.00',
        )
        self.service.ingest_gpi(
            actor='teller', actor_type='tier1',
            message=self._tracker(
                message_id='TRCK20240614000009', uetr=later, trn=later_trn,
                amount=Decimal('10.00'),
            ),
        )
        self.now[0] = ts(2024, 6, 16, 12, 0)
        late_case = self.service.store.get_by_uetr(later)
        with self.assertRaises(GpiError) as ctx:
            self.service.request_query(
                case_id=late_case.case_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

    def test_dual_control_same_approver(self):
        self._enroll(amount='15000.00', nickname='Big')
        self.service.ingest_gpi(
            actor='teller', actor_type='tier1',
            message=self._tracker(amount=Decimal('15000.00'), network_status='ACSC'),
        )
        inbound_uetr = 'B1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
        inbound_trn = 'GPI20240614000008'
        self._enroll(
            nickname='Big in', direction='inbound', amount='15000.00',
            uetr=inbound_uetr, trn=inbound_trn,
        )
        case, _ = self.service.ingest_mt199(
            actor='teller', actor_type='tier1',
            message=self._mt199(
                message_id='INQY20240614000008', uetr=inbound_uetr,
                trn=inbound_trn, amount=Decimal('15000.00'),
            ),
        )
        self.assertEqual(case.status, 'pending_release')
        with self.assertRaises(GpiError) as ctx:
            self.service.release_case(case_id=case.case_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_case(
            case_id=case.case_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'query_inbound')

    def test_nsf_on_inbound_accept(self):
        self.service.debit_fn = lambda account, amount, remark=None: 'Insufficient funds'
        self._enroll(direction='inbound', nickname='NSF')
        self.service.ingest_gpi(actor='teller', actor_type='tier1', message=self._tracker())
        case, _ = self.service.ingest_mt199(
            actor='teller', actor_type='tier1', message=self._mt199(),
        )
        with self.assertRaises(GpiError) as ctx:
            self.service.accept_query(case_id=case.case_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'nsf')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteGpiStore(path)
            service = GpiService(
                GpiPolicy(), store, clock=lambda: self.now[0],
                debit_fn=lambda a, n, remark=None: 'Amount Debited',
                credit_fn=lambda a, n, remark=None: 'Success',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            enroll = service.add_enrollment(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='SQ', direction='outbound', rail='mt', amount='12.00',
                account='1001', counterparty_name='Ada Lovelace',
                counterparty_bic=SAMPLE_BIC, uetr=SAMPLE_UETR, trn=SAMPLE_TRN,
            )
            reloaded = SqliteGpiStore(path).get_enrollment(enroll.enrollment_id)
            self.assertEqual(reloaded.nickname, 'SQ')
            self.assertEqual(reloaded.uetr, SAMPLE_UETR)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
