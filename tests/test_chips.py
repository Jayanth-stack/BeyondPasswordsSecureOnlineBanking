import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.chips import (
    ChipsCalendar,
    ChipsError,
    ChipsPolicy,
    ChipsService,
    MemoryChipsStore,
    SqliteChipsStore,
    compose_chips_seq,
    compose_chip1,
    compose_uid,
    mask_chips_seq,
    mask_uid,
    normalize_participant,
    parse_chip1,
    participant_from_aba,
    split_chip1_file,
)
from utility.wire import AmountError, parse_money, us_fed_holidays

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_participant_pads_and_rejects_zero(self):
        self.assertEqual(normalize_participant('1'), '0001')
        self.assertEqual(normalize_participant('0002'), '0002')
        with self.assertRaises(ChipsError) as ctx:
            normalize_participant('0000')
        self.assertEqual(ctx.exception.code, 'invalid_participant')
        with self.assertRaises(ChipsError):
            normalize_participant('12345')

    def test_aba_maps_to_chips_participant_not_routing(self):
        self.assertEqual(participant_from_aba('021000021'), '0001')
        self.assertEqual(participant_from_aba('021-000-021'), '0001')
        with self.assertRaises(ChipsError) as ctx:
            participant_from_aba('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_aba')
        with self.assertRaises(ChipsError) as ctx:
            participant_from_aba('011401533')
        self.assertIn(ctx.exception.code, {'unknown_participant', 'invalid_aba'})

    def test_uid_folds_account_and_masks(self):
        uid = compose_uid('77881234')
        self.assertEqual(len(uid), 6)
        self.assertNotEqual(uid, '77881234')
        self.assertTrue(uid.isdigit())
        self.assertEqual(mask_uid(uid), '***' + uid[-3:])
        self.assertNotIn(uid, mask_uid(uid))

    def test_chips_seq_layout_is_not_imad(self):
        seq = compose_chips_seq('20240614', '0001', 1)
        self.assertEqual(seq, '202406140001000001')
        self.assertEqual(mask_chips_seq(seq), '20240614***0001')
        self.assertNotIn('KONOHA01', seq)
        with self.assertRaises(ChipsError) as ctx:
            compose_chips_seq('2024-06-14', '0001', 1)
        self.assertEqual(ctx.exception.code, 'invalid_sequence')

    def test_chip1_roundtrip_rejects_xml(self):
        record = compose_chip1(
            sequence='202406140001000001',
            value_date='20240614',
            send_pid='0001',
            recv_pid='0002',
            uid='123456',
            amount=Decimal('150.00'),
            name='Ada Lovelace',
            purpose='goods',
            mask=False,
        )
        parsed = parse_chip1(record)
        self.assertEqual(parsed['recv_pid'], '0002')
        self.assertEqual(parsed['amount'], '150.00')
        self.assertEqual(parsed['settlement_method'], 'chips_net')
        with self.assertRaises(ChipsError) as ctx:
            parse_chip1('<?xml version="1.0"?><CHIP1/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(ChipsError):
            split_chip1_file('<!DOCTYPE html>\nCHIP1|1|x')
        masked = compose_chip1(
            sequence='202406140001000001',
            value_date='20240614',
            send_pid='0001',
            recv_pid='0002',
            uid='123456',
            amount=Decimal('150.00'),
            name='Ada',
            mask=True,
        )
        self.assertIn('***456', masked)
        with self.assertRaises(ChipsError):
            parse_chip1(masked)

    def test_session_open_and_cutoff_and_july4(self):
        calendar = ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4)
        before = ts(2024, 6, 14, 8, 0)
        open_ts = ts(2024, 6, 14, 10, 0)
        after = ts(2024, 6, 14, 17, 30)
        self.assertFalse(calendar.is_session_open(before))
        self.assertTrue(calendar.snapshot(before)['before_open'])
        self.assertEqual(calendar.value_date(before).isoformat(), '2024-06-14')
        self.assertTrue(calendar.is_session_open(open_ts))
        self.assertEqual(calendar.value_date(after).isoformat(), '2024-06-17')
        self.assertTrue(calendar.snapshot(after)['after_cutoff'])
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(july4).isoformat(), '2024-07-05')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday).isoformat(), '2024-06-17')

    def test_parse_money_still_shared(self):
        with self.assertRaises(AmountError):
            parse_money('nope')


class ChipsServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 10, 0)]
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

        policy = ChipsPolicy(
            outbound_fee=Decimal('8.00'),
            dual_control_threshold=Decimal('10000.00'),
            net_debit_cap=Decimal('5000000.00'),
            min_amount=Decimal('100.00'),
        )
        self.service = ChipsService(
            policy,
            MemoryChipsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4),
        )

    def _add(self, nickname='Citi Clearing', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            participant=kwargs.pop('participant', '0002'),
            uid=kwargs.pop('uid', '123456'),
            account_number=kwargs.pop('account_number', '77881234'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_never_leaks_uid_or_account(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('uid', payload)
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['uid_masked'], '***456')
        self.assertEqual(payload['participant'], '0002')
        snap = self.service.snapshot('alice')
        self.assertNotIn('uid', snap['beneficiaries'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='c1',
        )
        self.assertTrue(created)
        body = payment.to_dict()
        self.assertNotIn('uid', body)
        self.assertNotIn('chips_seq', body)
        self.assertEqual(body['status'], 'released')
        self.assertEqual(body['settlement_method'], 'chips_net')
        self.assertTrue(body['chips_seq_masked'].startswith('20240614***'))
        self.assertEqual(self.debits[0], ('1001', '150.00', 'chips to Citi Clearing'))
        self.assertEqual(self.debits[1][1], '8.00')

    def test_same_day_send_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='140.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='140.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len([row for row in self.debits if row[1] == '140.00']), 1)

    def test_before_open_queues_until_session(self):
        self.now[0] = ts(2024, 6, 14, 8, 0)
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='early',
        )
        self.assertEqual(payment.status, 'queued')
        self.assertEqual(payment.value_date, '20240614')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 14, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'released')
        self.assertEqual(self.debits[0], ('1001', '150.00', 'chips to Citi Clearing'))

    def test_after_cutoff_queues_next_business_day(self):
        self.now[0] = ts(2024, 6, 14, 17, 30)
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='late',
        )
        self.assertEqual(payment.status, 'queued')
        self.assertEqual(payment.value_date, '20240617')
        self.assertEqual(self.debits, [])

    def test_end_of_day_net_settles_released(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='set1',
        )
        self.assertEqual(payment.status, 'released')
        self.now[0] = ts(2024, 6, 14, 17, 30)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'settled')
        self.assertTrue(due[0].settlement_id)
        with self.assertRaises(ChipsError) as ctx:
            self.service.recall_payment(payment_id=due[0].payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(ChipsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'released')
        self.assertEqual(self.debits[0][1], '150.00')

    def test_dual_control_requires_a_different_employee(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(ChipsError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(
            payment_id=payment.payment_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'released')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_credit_account_and_pause_and_archive(self):
        bene = self._add()
        with self.assertRaises(ChipsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='150.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(ChipsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='150.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')
        closed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(ChipsError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_nsf_does_not_assign_sequence(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(ChipsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].chips_seq, '')

    def test_net_debit_cap_blocks_second_release(self):
        self.service.policy.net_debit_cap = Decimal('200.00')
        bene = self._add()
        first, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='cap1',
        )
        self.assertEqual(first.status, 'released')
        with self.assertRaises(ChipsError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='cap2',
            )
        self.assertEqual(ctx.exception.code, 'net_debit_cap')

    def test_recall_released_credits_settled_is_final(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='160.00', trace_id='r1',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '160.00')

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='170.00', trace_id='r2',
        )
        settled = self.service.settle_payment(
            payment_id=other.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(settled.status, 'settled')
        with self.assertRaises(ChipsError) as ctx:
            self.service.recall_payment(payment_id=other.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_customer_can_cancel_queued_not_released(self):
        self.now[0] = ts(2024, 6, 14, 8, 0)
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='q1',
        )
        cancelled = self.service.cancel_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = ts(2024, 6, 14, 11, 0)
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'released')
        with self.assertRaises(ChipsError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_export_chip1_masks_uid(self):
        bene = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='150.00', trace_id='ex1',
        )
        exported = self.service.export_chip1(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('CHIP1|1|', exported['chip1'])
        self.assertIn('***456', exported['chip1'])
        self.assertNotIn('123456', exported['chip1'])
        with self.assertRaises(ChipsError) as ctx:
            self.service.export_chip1(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'chips_forbidden')

    def test_beneficiary_from_aba_mapping(self):
        bene = self.service.add_beneficiary(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Chase CHIPS', legal_name='Ada Lovelace',
            participant='', uid='654321', default_account='1001',
            aba='021000021',
        )
        self.assertEqual(bene.participant, '0001')
        self.assertEqual(bene.participant_label, 'JPMORGAN CHASE')

    def test_sqlite_roundtrip_masks_uid(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteChipsStore(path)
            service = ChipsService(
                ChipsPolicy(min_amount=Decimal('100.00')),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BofA', legal_name='Ada Lovelace', participant='0103',
                uid='998877', account_number='99887766', default_account='1001',
            )
            reloaded = SqliteChipsStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.uid, '998877')
            self.assertNotIn('uid', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['uid_masked'], '***877')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
