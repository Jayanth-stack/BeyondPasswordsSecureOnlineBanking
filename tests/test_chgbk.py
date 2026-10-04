import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.chgbk import (
    AmountError,
    ChgbkError,
    ChgbkPolicy,
    ChgbkService,
    MemoryChgbkStore,
    SqliteChgbkStore,
    compose_arn,
    compose_chgbk_record,
    compose_packet_id,
    compose_vcr_file,
    evidence_fingerprint,
    luhn_ok,
    mask_arn,
    message_from_values,
    normalize_arn,
    normalize_bin,
    normalize_ica,
    normalize_network,
    normalize_reason,
    parse_vcr_file,
    reason_code_for,
    window_days_for,
)
from utility.wire import parse_money

UTC = timezone.utc


def ts(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


class FoundationTests(unittest.TestCase):
    def test_arn_luhn_and_mask(self):
        arn = compose_arn('400000', '2024-06-14', 1)
        self.assertEqual(len(arn), 23)
        self.assertTrue(luhn_ok(arn))
        self.assertEqual(normalize_arn(arn), arn)
        self.assertTrue(arn.startswith('400000240614'))
        self.assertEqual(mask_arn(arn), arn[:6] + '*********' + arn[-8:])
        self.assertNotIn(arn[6:15], mask_arn(arn))
        with self.assertRaises(ChgbkError) as ctx:
            normalize_arn('4000002406140000000001')
        self.assertEqual(ctx.exception.code, 'invalid_arn')
        with self.assertRaises(ChgbkError):
            normalize_arn('00000000000000000000000')

    def test_reason_codes_and_windows(self):
        self.assertEqual(normalize_network('MC'), 'mastercard')
        self.assertEqual(normalize_reason('13.1'), 'not_received')
        self.assertEqual(reason_code_for('visa', 'not_received'), '13.1')
        self.assertEqual(reason_code_for('mastercard', 'fraud'), '4837')
        self.assertEqual(window_days_for('visa'), 30)
        self.assertEqual(window_days_for('mastercard'), 45)
        self.assertEqual(normalize_ica(''), '400000')
        self.assertEqual(normalize_ica('400000'), '400000')
        self.assertEqual(normalize_bin('411111'), '411111')
        with self.assertRaises(ChgbkError) as ctx:
            normalize_network('upi')
        self.assertEqual(ctx.exception.code, 'invalid_network')

    def test_vcr_parse_compose_rejects_xml(self):
        message = message_from_values({
            'network': 'visa',
            'reason': '13.1',
            'amount': '25.00',
            'ica': '400000',
            'card_bin': '411111',
            'card_last4': '1111',
            'merchant_account': '1001',
            'cardholder': 'Jane Cardholder',
            'merchant': 'Acme Store',
            'chargeback_date': '20240614',
            'sequence': 1,
        })
        line = compose_chgbk_record(message)
        self.assertTrue(line.startswith('CHGBK|VISA|'))
        parsed = parse_vcr_file(compose_vcr_file([message]))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].arn, message.arn)
        self.assertEqual(parsed[0].reason, 'not_received')
        self.assertNotIn('merchant_account', parsed[0].to_dict())
        self.assertEqual(parsed[0].to_dict()['account_last4'], '1001')
        with self.assertRaises(ChgbkError) as ctx:
            parse_vcr_file('<?xml version="1.0"?><chgbk/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(ChgbkError) as ctx:
            parse_vcr_file('<!DOCTYPE html>')
        self.assertEqual(ctx.exception.code, 'invalid_file')

    def test_evidence_fingerprint_hides_ref(self):
        first = evidence_fingerprint('receipt', 'INV-99', 'shipped')
        again = evidence_fingerprint('receipt', 'INV-99', 'shipped')
        other = evidence_fingerprint('receipt', 'INV-100', 'shipped')
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertNotIn('INV-99', first)
        packet = compose_packet_id('1' * 23, [])
        self.assertTrue(packet.startswith('PKT'))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14)]
        self.debits = []
        self.credits = []

        def debit_fn(account, amount, remark):
            self.debits.append((str(account), str(amount), remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((str(account), str(amount), remark))
            return 'Amount Credited'

        self.service = ChgbkService(
            ChgbkPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryChgbkStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=lambda userid: {
                'checkin': {'Account': 1001, 'Balance': 500},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': {'Account': 2001, 'Balance': 0},
            },
            directory_fn=lambda account: 'alice' if str(account) in {'1001', '1002'} else None,
        )

    def _values(self, **overrides):
        payload = {
            'network': 'visa',
            'reason': '13.1',
            'amount': '25.00',
            'ica': '400000',
            'card_bin': '411111',
            'card_last4': '1111',
            'merchant_account': '1001',
            'cardholder': 'Jane Cardholder',
            'merchant': 'Acme Store',
            'chargeback_date': '2024-06-14',
            'sequence': 1,
        }
        payload.update(overrides)
        return payload

    def test_ingest_posts_and_is_idempotent(self):
        row, created = self.service.ingest(actor='teller', actor_type='tier1', values=self._values())
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.userid, 'alice')
        self.assertEqual(len(self.debits), 1)
        self.assertEqual(self.debits[0][0], '1001')
        self.assertTrue(self.debits[0][2].startswith('chgbk '))
        body = row.to_dict()
        self.assertNotIn('merchant_account', body)
        self.assertEqual(body['account_last4'], '1001')
        self.assertEqual(body['card_last4'], '1111')
        again, created_again = self.service.ingest(actor='teller', actor_type='tier1', values=self._values())
        self.assertFalse(created_again)
        self.assertEqual(again.case_id, row.case_id)
        self.assertEqual(len(self.debits), 1)

    def test_wrong_receiver_and_unmatched_and_credit(self):
        with self.assertRaises(ChgbkError) as ctx:
            self.service.ingest(actor='teller', actor_type='tier1', values=self._values(ica='411111', arn=compose_arn('411111', '2024-06-14', 9)))
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        unmatched, _created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._values(merchant_account='9999', sequence=2),
        )
        self.assertEqual(unmatched.status, 'unmatched')
        self.assertEqual(self.debits, [])
        credit, _created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._values(merchant_account='2001', customer_id='alice', sequence=3),
        )
        self.assertEqual(credit.status, 'unmatched')
        self.assertEqual(credit.reason_note, 'credit_not_allowed')

    def test_ofac_hold_override_and_dual_control(self):
        held, _created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._values(cardholder='Blocked Person', sequence=4),
        )
        self.assertEqual(held.status, 'held')
        self.assertEqual(self.debits, [])
        posted = self.service.override_ofac(case_id=held.case_id, actor='teller', actor_type='tier1')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)

        pending, _created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._values(amount='10000.00', sequence=5),
        )
        self.assertEqual(pending.status, 'pending_release')
        with self.assertRaises(ChgbkError) as ctx:
            self.service.release(case_id=pending.case_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(case_id=pending.case_id, actor='approver', actor_type='tier2')
        self.assertEqual(released.status, 'posted')

    def test_evidence_represent_win_and_window(self):
        row, _created = self.service.ingest(actor='teller', actor_type='tier1', values=self._values(sequence=6))
        with self.assertRaises(ChgbkError) as ctx:
            self.service.represent(case_id=row.case_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'missing_evidence')
        evidenced = self.service.add_evidence(
            case_id=row.case_id, actor='alice', actor_type='customer',
            kind='receipt', ref='INV-99', note='delivered',
        )
        self.assertEqual(evidenced.status, 'evidence')
        self.assertEqual(evidenced.evidence[0].kind, 'receipt')
        self.assertNotIn('INV-99', evidenced.to_dict()['evidence'][0]['fingerprint'])
        represented = self.service.represent(case_id=row.case_id, actor='alice', actor_type='customer')
        self.assertEqual(represented.status, 'represented')
        self.assertTrue(represented.packet_id.startswith('PKT'))
        won = self.service.record_win(case_id=row.case_id, actor='teller', actor_type='tier1')
        self.assertEqual(won.status, 'won')
        self.assertEqual(len(self.credits), 1)
        self.assertTrue(self.credits[0][2].startswith('chgbk win '))

        late, _created = self.service.ingest(actor='teller', actor_type='tier1', values=self._values(sequence=7))
        self.service.add_evidence(
            case_id=late.case_id, actor='alice', actor_type='customer',
            kind='delivery', ref='TRK-1',
        )
        self.now[0] = ts(2024, 7, 20)
        with self.assertRaises(ChgbkError) as ctx:
            self.service.represent(case_id=late.case_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'represent_window_closed')
        expired = self.service.run_due('alice')
        self.assertEqual(expired[0].status, 'expired')

    def test_assign_accept_and_sqlite_roundtrip(self):
        unmatched, _created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._values(merchant_account='8888', sequence=8),
        )
        self.assertEqual(unmatched.status, 'unmatched')
        assigned = self.service.assign(
            case_id=unmatched.case_id, actor='teller', actor_type='tier1',
            customer_id='alice', internal_account='1001',
        )
        self.assertEqual(assigned.status, 'posted')
        accepted = self.service.accept_liability(
            case_id=assigned.case_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(accepted.status, 'lost')
        self.assertEqual(self.credits, [])

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteChgbkStore(path)
            service = ChgbkService(
                ChgbkPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}, 'savings': 'None', 'credit': 'None'},
                directory_fn=lambda account: 'alice' if str(account) == '1001' else None,
            )
            row, created = service.ingest(actor='teller', actor_type='tier1', values=self._values(sequence=9))
            self.assertTrue(created)
            again = SqliteChgbkStore(path).get_case_by_arn(row.arn)
            self.assertEqual(again.userid, 'alice')
            self.assertEqual(again.status, 'posted')
            self.assertNotIn('merchant_account', again.to_dict())
        finally:
            os.unlink(path)

    def test_customer_cannot_ingest(self):
        with self.assertRaises(ChgbkError) as ctx:
            self.service.ingest(actor='alice', actor_type='customer', values=self._values())
        self.assertEqual(ctx.exception.code, 'chgbk_forbidden')
        self.assertEqual(parse_money('25'), Decimal('25.00'))
        with self.assertRaises(AmountError):
            parse_money('nope')
