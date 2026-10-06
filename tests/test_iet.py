import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.iet import (
    CadUsdBook,
    IetError,
    IetPolicy,
    IetService,
    InteracClock,
    MemoryIetStore,
    SqliteIetStore,
    answer_fingerprint,
    answers_match,
    compose_cpa_routing,
    compose_iet,
    compose_reference,
    compute_fee,
    cpa_checksum_ok,
    mask_alias,
    mask_email,
    mask_phone,
    mask_reference,
    normalize_cpa_routing,
    normalize_email,
    normalize_phone,
    parse_iet,
    payments_canada_holidays,
)


ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_cpa_checksum_official_bmo(self):
        self.assertTrue(cpa_checksum_ok('000100016'))
        self.assertEqual(compose_cpa_routing('001', '00016'), '000100016')
        self.assertEqual(normalize_cpa_routing('00100016'), '000100016')
        with self.assertRaises(IetError) as ctx:
            normalize_cpa_routing('000100017')
        self.assertEqual(ctx.exception.code, 'invalid_routing')

    def test_alias_normalize_and_mask(self):
        self.assertEqual(normalize_email('Ada.Lovelace@Example.COM'), 'ada.lovelace@example.com')
        self.assertEqual(mask_email('ada.lovelace@example.com'), 'a***@example.com')
        self.assertEqual(normalize_phone('+1 (416) 555-0199'), '4165550199')
        self.assertEqual(mask_phone('4165550199'), '***-***-0199')
        self.assertEqual(mask_alias('sms', '4165550199'), '***-***-0199')
        with self.assertRaises(IetError) as ctx:
            normalize_phone('2125550199')
        self.assertEqual(ctx.exception.code, 'invalid_alias')
        with self.assertRaises(IetError):
            normalize_email('not-an-email')

    def test_reference_and_iet1_roundtrip(self):
        self.assertEqual(compose_reference('20240614', 7), 'IET202406140007')
        self.assertEqual(mask_reference('IET202406140007'), 'IET202*****0007')
        record = compose_iet(
            reference='IET202406140007',
            amount=Decimal('25.00'),
            sender_name='Ada Lovelace',
            sender_fi='000100016',
            kind='email',
            alias='ada@example.com',
            rail='autodeposit',
        )
        parsed = parse_iet(record)
        self.assertEqual(parsed['reference'], 'IET202406140007')
        self.assertEqual(parsed['alias'], 'ada@example.com')
        self.assertEqual(parsed['currency'], 'CAD')
        with self.assertRaises(IetError) as ctx:
            parse_iet('<?xml version="1.0"?><iet/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')

    def test_answer_fingerprint_never_equals_plaintext(self):
        digest = answer_fingerprint('Blue Maple')
        self.assertNotIn('blue', digest)
        self.assertTrue(answers_match(digest, '  BLUE   maple '))
        self.assertFalse(answers_match(digest, 'red maple'))

    def test_cadusd_quote_and_fees(self):
        book = CadUsdBook(Decimal('0.740000'))
        quote = book.quote(Decimal('100.00'))
        self.assertEqual(quote['amount_usd'], '74.00')
        self.assertEqual(compute_fee(Decimal('100.00'), 'autodeposit', Decimal('1.50'), Decimal('1.00')), Decimal('1.50'))
        self.assertEqual(compute_fee(Decimal('100.00'), 'question', Decimal('1.50'), Decimal('1.00')), Decimal('1.00'))
        self.assertEqual(
            compute_fee(Decimal('100.00'), 'autodeposit', Decimal('1.50'), Decimal('1.00'), waived=True),
            Decimal('0.00'),
        )

    def test_canada_day_is_holiday_but_clock_is_247(self):
        clock = InteracClock(tz_offset_hours=-4)
        canada_day = ts(2024, 7, 1, 10, 0)
        self.assertIn(datetime(2024, 7, 1).date(), payments_canada_holidays(2024))
        snap = clock.snapshot(canada_day)
        self.assertFalse(snap['business_day'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertEqual(snap['value_date'], '2024-07-01')


class IetServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
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

        policy = IetPolicy(
            autodeposit_fee=Decimal('1.50'),
            question_fee=Decimal('1.00'),
            dual_control_threshold=Decimal('10000.00'),
            cadusd=Decimal('0.740000'),
        )
        self.service = IetService(
            policy,
            MemoryIetStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=InteracClock(tz_offset_hours=-4),
            fx=CadUsdBook(Decimal('0.740000')),
        )

    def _add(self, nickname='Pat', name='Pat Singh', **kwargs):
        return self.service.add_contact(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            kind=kwargs.pop('kind', 'email'),
            alias=kwargs.pop('alias', 'pat@example.com'),
            rail=kwargs.pop('rail', 'autodeposit'),
            question=kwargs.pop('question', ''),
            answer=kwargs.pop('answer', ''),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_never_leaks_alias_or_answer(self):
        contact = self._add(
            rail='question', question='Maiden name of first pet?', answer='BlueMaple',
            alias='4165550199', kind='sms',
        )
        payload = contact.to_dict()
        self.assertNotIn('alias_value', payload)
        self.assertNotIn('answer_digest', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['alias_masked'], '***-***-0199')
        self.assertEqual(payload['question'], 'Maiden name of first pet?')
        snap = self.service.snapshot('alice')
        self.assertNotIn('alias_value', snap['contacts'][0])
        transfer, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='100.00', trace_id='t1',
        )
        self.assertTrue(created)
        self.assertEqual(transfer.status, 'pending_claim')
        self.assertNotIn('alias_value', transfer.to_dict())
        self.assertTrue(transfer.reference.startswith('IET20240614'))
        self.assertEqual(self.debits[0], ('1001', '74.00', 'interac to Pat'))
        self.assertEqual(self.debits[1][1], '1.00')

    def test_autodeposit_completes_247_and_is_idempotent(self):
        self.now[0] = ts(2024, 7, 1, 22, 0)
        contact = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='50.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='50.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.transfer_id, again.transfer_id)
        self.assertEqual(first.status, 'completed')
        self.assertEqual(len([row for row in self.debits if row[1] == '37.00']), 1)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        contact = self._add(nickname='Blocked', name='Blocked Person')
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(transfer.status, 'held')
        self.assertTrue(transfer.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(IetError) as ctx:
            self.service.release_transfer(transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '59.20')

    def test_dual_control_requires_a_different_employee(self):
        contact = self._add(
            nickname='Big', alias='big@example.com', rail='question',
            question='Name of first school?', answer='oakridge',
        )
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            contact_id=contact.contact_id, amount='14000.00', trace_id='hv',
        )
        self.assertEqual(transfer.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(IetError) as ctx:
            self.service.release_transfer(transfer_id=transfer.transfer_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_transfer(
            transfer_id=transfer.transfer_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'pending_claim')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10360.00')

    def test_sqa_cancel_credits_and_autodeposit_is_irrevocable_for_customer(self):
        sqa = self._add(
            nickname='Sam', alias='sam@example.com', rail='question',
            question='Name of first school?', answer='oakridge',
        )
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=sqa.contact_id, amount='20.00', trace_id='sqa1',
        )
        self.assertEqual(pending.status, 'pending_claim')
        cancelled = self.service.cancel_transfer(
            transfer_id=pending.transfer_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.assertEqual(self.credits[0][1], '14.80')

        auto = self._add(nickname='Auto', alias='auto@example.com')
        done, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=auto.contact_id, amount='10.00', trace_id='auto1',
        )
        self.assertEqual(done.status, 'completed')
        with self.assertRaises(IetError) as ctx:
            self.service.cancel_transfer(transfer_id=done.transfer_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_autodeposit_staff_recall_window_and_sqa_claim(self):
        contact = self._add()
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='30.00', trace_id='r1',
        )
        recalled = self.service.recall_transfer(
            transfer_id=sent.transfer_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '22.20')

        sqa = self._add(
            nickname='Lee', alias='lee@example.com', rail='question',
            question='Favourite colour here?', answer='green',
        )
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=sqa.contact_id, amount='12.00', trace_id='claim1',
        )
        claimed = self.service.complete_transfer(
            transfer_id=pending.transfer_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(claimed.status, 'completed')
        with self.assertRaises(IetError) as ctx:
            self.service.recall_transfer(transfer_id=claimed.transfer_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_sqa_expiry_credits_back(self):
        contact = self._add(
            rail='question', question='Street you grew up on?', answer='pine',
            alias='pine@example.com', nickname='Pine',
        )
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='15.00', trace_id='exp1',
        )
        self.assertEqual(pending.status, 'pending_claim')
        self.now[0] = ts(2024, 7, 15, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'expired')
        self.assertEqual(self.credits[0][1], '11.10')

    def test_credit_account_pause_archive_and_caps(self):
        contact = self._add()
        with self.assertRaises(IetError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='20.00', internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(IetError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='10000.01',
            )
        self.assertEqual(ctx.exception.code, 'autodeposit_amount_exceeded')
        paused = self.service.set_contact_status(
            contact_id=contact.contact_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(IetError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'contact_paused')
        closed = self.service.set_contact_status(
            contact_id=contact.contact_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')

    def test_sqlite_reopens_after_restart(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteIetStore(path)
            service = IetService(
                IetPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            contact = service.add_contact(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Pat', legal_name='Pat Singh', kind='email',
                alias='pat@example.com', rail='autodeposit', default_account='1001',
            )
            service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='10.00', trace_id='persist',
            )
            reopened = IetService(
                IetPolicy(),
                SqliteIetStore(path),
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            snap = reopened.snapshot('alice')
            self.assertEqual(snap['contacts'][0]['nickname'], 'Pat')
            self.assertEqual(snap['transfers'][0]['status'], 'completed')
            self.assertNotIn('alias_value', snap['contacts'][0])
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
