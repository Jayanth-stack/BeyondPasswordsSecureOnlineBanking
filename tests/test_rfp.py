import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.wire import AmountError, aba_check_digit_ok, money_str, parse_money, us_fed_holidays
from utility.rfp import (
    InstantClock,
    MemoryRfpStore,
    RfpEnrollment,
    RfpError,
    RfpPolicy,
    RfpService,
    SqliteRfpStore,
    alias_fingerprint,
    compose_end_to_end_id,
    compose_pain013,
    compose_pacs008,
    compose_uetr,
    mask_email,
    mask_end_to_end_id,
    mask_phone,
    message_from_pain013,
    normalize_email,
    normalize_end_to_end_id,
    normalize_participant,
    normalize_phone,
    parse_pain013,
    split_pain013_file,
)

ET = timezone(timedelta(hours=-4))
RECEIVER = '021000021'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_aba_and_end_to_end_id(self):
        self.assertTrue(aba_check_digit_ok(RECEIVER))
        self.assertEqual(normalize_participant('21000021'), RECEIVER)
        with self.assertRaises(RfpError) as ctx:
            normalize_participant('021000022')
        self.assertEqual(ctx.exception.code, 'invalid_routing')
        self.assertEqual(compose_end_to_end_id('20240614', 1), 'RFP20240614000001')
        self.assertEqual(normalize_end_to_end_id('rfp-20240614-000001'), 'RFP20240614000001')
        self.assertEqual(mask_end_to_end_id('RFP20240614000001'), 'RFP202*****0001')
        with self.assertRaises(RfpError) as bad:
            compose_end_to_end_id('2024-06-14', 1)
        self.assertEqual(bad.exception.code, 'invalid_reference')
        uetr = compose_uetr()
        self.assertEqual(len(uetr), 36)
        self.assertEqual(uetr, uetr.upper())

    def test_email_phone_mask_never_full_value(self):
        self.assertEqual(normalize_email('Ada.Lovelace@Example.COM'), 'ada.lovelace@example.com')
        self.assertEqual(mask_email('ada.lovelace@example.com'), 'a***@example.com')
        self.assertEqual(normalize_phone('+1 (212) 555-0199'), '2125550199')
        self.assertEqual(mask_phone('2125550199'), '***-***-0199')
        with self.assertRaises(RfpError) as ctx:
            normalize_phone('0125551234')
        self.assertEqual(ctx.exception.code, 'invalid_alias')

    def test_clock_is_24_7_on_july4(self):
        clock = InstantClock(tz_offset_hours=-4)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        snap = clock.snapshot(ts(2024, 7, 4, 23, 0))
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertFalse(snap['after_cutoff'])
        self.assertFalse(snap['business_day'])
        self.assertEqual(snap['value_date'], '2024-07-04')

    def test_pain013_roundtrip_rejects_xml(self):
        record = compose_pain013(
            end_to_end_id='RFP20240614000001',
            amount=Decimal('25.00'),
            rail='fednow',
            creditor_name='Ada Lovelace',
            creditor_aba=RECEIVER,
            creditor_account='77881234',
            debtor_account='1001',
            receiver=RECEIVER,
        )
        parsed = parse_pain013(record)
        self.assertEqual(parsed['end_to_end_id'], 'RFP20240614000001')
        self.assertEqual(parsed['debtor_account'], '1001')
        self.assertEqual(message_from_pain013(record)['rail'], 'fednow')
        with self.assertRaises(RfpError) as ctx:
            split_pain013_file('<?xml version="1.0"?><!DOCTYPE rfp><pain.013></pain.013>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(RfpError):
            parse_pain013('<!ENTITY xxe SYSTEM "file:///etc/passwd">')
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')


class RfpServiceTests(unittest.TestCase):
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

        policy = RfpPolicy(
            dual_control_threshold=Decimal('10000.00'),
            fednow_cap=Decimal('100000.00'),
            rtp_cap=Decimal('1000000.00'),
            zelle_cap=Decimal('2500.00'),
            receiver_routing=RECEIVER,
        )
        self.service = RfpService(
            policy,
            MemoryRfpStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=InstantClock(tz_offset_hours=-4),
        )

    def _enroll(self, **kwargs):
        return self.service.add_enrollment(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname=kwargs.pop('nickname', 'Checking'),
            destination_account=kwargs.pop('destination_account', '1001'),
            kind=kwargs.pop('kind', 'none'),
            value=kwargs.pop('value', ''),
            **kwargs,
        )

    def _ingest(self, actor='teller', actor_type='tier1', **values):
        body = {
            'end_to_end_id': values.pop('end_to_end_id', 'RFP20240614000001'),
            'amount': values.pop('amount', '100.00'),
            'creditor_name': values.pop('creditor_name', 'Ada Lovelace'),
            'creditor_aba': RECEIVER,
            'creditor_account': values.pop('creditor_account', '77881234'),
            'debtor_account': values.pop('debtor_account', '1001'),
            'rail': values.pop('rail', 'fednow'),
            'receiver_routing': values.pop('receiver_routing', RECEIVER),
        }
        body.update(values)
        return self.service.ingest(actor=actor, actor_type=actor_type, values=body)

    def test_snapshot_and_to_dict_never_leak_accounts_or_alias(self):
        enrollment = self._enroll(kind='email', value='ada@example.com')
        payload = enrollment.to_dict()
        self.assertNotIn('alias_value', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('destination_account', payload)
        self.assertEqual(payload['alias_masked'], 'a***@example.com')
        self.assertEqual(payload['account_last4'], '1001')
        inbound, created = self._ingest()
        self.assertTrue(created)
        dumped = inbound.to_dict()
        self.assertNotIn('creditor_account', dumped)
        self.assertNotIn('alias_value', dumped)
        self.assertNotIn('debtor_account', dumped)
        self.assertEqual(dumped['creditor_last4'], '1234')
        self.assertEqual(dumped['status'], 'requested')
        self.assertEqual(self.debits, [])
        snap = self.service.snapshot('alice')
        self.assertNotIn('creditor_account', snap['requests'][0])

    def test_pay_is_idempotent_by_end_to_end_id(self):
        self._enroll()
        first, created = self._ingest()
        again, created_again = self._ingest()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.request_id, again.request_id)
        paid = self.service.pay(request_id=first.request_id, actor='alice', actor_type='customer')
        self.assertEqual(paid.status, 'paid')
        self.assertEqual(self.debits[0], ('1001', '100.00', 'rfp to Ada Lovelace'))
        self.assertTrue(paid.uetr)
        exported = compose_pacs008(paid)
        self.assertTrue(exported.startswith('PACS008|'))
        self.assertNotIn('77881234', exported)

    def test_unmatched_account_does_not_debit(self):
        inbound, _ = self._ingest(debtor_account='1001')
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(self.debits, [])

    def test_credit_card_destination_stays_unmatched(self):
        now = self.now[0]
        self.service.store.put_enrollment(RfpEnrollment(
            enrollment_id='cardenroll', userid='alice', nickname='Card',
            destination_account='1003', alias_kind='none', alias_value='',
            fingerprint='', status='active', actor='alice', created_at=now, updated_at=now,
        ))
        inbound, _ = self._ingest(debtor_account='1003')
        self.assertEqual(inbound.status, 'unmatched')
        self.assertEqual(inbound.reason, 'credit_not_allowed')
        self.assertEqual(self.debits, [])
        with self.assertRaises(RfpError) as ctx:
            self.service.add_enrollment(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Card pay', destination_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_ofac_hold_then_staff_override_then_pay(self):
        self._enroll()
        inbound, _ = self._ingest(creditor_name='Blocked Person', end_to_end_id='RFP20240614000002')
        self.assertEqual(inbound.status, 'held')
        self.assertTrue(inbound.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(RfpError) as ctx:
            self.service.pay(request_id=inbound.request_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        requested = self.service.override_ofac(
            request_id=inbound.request_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(requested.status, 'requested')
        paid = self.service.pay(request_id=requested.request_id, actor='alice', actor_type='customer')
        self.assertEqual(paid.status, 'paid')
        self.assertEqual(self.debits[0][1], '100.00')

    def test_dual_control_requires_different_employee(self):
        self.service.policy.dual_control_threshold = Decimal('50.00')
        self._enroll()
        inbound, _ = self._ingest(amount='100.00', end_to_end_id='RFP20240614000003')
        pending = self.service.pay(request_id=inbound.request_id, actor='alice', actor_type='customer')
        self.assertEqual(pending.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(RfpError) as ctx:
            self.service.release(request_id=inbound.request_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        paid = self.service.release(request_id=inbound.request_id, actor='manager', actor_type='tier2')
        self.assertEqual(paid.status, 'paid')
        self.assertEqual(len(self.debits), 1)

    def test_zelle_alias_pay_and_decline(self):
        self._enroll(kind='email', value='ada@example.com')
        inbound, _ = self._ingest(
            rail='zelle', amount='10.00', end_to_end_id='RFP20240614000004',
            alias_type='email', alias='ada@example.com', debtor_account='',
        )
        self.assertEqual(inbound.status, 'requested')
        dumped = inbound.to_dict()
        self.assertTrue(dumped['payable'])
        self.assertNotIn('alias_value', dumped)
        paid = self.service.pay(request_id=inbound.request_id, actor='alice', actor_type='customer')
        self.assertEqual(paid.status, 'paid')
        self.assertEqual(self.debits[0][1], '10.00')

        declined, _ = self._ingest(
            rail='zelle', amount='10.00', end_to_end_id='RFP20240614000005',
            alias_type='email', alias='ada@example.com', debtor_account='',
        )
        rejected = self.service.decline(
            request_id=declined.request_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(rejected.status, 'declined')
        self.assertEqual(len(self.debits), 1)

    def test_assign_rematch_and_staff_return_after_pay(self):
        inbound, _ = self._ingest(debtor_account='1001', end_to_end_id='RFP20240614000006', amount='10.00')
        self.assertEqual(inbound.status, 'unmatched')
        assigned = self.service.assign(
            request_id=inbound.request_id, actor='teller', actor_type='tier1',
            customer_id='alice', account='1001', nickname='Home',
        )
        self.assertEqual(assigned.status, 'requested')
        paid = self.service.pay(request_id=assigned.request_id, actor='alice', actor_type='customer')
        self.assertEqual(paid.status, 'paid')
        with self.assertRaises(RfpError) as ctx:
            self.service.pay(request_id=assigned.request_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'already_paid')
        returned = self.service.return_request(
            request_id=assigned.request_id, actor='teller', actor_type='tier1', reason='other',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.credits[0], ('1001', '10.00', 'rfp return 240614000006'))

    def test_wrong_receiver_and_rail_cap_and_expiry(self):
        self._enroll()
        with self.assertRaises(RfpError) as ctx:
            self._ingest(receiver_routing='011401533', end_to_end_id='RFP20240614000008')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')
        with self.assertRaises(RfpError) as cap:
            self._ingest(amount='100000.01', end_to_end_id='RFP20240614000009')
        self.assertEqual(cap.exception.code, 'fednow_amount_exceeded')
        with self.assertRaises(RfpError) as zcap:
            self._ingest(
                rail='zelle', amount='2500.01', end_to_end_id='RFP20240614000012',
                alias_type='email', alias='ada@example.com', debtor_account='',
            )
        self.assertEqual(zcap.exception.code, 'zelle_amount_exceeded')
        inbound, _ = self._ingest(
            end_to_end_id='RFP20240614000010', amount='10.00', expiry='20240613',
            debtor_account='9999',
        )
        self.assertEqual(inbound.status, 'expired')
        due = self.service.run_due('alice')
        self.assertEqual(due, [])

    def test_pause_resume_rematch_and_sqlite_roundtrip(self):
        enrollment = self._enroll()
        paused = self.service.set_enrollment_status(
            enrollment_id=enrollment.enrollment_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        inbound, _ = self._ingest(end_to_end_id='RFP20240614000011', amount='10.00')
        self.assertEqual(inbound.status, 'unmatched')
        resumed = self.service.set_enrollment_status(
            enrollment_id=enrollment.enrollment_id, actor='alice', actor_type='customer', status='active',
        )
        self.assertEqual(resumed.status, 'active')
        rematched = self.service.store.get_request(inbound.request_id)
        self.assertEqual(rematched.status, 'requested')

        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteRfpStore(path)
            service = RfpService(
                RfpPolicy(receiver_routing=RECEIVER),
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
            row = service.add_enrollment(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Persist', destination_account='1001',
                kind='email', value='persist@example.com',
            )
            loaded = SqliteRfpStore(path).get_enrollment(row.enrollment_id)
            self.assertEqual(loaded.nickname, 'Persist')
            self.assertNotIn('alias_value', loaded.to_dict())
            self.assertEqual(alias_fingerprint('email', 'persist@example.com'), loaded.fingerprint)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
