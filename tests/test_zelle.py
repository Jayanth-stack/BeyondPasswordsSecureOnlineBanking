import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.zelle import (
    MemoryZelleStore,
    SqliteZelleStore,
    ZelleClock,
    ZelleError,
    ZellePolicy,
    ZelleService,
    compose_payment_id,
    compose_zel1,
    compute_fee,
    mask_email,
    mask_payment_id,
    mask_phone,
    normalize_email,
    normalize_participant,
    normalize_phone,
    parse_zel1,
    token_fingerprint,
    us_fed_holidays,
)
from utility.wire import us_fed_holidays as wire_holidays


ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_participant_aba_checksum(self):
        self.assertEqual(normalize_participant('21000021'), '021000021')
        with self.assertRaises(ZelleError) as ctx:
            normalize_participant('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_routing')

    def test_token_normalize_and_mask(self):
        self.assertEqual(normalize_email('Ada.Lovelace@Example.COM'), 'ada.lovelace@example.com')
        self.assertEqual(mask_email('ada.lovelace@example.com'), 'a***@example.com')
        self.assertEqual(normalize_phone('+1 (415) 555-0199'), '4155550199')
        self.assertEqual(mask_phone('4155550199'), '***-***-0199')
        with self.assertRaises(ZelleError) as ctx:
            normalize_phone('0555550199')
        self.assertEqual(ctx.exception.code, 'invalid_alias')
        with self.assertRaises(ZelleError):
            normalize_email('not-an-email')
        digest = token_fingerprint('email', 'ada@example.com')
        self.assertNotIn('ada', digest)
        self.assertEqual(len(digest), 64)

    def test_payment_id_and_zel1_roundtrip(self):
        self.assertEqual(compose_payment_id('20240614', 7), 'ZEL202406140007')
        self.assertEqual(mask_payment_id('ZEL202406140007'), 'ZEL202*****0007')
        record = compose_zel1(
            payment_id='ZEL202406140007',
            amount=Decimal('25.00'),
            recipient_name='Ada Lovelace',
            sender_fi='021000021',
            kind='email',
            alias='ada@example.com',
            rail='enrolled',
        )
        parsed = parse_zel1(record)
        self.assertEqual(parsed['payment_id'], 'ZEL202406140007')
        self.assertEqual(parsed['alias'], 'ada@example.com')
        self.assertEqual(parsed['currency'], 'USD')
        self.assertEqual(parsed['network'], 'ZELLE')
        with self.assertRaises(ZelleError) as ctx:
            parse_zel1('<?xml version="1.0"?><zelle/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')

    def test_zero_fee_and_july4_clock_is_247(self):
        self.assertEqual(compute_fee(Decimal('100.00'), 'enrolled', Decimal('0.00'), Decimal('0.00')), Decimal('0.00'))
        self.assertEqual(
            compute_fee(Decimal('100.00'), 'request', Decimal('1.00'), Decimal('0.50'), waived=True),
            Decimal('0.00'),
        )
        clock = ZelleClock(tz_offset_hours=-4)
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertIn(datetime(2024, 7, 4).date(), wire_holidays(2024))
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        snap = clock.snapshot(july4)
        self.assertFalse(snap['business_day'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertEqual(snap['value_date'], '2024-07-04')


class ZelleServiceTests(unittest.TestCase):
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

        policy = ZellePolicy(
            enrolled_fee=Decimal('0.00'),
            request_fee=Decimal('0.00'),
            dual_control_threshold=Decimal('10000.00'),
        )
        self.service = ZelleService(
            policy,
            MemoryZelleStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=ZelleClock(tz_offset_hours=-4),
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
            rail=kwargs.pop('rail', 'enrolled'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_never_leaks_token(self):
        contact = self._add(alias='4155550199', kind='sms', rail='request')
        payload = contact.to_dict()
        self.assertNotIn('alias_value', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['alias_masked'], '***-***-0199')
        snap = self.service.snapshot('alice')
        self.assertNotIn('alias_value', snap['contacts'][0])
        transfer, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='100.00', trace_id='t1',
        )
        self.assertTrue(created)
        self.assertEqual(transfer.status, 'pending_claim')
        self.assertNotIn('alias_value', transfer.to_dict())
        self.assertTrue(transfer.payment_id.startswith('ZEL20240614'))
        self.assertEqual(self.debits[0], ('1001', '100.00', 'zelle to Pat'))

    def test_enrolled_completes_247_and_is_idempotent(self):
        self.now[0] = ts(2024, 7, 4, 22, 0)
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
        self.assertEqual(len([row for row in self.debits if row[1] == '50.00']), 1)

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        contact = self._add(nickname='Blocked', name='Blocked Person')
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(transfer.status, 'held')
        self.assertTrue(transfer.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(ZelleError) as ctx:
            self.service.release_transfer(transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            transfer_id=transfer.transfer_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '80.00')

    def test_dual_control_requires_a_different_employee(self):
        contact = self._add(nickname='Big', alias='big@example.com')
        transfer, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            contact_id=contact.contact_id, amount='14000.00', trace_id='hv',
        )
        self.assertEqual(transfer.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(ZelleError) as ctx:
            self.service.release_transfer(transfer_id=transfer.transfer_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_transfer(
            transfer_id=transfer.transfer_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '14000.00')

    def test_request_cancel_credits_and_enrolled_is_irrevocable_for_customer(self):
        request = self._add(nickname='Sam', alias='sam@example.com', rail='request')
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=request.contact_id, amount='20.00', trace_id='req1',
        )
        self.assertEqual(pending.status, 'pending_claim')
        cancelled = self.service.cancel_transfer(
            transfer_id=pending.transfer_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.assertEqual(self.credits[0][1], '20.00')

        enrolled = self._add(nickname='Auto', alias='auto@example.com')
        done, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=enrolled.contact_id, amount='10.00', trace_id='enr1',
        )
        self.assertEqual(done.status, 'completed')
        with self.assertRaises(ZelleError) as ctx:
            self.service.cancel_transfer(transfer_id=done.transfer_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_enrolled_staff_recall_window_and_request_claim(self):
        contact = self._add()
        sent, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='30.00', trace_id='r1',
        )
        recalled = self.service.recall_transfer(
            transfer_id=sent.transfer_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '30.00')

        request = self._add(nickname='Lee', alias='lee@example.com', rail='request')
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=request.contact_id, amount='12.00', trace_id='claim1',
        )
        claimed = self.service.complete_transfer(
            transfer_id=pending.transfer_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(claimed.status, 'completed')
        with self.assertRaises(ZelleError) as ctx:
            self.service.recall_transfer(transfer_id=claimed.transfer_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')

    def test_request_expiry_credits_back(self):
        contact = self._add(rail='request', alias='pine@example.com', nickname='Pine')
        pending, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            contact_id=contact.contact_id, amount='15.00', trace_id='exp1',
        )
        self.assertEqual(pending.status, 'pending_claim')
        self.now[0] = ts(2024, 6, 29, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'expired')
        self.assertEqual(self.credits[0][1], '15.00')

    def test_credit_account_pause_archive_and_caps(self):
        contact = self._add()
        with self.assertRaises(ZelleError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='20.00', internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        with self.assertRaises(ZelleError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='15000.01',
            )
        self.assertEqual(ctx.exception.code, 'enrolled_amount_exceeded')
        paused = self.service.set_contact_status(
            contact_id=contact.contact_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(ZelleError) as ctx:
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
            store = SqliteZelleStore(path)
            service = ZelleService(
                ZellePolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 50}},
            )
            contact = service.add_contact(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Pat', legal_name='Pat Singh', kind='email',
                alias='pat@example.com', rail='enrolled', default_account='1001',
            )
            service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                contact_id=contact.contact_id, amount='10.00', trace_id='persist',
            )
            reopened = ZelleService(
                ZellePolicy(),
                SqliteZelleStore(path),
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
