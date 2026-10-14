import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.inv import (
    InvError,
    InvPolicy,
    InvService,
    InstantClock,
    MemoryInvStore,
    SqliteInvStore,
    compose_camt029,
    compose_end_to_end_id,
    compose_message_id,
    compose_pacs002,
    compose_uetr,
    mask_end_to_end_id,
    mask_uetr,
    normalize_end_to_end_id,
    normalize_message_id,
    normalize_participant,
    normalize_uetr,
    parse_camt056,
    parse_pacs002,
    split_inv_file,
)
from utility.wire import AmountError, WireCalendar, parse_money, screen_name

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


SAMPLE_UETR = 'A1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
SAMPLE_E2E = 'INV20240614000001'


class FoundationTests(unittest.TestCase):
    def test_parse_money_and_aba(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(normalize_participant('21000021'), '021000021')
        with self.assertRaises(InvError) as ctx:
            normalize_participant('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_routing')

    def test_uetr_and_end_to_end_id(self):
        self.assertEqual(compose_end_to_end_id('20240614', 1), SAMPLE_E2E)
        self.assertEqual(normalize_end_to_end_id(SAMPLE_E2E), SAMPLE_E2E)
        self.assertEqual(mask_end_to_end_id(SAMPLE_E2E), 'INV202*****0001')
        self.assertEqual(normalize_uetr(SAMPLE_UETR), SAMPLE_UETR)
        self.assertEqual(mask_uetr(SAMPLE_UETR), 'A1B2C3D4***B5C6')
        generated = compose_uetr()
        self.assertEqual(normalize_uetr(generated), generated)
        with self.assertRaises(InvError) as ctx:
            normalize_uetr('not-a-uuid')
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        with self.assertRaises(InvError):
            normalize_end_to_end_id('RFP20240614000001')

    def test_message_ids(self):
        self.assertEqual(compose_message_id('pacs002', '20240614', 2), 'TXST20240614000002')
        self.assertEqual(compose_message_id('camt056', '20240614', 3), 'CXL20240614000003')
        self.assertEqual(compose_message_id('camt029', '20240614', 4), 'RSIN20240614000004')
        self.assertEqual(normalize_message_id('txst-20240614-000002'), 'TXST20240614000002')
        with self.assertRaises(InvError):
            normalize_message_id('PACS20240614000001')

    def test_pacs002_roundtrip_and_xml_reject(self):
        record = compose_pacs002(
            message_id='TXST20240614000001', uetr=SAMPLE_UETR, end_to_end_id=SAMPLE_E2E,
            status='ACSC', amount=Decimal('25.00'), rail='fednow', party_name='Ada Lovelace',
            party_aba='021000021', cycle_date='20240614',
        )
        parsed = parse_pacs002(record)
        self.assertEqual(parsed['network_status'], 'ACSC')
        self.assertEqual(parsed['amount'], Decimal('25.00'))
        self.assertEqual(parsed['uetr'], SAMPLE_UETR)
        with self.assertRaises(InvError) as ctx:
            parse_pacs002('<?xml version="1.0"?><pacs:Pacs002></pacs:Pacs002>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(InvError):
            split_inv_file('<!DOCTYPE foo>', header='PACS002')

    def test_camt056_parse_and_camt029_masks(self):
        record = '|'.join([
            'CAMT056', 'CXL20240614000001', SAMPLE_UETR, SAMPLE_E2E, '40.00', 'USD',
            'rtp', 'FRAD', 'Blocked Person', '021000021', '021000021',
        ])
        parsed = parse_camt056(record)
        self.assertEqual(parsed['reason'], 'FRAD')
        self.assertEqual(parsed['kind'], 'camt056')

        class Row:
            resolution_id = 'RSIN20240614000009'
            uetr = SAMPLE_UETR
            end_to_end_id = SAMPLE_E2E
            reason = 'FRAD'
            rail = 'fednow'
            receiver_routing = '021000021'
            message_id = 'CXL20240614000001'

        composed = compose_camt029(Row(), resolution='ACCR')
        self.assertTrue(composed.startswith('CAMT029|'))
        self.assertIn(mask_uetr(SAMPLE_UETR), composed)
        self.assertNotIn(SAMPLE_UETR, composed)

    def test_instant_clock_24_7_and_july4(self):
        clock = InstantClock(tz_offset_hours=-4)
        friday_after = ts(2024, 6, 14, 18, 0)
        snap = clock.snapshot(friday_after)
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertFalse(snap['after_cutoff'])
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertFalse(clock.is_business_day(datetime(2024, 7, 4).date()))
        self.assertEqual(clock.cycle_date(july4), '20240704')

    def test_ofac_phrase_not_single_token(self):
        hit = screen_name('Mr Blocked Person LLC')
        self.assertTrue(hit.hit)
        self.assertFalse(screen_name('SMITH', watchlist=('SMITH JR',)).hit)


class InvServiceTests(unittest.TestCase):
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

        policy = InvPolicy(dual_control_threshold=Decimal('10000.00'))
        self.service = InvService(
            policy,
            MemoryInvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=WireCalendar(cutoff_hour=17, tz_offset_hours=-4),
        )

    def _enroll(self, **kwargs):
        return self.service.add_enrollment(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Payroll'),
            direction=kwargs.pop('direction', 'outbound'),
            rail=kwargs.pop('rail', 'fednow'),
            amount=kwargs.pop('amount', '25.00'),
            account=kwargs.pop('account', '1001'),
            counterparty_name=kwargs.pop('counterparty_name', 'Ada Lovelace'),
            counterparty_aba=kwargs.pop('counterparty_aba', '021000021'),
            uetr=kwargs.pop('uetr', SAMPLE_UETR),
            end_to_end_id=kwargs.pop('end_to_end_id', SAMPLE_E2E),
            **kwargs,
        )

    def _pacs(self, **overrides):
        body = {
            'message_id': 'TXST20240614000001',
            'uetr': SAMPLE_UETR,
            'end_to_end_id': SAMPLE_E2E,
            'network_status': 'ACSC',
            'amount': Decimal('25.00'),
            'currency': 'USD',
            'rail': 'fednow',
            'counterparty_name': 'Ada Lovelace',
            'counterparty_aba': '021000021',
            'receiver_routing': '021000021',
            'cycle_date': '20240614',
            'kind': 'pacs002',
            'direction': 'outbound',
        }
        body.update(overrides)
        return body

    def _camt(self, **overrides):
        body = {
            'message_id': 'CXL20240614000001',
            'uetr': SAMPLE_UETR,
            'end_to_end_id': SAMPLE_E2E,
            'amount': Decimal('25.00'),
            'currency': 'USD',
            'rail': 'fednow',
            'reason': 'CUST',
            'counterparty_name': 'Ada Lovelace',
            'counterparty_aba': '021000021',
            'receiver_routing': '021000021',
            'kind': 'camt056',
            'direction': 'inbound',
        }
        body.update(overrides)
        return body

    def test_snapshot_never_leaks_uetr_or_account(self):
        enroll = self._enroll()
        payload = enroll.to_dict()
        self.assertNotIn('uetr', payload)
        self.assertNotIn('end_to_end_id', payload)
        self.assertNotIn('account', payload)
        self.assertEqual(payload['account_last4'], '1001')
        self.assertEqual(payload['uetr_masked'], mask_uetr(SAMPLE_UETR))
        case, created = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1', message=self._pacs(),
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
        with self.assertRaises(InvError) as ctx:
            self._enroll(account='1003', nickname='Card')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        case, created = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1', message=self._pacs(),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'unmatched')
        with self.assertRaises(InvError) as ctx:
            self.service.assign_case(
                case_id=case.case_id, actor='teller', actor_type='tier1',
                owner_userid='alice', account='1003', nickname='Card',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_duplicate_pacs002_is_idempotent(self):
        self._enroll()
        first, created = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1', message=self._pacs(),
        )
        self.assertTrue(created)
        second, created = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1', message=self._pacs(),
        )
        self.assertFalse(created)
        self.assertEqual(first.case_id, second.case_id)

    def test_wrong_receiver_rejected(self):
        self._enroll()
        with self.assertRaises(InvError) as ctx:
            self.service.ingest_pacs002(
                actor='teller', actor_type='tier1',
                message=self._pacs(receiver_routing='026009593'),
            )
        self.assertEqual(ctx.exception.code, 'wrong_receiver')

    def test_unmatched_then_assign_and_ofac_hold(self):
        case, created = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1',
            message=self._pacs(counterparty_name='Blocked Person'),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'unmatched')
        assigned = self.service.assign_case(
            case_id=case.case_id, actor='teller', actor_type='tier1',
            owner_userid='alice', account='1001', nickname='Held pay',
        )
        self.assertEqual(assigned.status, 'held')
        with self.assertRaises(InvError) as ctx:
            self.service.override_ofac(
                case_id=case.case_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'inv_forbidden')
        cleared = self.service.override_ofac(
            case_id=case.case_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(cleared.status, 'settled')
        self.assertTrue(cleared.ofac_overridden)

    def test_customer_recall_credits_outbound(self):
        self._enroll()
        case, _ = self.service.ingest_pacs002(
            actor='teller', actor_type='tier1', message=self._pacs(),
        )
        recalled = self.service.request_recall(
            case_id=case.case_id, actor='alice', actor_type='customer', reason='CUST',
        )
        self.assertEqual(recalled.status, 'recall_requested')
        self.assertTrue(recalled.message_id.startswith('CXL'))
        accepted = self.service.accept_recall(
            case_id=case.case_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(accepted.status, 'resolved_accepted')
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '25.00')
        self.assertTrue(self.credits[0][2].startswith('recall '))
        self.assertTrue(accepted.resolution_id.startswith('RSIN'))

    def test_inbound_recall_debits_and_window(self):
        self._enroll(direction='inbound', nickname='In pay')
        self.service.ingest_pacs002(actor='teller', actor_type='tier1', message=self._pacs())
        case, created = self.service.ingest_camt056(
            actor='teller', actor_type='tier1', message=self._camt(),
        )
        self.assertTrue(created)
        self.assertEqual(case.status, 'recall_inbound')
        accepted = self.service.accept_recall(
            case_id=case.case_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(accepted.status, 'resolved_accepted')
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '25.00')

        later = SAMPLE_UETR[:-1] + '1'
        later_e2e = 'INV20240614000002'
        self._enroll(
            nickname='Late', uetr=later, end_to_end_id=later_e2e, amount='10.00',
        )
        self.service.ingest_pacs002(
            actor='teller', actor_type='tier1',
            message=self._pacs(
                message_id='TXST20240614000009', uetr=later, end_to_end_id=later_e2e,
                amount=Decimal('10.00'),
            ),
        )
        self.now[0] = ts(2024, 6, 16, 12, 0)
        late_case = self.service.store.get_by_uetr(later)
        with self.assertRaises(InvError) as ctx:
            self.service.request_recall(
                case_id=late_case.case_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

    def test_dual_control_same_approver(self):
        self._enroll(amount='15000.00', nickname='Big')
        self.service.ingest_pacs002(
            actor='teller', actor_type='tier1',
            message=self._pacs(amount=Decimal('15000.00'), network_status='ACSC'),
        )
        inbound_uetr = 'B1B2C3D4-E5F6-47A8-B9C0-D1E2F3A4B5C6'
        inbound_e2e = 'INV20240614000008'
        self._enroll(
            nickname='Big in', direction='inbound', amount='15000.00',
            uetr=inbound_uetr, end_to_end_id=inbound_e2e,
        )
        case, _ = self.service.ingest_camt056(
            actor='teller', actor_type='tier1',
            message=self._camt(
                message_id='CXL20240614000008', uetr=inbound_uetr,
                end_to_end_id=inbound_e2e, amount=Decimal('15000.00'),
            ),
        )
        self.assertEqual(case.status, 'pending_release')
        with self.assertRaises(InvError) as ctx:
            self.service.release_case(case_id=case.case_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_case(
            case_id=case.case_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'recall_inbound')

    def test_nsf_on_inbound_accept(self):
        self.service.debit_fn = lambda account, amount, remark=None: 'Insufficient funds'
        self._enroll(direction='inbound', nickname='NSF')
        self.service.ingest_pacs002(actor='teller', actor_type='tier1', message=self._pacs())
        case, _ = self.service.ingest_camt056(
            actor='teller', actor_type='tier1', message=self._camt(),
        )
        with self.assertRaises(InvError) as ctx:
            self.service.accept_recall(case_id=case.case_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'nsf')

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInvStore(path)
            service = InvService(
                InvPolicy(), store, clock=lambda: self.now[0],
                debit_fn=lambda a, n, remark=None: 'Amount Debited',
                credit_fn=lambda a, n, remark=None: 'Success',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            enroll = service.add_enrollment(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='SQ', direction='outbound', rail='rtp', amount='12.00',
                account='1001', counterparty_name='Ada Lovelace',
                counterparty_aba='021000021', uetr=SAMPLE_UETR, end_to_end_id=SAMPLE_E2E,
            )
            reloaded = SqliteInvStore(path).get_enrollment(enroll.enrollment_id)
            self.assertEqual(reloaded.nickname, 'SQ')
            self.assertEqual(reloaded.uetr, SAMPLE_UETR)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
