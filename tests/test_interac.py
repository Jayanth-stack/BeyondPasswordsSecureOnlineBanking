import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.interac import (
    CadUsdBook,
    InteracAlias,
    InteracClock,
    InteracError,
    InteracPolicy,
    InteracService,
    MemoryInteracStore,
    SqliteInteracStore,
    alias_fingerprint,
    answer_fingerprint,
    answers_match,
    compose_cpa_routing,
    compose_iet,
    compose_reference,
    cpa_checksum_ok,
    mask_alias,
    mask_email,
    mask_phone,
    mask_reference,
    message_from_iet,
    normalize_cpa_routing,
    normalize_email,
    normalize_phone,
    normalize_reference,
    parse_iet,
    payments_canada_holidays,
    split_iet_file,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '000100016'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_cpa_official_fixture_and_compose(self):
        self.assertTrue(cpa_checksum_ok(RECEIVER))
        self.assertEqual(compose_cpa_routing('001', '00016'), RECEIVER)
        self.assertEqual(normalize_cpa_routing('000-100-016'), RECEIVER)
        with self.assertRaises(InteracError) as ctx:
            normalize_cpa_routing('000100017')
        self.assertEqual(ctx.exception.code, 'invalid_routing')
        other = compose_cpa_routing('004', '00044')
        self.assertTrue(cpa_checksum_ok(other))
        self.assertNotEqual(other, RECEIVER)

    def test_email_phone_mask_never_full_value(self):
        self.assertEqual(normalize_email('Ada.Lovelace@Example.COM'), 'ada.lovelace@example.com')
        self.assertEqual(mask_email('ada.lovelace@example.com'), 'a***@example.com')
        self.assertEqual(normalize_phone('+1 (416) 555-1234'), '4165551234')
        self.assertEqual(mask_phone('4165551234'), '***-***-1234')
        self.assertEqual(mask_alias('email', 'ada@example.com'), 'a***@example.com')
        with self.assertRaises(InteracError) as ctx:
            normalize_phone('2125551234')
        self.assertEqual(ctx.exception.code, 'invalid_alias')
        with self.assertRaises(InteracError):
            normalize_email('not-an-email')

    def test_reference_layout(self):
        self.assertEqual(compose_reference('20240614', 1), 'IET202406140001')
        self.assertEqual(normalize_reference('iet-20240614-0001'), 'IET202406140001')
        self.assertEqual(mask_reference('IET202406140001'), 'IET202*****0001')
        with self.assertRaises(InteracError) as ctx:
            compose_reference('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')

    def test_cadusd_quote(self):
        book = CadUsdBook(Decimal('0.740000'))
        quote = book.quote(Decimal('100.00'))
        self.assertEqual(quote['amount_cad'], '100.00')
        self.assertEqual(quote['amount_usd'], '74.00')
        self.assertEqual(quote['currency'], 'CAD')

    def test_payments_canada_clock_is_24_7(self):
        clock = InteracClock(tz_offset_hours=-4)
        self.assertIn(datetime(2024, 7, 1).date(), payments_canada_holidays(2024))
        self.assertIn(datetime(2024, 3, 29).date(), payments_canada_holidays(2024))
        snap = clock.snapshot(ts(2024, 7, 1, 23, 0))
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertFalse(snap['after_cutoff'])
        self.assertFalse(snap['business_day'])
        self.assertEqual(snap['value_date'], '2024-07-01')

    def test_iet_roundtrip_rejects_xml(self):
        record = compose_iet(
            reference='IET202406140001',
            amount=Decimal('25.00'),
            sender_name='Ada Lovelace',
            sender_fi=RECEIVER,
            kind='email',
            alias='ada@example.com',
            rail='autodeposit',
            receiver=RECEIVER,
        )
        parsed = parse_iet(record)
        self.assertEqual(parsed['reference'], 'IET202406140001')
        self.assertEqual(parsed['alias'], 'ada@example.com')
        self.assertEqual(message_from_iet(record)['rail'], 'autodeposit')
        with self.assertRaises(InteracError) as ctx:
            split_iet_file('<?xml version="1.0"?><!DOCTYPE iet><IET1></IET1>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(InteracError):
            parse_iet('<!ENTITY xxe SYSTEM "file:///etc/passwd">')

    def test_answer_fingerprint_is_case_insensitive_and_constant_length(self):
        left = answer_fingerprint('Blue Jay')
        right = answer_fingerprint('  BLUE   jay ')
        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)
        self.assertTrue(answers_match(left, 'blue jay'))
        self.assertFalse(answers_match(left, 'cardinal'))


class InteracServiceTests(unittest.TestCase):
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
            if userid != 'alice':
                return {}
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = InteracPolicy(
            dual_control_threshold=Decimal('10000.00'),
            autodeposit_cap=Decimal('10000.00'),
            question_cap=Decimal('25000.00'),
            receiver_routing=RECEIVER,
        )
        self.service = InteracService(
            policy,
            MemoryInteracStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=InteracClock(tz_offset_hours=-4),
        )

    def _alias(self, **kwargs):
        return self.service.add_alias(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Home email'),
            kind=kwargs.pop('kind', 'email'),
            value=kwargs.pop('value', 'ada@example.com'),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'reference': values.pop('reference', 'IET202406140001'),
            'amount': values.pop('amount', '100.00'),
            'sender_name': values.pop('sender_name', 'Ada Lovelace'),
            'sender_fi': RECEIVER,
            'alias_type': values.pop('alias_type', 'email'),
            'alias': values.pop('alias', 'ada@example.com'),
            'rail': values.pop('rail', 'autodeposit'),
            'receiver_routing': values.pop('receiver_routing', RECEIVER),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_alias_or_answer(self):
        alias = self._alias()
        payload = alias.to_dict()
        self.assertNotIn('value', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['alias_masked'], 'a***@example.com')
        self.assertEqual(payload['account_last4'], '1001')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('alias_value', dumped)
        self.assertNotIn('answer_digest', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['alias_masked'], 'a***@example.com')
        snap = self.service.snapshot('alice')
        self.assertNotIn('alias_value', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '74.00', 'interac from Ada Lovelace'))

    def test_autodeposit_is_idempotent_by_reference(self):
        self._alias()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_alias_does_not_credit(self):
        inbound, _ = self._ingest(alias='other@example.com')
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_alias(InteracAlias(
            alias_id='cardalias', userid='alice', nickname='Card', kind='email',
            value='ada@example.com', fingerprint=alias_fingerprint('email', 'ada@example.com'),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(InteracError) as ctx:
            self.service.add_alias(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card sms', kind='sms', value='4165559999', destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._alias()
        inbound, _ = self._ingest(sender_name='Blocked Person', reference='IET202406140002')
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(InteracError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '74.00')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('50.00')
        self._alias()
        inbound, _ = self._ingest(amount='100.00', reference='IET202406140003')
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(InteracError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_question_claim_posts_and_locks_after_three_misses(self):
        self._alias()
        inbound, _ = self._ingest(
            rail='question', question='Favourite bird?', answer='blue jay',
            amount='10.00', reference='IET202406140004',
        )
        self.assertEqual(inbound.status, 'pending_claim')
        self.assertEqual(self.credits, [])
        dumped = inbound.to_dict()
        self.assertEqual(dumped['question'], 'Favourite bird?')
        self.assertNotIn('answer_digest', dumped)
        with self.assertRaises(InteracError) as ctx:
            self.service.claim(
                inbound_id=inbound.inbound_id, actor='alice', actor_type='customer', answer='wrong',
            )
        self.assertEqual(ctx.exception.code, 'invalid_answer')
        posted = self.service.claim(
            inbound_id=inbound.inbound_id, actor='alice', actor_type='customer', answer='BLUE JAY',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '7.40')

        locked, _ = self._ingest(
            rail='question', question='Favourite bird?', answer='blue jay',
            amount='10.00', reference='IET202406140005',
        )
        for _ in range(3):
            with self.assertRaises(InteracError):
                self.service.claim(
                    inbound_id=locked.inbound_id, actor='alice', actor_type='customer', answer='nope',
                )
        with self.assertRaises(InteracError) as locked_ctx:
            self.service.claim(
                inbound_id=locked.inbound_id, actor='alice', actor_type='customer', answer='blue jay',
            )
        self.assertEqual(locked_ctx.exception.code, 'claim_locked')

    def test_assign_rematch_and_return_window(self):
        inbound, _ = self._ingest(alias='new@example.com', reference='IET202406140006', amount='10.00')
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Work',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.to_dict()['alias_masked'], 'n***@example.com')
        returned = self.service.request_return(
            inbound_id=assigned.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0], ('1001', '7.40', 'interac return 202406140006'))
        with self.assertRaises(InteracError) as ctx:
            self.service.request_return(
                inbound_id=assigned.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'already_returned')

        late, _ = self._ingest(alias='new@example.com', reference='IET202406140007', amount='10.00')
        self.assertEqual(late.status, 'posted')
        self.now[0] = ts(2024, 6, 16, 16, 0)
        with self.assertRaises(InteracError) as window:
            self.service.request_return(
                inbound_id=late.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(window.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=late.inbound_id, actor='teller', actor_type='tier1', reason='cust',
        )
        self.assertEqual(staff.status, 'returned')

    def test_wrong_receiver_and_rail_cap_and_expiry(self):
        self._alias()
        other = compose_cpa_routing('004', '00044')
        with self.assertRaises(InteracError) as ctx:
            self._ingest(receiver_routing=other, reference='IET202406140008')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(InteracError) as cap:
            self._ingest(amount='10000.01', reference='IET202406140009')
        self.assertEqual(cap.exception.code, 'autodeposit_amount_exceeded')
        inbound, _ = self._ingest(
            reference='IET202406140010', amount='10.00', expiry='20240613',
            alias='gone@example.com',
        )
        self.assertEqual(inbound.status, 'expired')
        due = self.service.run_due('alice')
        self.assertEqual(due, [])

    def test_pause_resume_rematch_and_sqlite_roundtrip(self):
        alias = self._alias()
        paused = self.service.set_alias_status(
            alias_id=alias.alias_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        inbound, _ = self._ingest(reference='IET202406140011', amount='10.00')
        self.assertEqual(inbound.status, 'unmatched')
        resumed = self.service.set_alias_status(
            alias_id=alias.alias_id, actor='alice', actor_type='customer', status='active',
        )
        self.assertEqual(resumed.status, 'active')
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteInteracStore(path)
            service = InteracService(
                InteracPolicy(receiver_routing=RECEIVER),
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
            row = service.add_alias(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Persist', kind='email', value='persist@example.com',
                destination_account='1001',
            )
            loaded = SqliteInteracStore(path).get_alias(row.alias_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('value', loaded.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
