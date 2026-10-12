import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, aba_check_digit_ok, money_str, parse_money, us_fed_holidays
from utility.zrecv import (
    MemoryZrecvStore,
    SqliteZrecvStore,
    ZelleClock,
    ZrecvError,
    ZrecvPolicy,
    ZrecvService,
    ZrecvToken,
    compose_payment_id,
    compose_zel1,
    mask_email,
    mask_payment_id,
    mask_phone,
    message_from_zel1,
    normalize_email,
    normalize_participant,
    normalize_payment_id,
    normalize_phone,
    parse_zel1,
    split_zel1_file,
    token_fingerprint,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '021000021'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_aba_participant_and_payment_id(self):
        self.assertTrue(aba_check_digit_ok(RECEIVER))
        self.assertEqual(normalize_participant('21000021'), RECEIVER)
        with self.assertRaises(ZrecvError) as ctx:
            normalize_participant('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_routing')
        self.assertEqual(compose_payment_id('20240614', 1), 'ZEL202406140001')
        self.assertEqual(normalize_payment_id('zel-20240614-0001'), 'ZEL202406140001')
        self.assertEqual(mask_payment_id('ZEL202406140001'), 'ZEL202*****0001')
        with self.assertRaises(ZrecvError) as bad:
            compose_payment_id('2024-06-14', 1)
        self.assertEqual(bad.exception.code, 'invalid_reference')

    def test_email_phone_mask_never_full_value(self):
        self.assertEqual(normalize_email('Ada.Lovelace@Example.COM'), 'ada.lovelace@example.com')
        self.assertEqual(mask_email('ada.lovelace@example.com'), 'a***@example.com')
        self.assertEqual(normalize_phone('+1 (212) 555-0199'), '2125550199')
        self.assertEqual(mask_phone('2125550199'), '***-***-0199')
        with self.assertRaises(ZrecvError) as ctx:
            normalize_phone('0125551234')
        self.assertEqual(ctx.exception.code, 'invalid_alias')
        with self.assertRaises(ZrecvError):
            normalize_email('not-an-email')

    def test_clock_is_24_7_on_july4(self):
        clock = ZelleClock(tz_offset_hours=-4)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        snap = clock.snapshot(ts(2024, 7, 4, 23, 0))
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertFalse(snap['after_cutoff'])
        self.assertFalse(snap['business_day'])
        self.assertEqual(snap['value_date'], '2024-07-04')

    def test_zel1_roundtrip_rejects_xml(self):
        record = compose_zel1(
            payment_id='ZEL202406140001',
            amount=Decimal('25.00'),
            sender_name='Ada Lovelace',
            sender_fi=RECEIVER,
            kind='email',
            alias='ada@example.com',
            rail='enrolled',
            receiver=RECEIVER,
        )
        parsed = parse_zel1(record)
        self.assertEqual(parsed['payment_id'], 'ZEL202406140001')
        self.assertEqual(parsed['alias'], 'ada@example.com')
        self.assertEqual(message_from_zel1(record)['rail'], 'enrolled')
        with self.assertRaises(ZrecvError) as ctx:
            split_zel1_file('<?xml version="1.0"?><!DOCTYPE zelle><ZEL1></ZEL1>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(ZrecvError):
            parse_zel1('<!ENTITY xxe SYSTEM "file:///etc/passwd">')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class ZrecvServiceTests(unittest.TestCase):
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

        policy = ZrecvPolicy(
            dual_control_threshold=Decimal('10000.00'),
            enrolled_cap=Decimal('15000.00'),
            request_cap=Decimal('2500.00'),
            receiver_routing=RECEIVER,
        )
        self.service = ZrecvService(
            policy,
            MemoryZrecvStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=ZelleClock(tz_offset_hours=-4),
        )

    def _token(self, **kwargs):
        return self.service.add_token(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Home email'),
            kind=kwargs.pop('kind', 'email'),
            value=kwargs.pop('value', 'ada@example.com'),
            destination_account=kwargs.pop('destination_account', '1001'),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'payment_id': values.pop('payment_id', 'ZEL202406140001'),
            'amount': values.pop('amount', '100.00'),
            'sender_name': values.pop('sender_name', 'Ada Lovelace'),
            'sender_fi': RECEIVER,
            'alias_type': values.pop('alias_type', 'email'),
            'alias': values.pop('alias', 'ada@example.com'),
            'rail': values.pop('rail', 'enrolled'),
            'receiver_routing': values.pop('receiver_routing', RECEIVER),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_token(self):
        token = self._token()
        payload = token.to_dict()
        self.assertNotIn('value', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['alias_masked'], 'a***@example.com')
        self.assertEqual(payload['account_last4'], '1001')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('alias_value', dumped)
        self.assertNotIn('destination_account', dumped)
        self.assertEqual(dumped['alias_masked'], 'a***@example.com')
        snap = self.service.snapshot('alice')
        self.assertNotIn('alias_value', snap['inbounds'][0])
        self.assertEqual(inbound.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '100.00', 'zelle from Ada Lovelace'))

    def test_enrolled_is_idempotent_by_payment_id(self):
        self._token()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_unmatched_token_does_not_credit(self):
        inbound, _ = self._ingest(alias='other@example.com')
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.credits, [])

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_token(ZrecvToken(
            token_id='cardtoken', userid='alice', nickname='Card', kind='email',
            value='ada@example.com', fingerprint=token_fingerprint('email', 'ada@example.com'),
            destination_account='1003', status='active', actor='alice',
            created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest()
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.credits, [])
        with self.assertRaises(ZrecvError) as ctx:
            self.service.add_token(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card sms', kind='sms', value='2125559999', destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_posts(self):
        self._token()
        inbound, _ = self._ingest(sender_name='Blocked Person', payment_id='ZEL202406140002')
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(ZrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='other', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'not_releasable')
        posted = self.service.override_ofac(
            inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '100.00')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('50.00')
        self._token()
        inbound, _ = self._ingest(amount='100.00', payment_id='ZEL202406140003')
        self.assertEqual(inbound.status, 'pending_release')
        with self.assertRaises(ZrecvError) as ctx:
            self.service.release(inbound_id=inbound.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=inbound.inbound_id, actor='manager', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_request_claim_posts_without_security_answer(self):
        self._token()
        inbound, _ = self._ingest(
            rail='request', amount='10.00', payment_id='ZEL202406140004',
        )
        self.assertEqual(inbound.status, 'pending_claim')
        self.assertEqual(self.credits, [])
        dumped = inbound.to_dict()
        self.assertTrue(dumped['claimable'])
        self.assertNotIn('alias_value', dumped)
        posted = self.service.claim(
            inbound_id=inbound.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.credits[0][1], '10.00')

        declined, _ = self._ingest(
            rail='request', amount='10.00', payment_id='ZEL202406140005',
        )
        rejected = self.service.decline(
            inbound_id=declined.inbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(rejected.status, 'rejected')
        self.assertEqual(len(self.credits), 1)

    def test_assign_rematch_and_return_window(self):
        inbound, _ = self._ingest(alias='new@example.com', payment_id='ZEL202406140006', amount='10.00')
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
        self.assertEqual(self.debits[0], ('1001', '10.00', 'zelle return 202406140006'))
        with self.assertRaises(ZrecvError) as ctx:
            self.service.request_return(
                inbound_id=assigned.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'already_returned')

        late, _ = self._ingest(alias='new@example.com', payment_id='ZEL202406140007', amount='10.00')
        self.assertEqual(late.status, 'posted')
        self.now[0] = ts(2024, 6, 16, 16, 0)
        with self.assertRaises(ZrecvError) as window:
            self.service.request_return(
                inbound_id=late.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(window.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=late.inbound_id, actor='teller', actor_type='tier1', reason='cust',
        )
        self.assertEqual(staff.status, 'returned')

    def test_wrong_receiver_and_rail_cap_and_expiry(self):
        self._token()
        with self.assertRaises(ZrecvError) as ctx:
            self._ingest(receiver_routing='011401533', payment_id='ZEL202406140008')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(ZrecvError) as cap:
            self._ingest(amount='15000.01', payment_id='ZEL202406140009')
        self.assertEqual(cap.exception.code, 'enrolled_amount_exceeded')
        with self.assertRaises(ZrecvError) as req:
            self._ingest(rail='request', amount='2500.01', payment_id='ZEL202406140012')
        self.assertEqual(req.exception.code, 'request_amount_exceeded')
        inbound, _ = self._ingest(
            payment_id='ZEL202406140010', amount='10.00', expiry='20240613',
            alias='gone@example.com',
        )
        self.assertEqual(inbound.status, 'expired')
        due = self.service.run_due('alice')
        self.assertEqual(due, [])

    def test_pause_resume_rematch_and_sqlite_roundtrip(self):
        token = self._token()
        paused = self.service.set_token_status(
            token_id=token.token_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        inbound, _ = self._ingest(payment_id='ZEL202406140011', amount='10.00')
        self.assertEqual(inbound.status, 'unmatched')
        resumed = self.service.set_token_status(
            token_id=token.token_id, actor='alice', actor_type='customer', status='active',
        )
        self.assertEqual(resumed.status, 'active')
        rematched = self.service.store.get_inbound(inbound.inbound_id)
        self.assertEqual(rematched.status, 'posted')

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteZrecvStore(path)
            service = ZrecvService(
                ZrecvPolicy(receiver_routing=RECEIVER),
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
            row = service.add_token(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Persist', kind='email', value='persist@example.com',
                destination_account='1001',
            )
            loaded = SqliteZrecvStore(path).get_token(row.token_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('value', loaded.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
