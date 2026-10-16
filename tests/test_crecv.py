import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, money_str, parse_money, us_fed_holidays
from utility.crecv import (
    ChipsCalendar,
    CrecvError,
    CrecvPolicy,
    CrecvService,
    CrecvUid,
    MemoryCrecvStore,
    SqliteCrecvStore,
    compose_chip1,
    compose_chips_seq,
    compose_uid,
    mask_chips_seq,
    mask_uid,
    message_from_chip1,
    normalize_participant,
    normalize_uid,
    parse_chip1,
    participant_from_aba,
    split_chip1_file,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '0001'
SENDER = '0002'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_participant_uid_and_sequence(self):
        self.assertEqual(normalize_participant('1'), '0001')
        self.assertEqual(normalize_participant('0002'), '0002')
        self.assertEqual(participant_from_aba('021000021'), '0001')
        with self.assertRaises(CrecvError) as ctx:
            normalize_participant('0000')
        self.assertEqual(ctx.exception.code, 'invalid_participant')
        with self.assertRaises(CrecvError) as aba:
            participant_from_aba('021000022')
        self.assertEqual(aba.exception.code, 'invalid_aba')
        uid = compose_uid('1001')
        self.assertEqual(len(uid), 6)
        self.assertEqual(normalize_uid('123456'), '123456')
        self.assertEqual(mask_uid('123456'), '***456')
        self.assertEqual(compose_chips_seq('20240614', '0002', 1), '202406140002000001')
        self.assertEqual(mask_chips_seq('202406140002000001'), '20240614***0001')
        with self.assertRaises(CrecvError) as bad:
            compose_chips_seq('2024-06-14', '0002', 1)
        self.assertEqual(bad.exception.code, 'invalid_sequence')

    def test_calendar_open_cutoff_weekend_and_july4(self):
        calendar = ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4)
        friday_open = ts(2024, 6, 14, 11, 0)
        friday_before = ts(2024, 6, 14, 8, 0)
        friday_after = ts(2024, 6, 14, 17, 30)
        self.assertTrue(calendar.is_session_open(friday_open))
        self.assertTrue(calendar.snapshot(friday_before)['before_open'])
        self.assertFalse(calendar.is_session_open(friday_before))
        self.assertTrue(calendar.snapshot(friday_after)['after_cutoff'])
        self.assertEqual(calendar.value_date(friday_after).isoformat(), '2024-06-17')
        july4 = ts(2024, 7, 4, 11, 0)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        self.assertFalse(calendar.is_session_open(july4))
        self.assertEqual(calendar.value_date(july4).isoformat(), '2024-07-05')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertFalse(calendar.is_session_open(saturday))
        extra = ChipsCalendar(extra_holidays=('20240614',))
        self.assertFalse(extra.is_business_day(datetime(2024, 6, 14).date()))

    def test_chip1_roundtrip_rejects_xml(self):
        record = compose_chip1(
            sequence='202406140002000001',
            value_date='20240614',
            send_pid=SENDER,
            recv_pid=RECEIVER,
            uid='123456',
            amount=Decimal('250.00'),
            name='Ada Lovelace',
            purpose='goods',
            mask=False,
        )
        parsed = parse_chip1(record)
        self.assertEqual(parsed['sequence'], '202406140002000001')
        self.assertEqual(parsed['uid'], '123456')
        self.assertEqual(parsed['amount'], Decimal('250.00'))
        self.assertEqual(message_from_chip1(record)['purpose'], 'goods')
        masked = compose_chip1(
            sequence='202406140002000001',
            value_date='20240614',
            send_pid=SENDER,
            recv_pid=RECEIVER,
            uid='123456',
            amount=Decimal('250.00'),
            name='Ada Lovelace',
            mask=True,
        )
        self.assertIn('***456', masked)
        self.assertIn('20240614***0001', masked)
        with self.assertRaises(CrecvError) as ctx:
            parse_chip1(masked)
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(CrecvError) as xml:
            split_chip1_file('<?xml version="1.0"?><!DOCTYPE chip><CHIP1></CHIP1>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(CrecvError):
            parse_chip1('<!ENTITY xxe SYSTEM "file:///etc/passwd">')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class CrecvServiceTests(unittest.TestCase):
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
            if userid != 'alice':
                return {}
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = CrecvPolicy(
            dual_control_threshold=Decimal('10000.00'),
            min_amount=Decimal('100.00'),
            max_amount=Decimal('100000000.00'),
            receiver_pid=RECEIVER,
        )
        self.service = CrecvService(
            policy,
            MemoryCrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=ChipsCalendar(open_hour=9, cutoff_hour=17, tz_offset_hours=-4),
        )

    def _uid(self, **kwargs):
        return self.service.add_uid(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Ops UID'),
            destination_account=kwargs.pop('destination_account', '1001'),
            uid=kwargs.pop('uid', '123456'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'sequence': values.pop('sequence', '202406140002000001'),
            'amount': values.pop('amount', '250.00'),
            'name': values.pop('name', 'Ada Lovelace'),
            'send_pid': values.pop('send_pid', SENDER),
            'recv_pid': values.pop('recv_pid', RECEIVER),
            'uid': values.pop('uid', '123456'),
            'purpose': values.pop('purpose', 'goods'),
            'value_date': values.pop('value_date', '20240614'),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_uid_or_sequence(self):
        enrollment = self._uid()
        payload = enrollment.to_dict()
        self.assertNotIn('uid', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['uid_masked'], '***456')
        self.assertEqual(payload['account_last4'], '1001')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('uid', dumped)
        self.assertNotIn('sequence', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['uid_masked'], '***456')
        self.assertEqual(dumped['sequence_masked'], '20240614***0001')
        snap = self.service.snapshot('alice')
        self.assertNotIn('uid', snap['inbounds'][0])
        self.assertNotIn('sequence', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'released')
        self.assertEqual(self.credits[0], ('1001', '250.00', 'chips from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_sequence(self):
        self._uid()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_uid_does_not_credit(self):
        inbound, _ = self._ingest(uid='654321')
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_uid(CrecvUid(
            uid_id='carduid', userid='alice', nickname='Card', uid='123456',
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(CrecvError) as ctx:
            self.service.add_uid(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card uid', destination_account='1003', uid='111111',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_releases(self):
        self._uid()
        inbound, _ = self._ingest(name='Blocked Person', sequence='202406140002000002')
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(CrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'released')
        self.assertEqual(self.credits[0][1], '250.00')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('200.00')
        self._uid()
        inbound, _ = self._ingest(amount='250.00', sequence='202406140002000003')
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(CrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'released')
        self.assertEqual(len(self.credits), 1)

    def test_before_open_and_after_cutoff_queue_then_run_due(self):
        self._uid()
        self.now[0] = ts(2024, 6, 14, 8, 0)
        queued, _ = self._ingest(sequence='202406140002000004')
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 14, 11, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'released')
        self.assertEqual(len(self.credits), 1)

        self.now[0] = ts(2024, 6, 14, 17, 30)
        late, _ = self._ingest(sequence='202406140002000005')
        self.assertEqual(late.status, 'queued')
        monday = ts(2024, 6, 17, 11, 0)
        self.now[0] = monday
        opened = self.service.run_due('alice')
        statuses = {row.sequence: row.status for row in opened}
        self.assertEqual(statuses['202406140002000005'], 'released')

    def test_net_settlement_after_cutoff_is_irrevocable(self):
        self._uid()
        inbound, _ = self._ingest(sequence='202406140002000006')
        self.assertEqual(inbound.status, 'released')
        with self.assertRaises(CrecvError) as open_session:
            self.service.settle(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(open_session.exception.code, 'not_settleable')
        self.now[0] = ts(2024, 6, 14, 17, 30)
        settled_rows = self.service.run_due('alice')
        self.assertEqual(settled_rows[0].status, 'settled')
        self.assertTrue(settled_rows[0].settlement_id.startswith('NET20240614'))
        with self.assertRaises(CrecvError) as irrev:
            self.service.request_return(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(irrev.exception.code, 'scheme_irrevocable')

    def test_assign_rematch_and_return_while_released(self):
        inbound, _ = self._ingest(uid='654321', sequence='202406140002000007')
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Vendor',
        )
        self.assertEqual(assigned.status, 'released')
        self.assertEqual(assigned.to_dict()['uid_masked'], '***321')
        returned = self.service.request_return(
            inbound_id=assigned.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0], ('1001', '250.00', 'chips return 140002000007'))
        with self.assertRaises(CrecvError) as ctx:
            self.service.request_return(
                inbound_id=assigned.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'already_returned')

    def test_wrong_receiver_cap_pause_resume_sqlite(self):
        self._uid()
        with self.assertRaises(CrecvError) as ctx:
            self._ingest(recv_pid='0103', sequence='202406140103000008')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(CrecvError) as cap:
            self._ingest(amount='99.99', sequence='202406140002000009')
        self.assertEqual(cap.exception.code, 'amount_out_of_range')
        with self.assertRaises(CrecvError) as huge:
            self._ingest(amount='100000000.01', sequence='202406140002000010')
        self.assertEqual(huge.exception.code, 'chips_amount_exceeded')

        token = self.service.store.list_uids('alice')[0]
        paused = self.service.set_uid_status(
            uid_id=token.uid_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        inbound, _ = self._ingest(sequence='202406140002000011')
        self.assertEqual(inbound.status, 'unmatched')
        resumed = self.service.set_uid_status(
            uid_id=token.uid_id, actor='alice', actor_type='customer', status='active',
        )
        self.assertEqual(resumed.status, 'active')
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'released')

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCrecvStore(path)
            service = CrecvService(
                CrecvPolicy(receiver_pid=RECEIVER),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *args: 'Amount Debited',
                credit_fn=lambda *args: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': 'None',
                    'credit': 'None',
                },
            )
            row = service.add_uid(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Persist', destination_account='1001', uid='222222',
            )
            loaded = SqliteCrecvStore(path).get_uid(row.uid_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('uid', loaded.to_dict())
            self.assertEqual(loaded.to_dict()['uid_masked'], '***222')
        finally:
            os.unlink(path)

    def test_export_is_masked_and_customer_cannot_ingest(self):
        self._uid()
        inbound, _ = self._ingest(sequence='202406140002000012')
        exported = self.service.export(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
        )
        self.assertIn('***456', exported['file'])
        self.assertNotIn('123456', exported['file'])
        with self.assertRaises(CrecvError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={'sequence': '202406140002000013', 'amount': '250.00',
                        'name': 'Ada', 'send_pid': SENDER, 'recv_pid': RECEIVER, 'uid': '123456'},
            )
        self.assertEqual(ctx.exception.code, 'crecv_forbidden')


if __name__ == '__main__':
    unittest.main()
