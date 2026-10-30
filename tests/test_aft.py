import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from utility.aft import (
    AmountError,
    CardNetworkClock,
    MemoryAftStore,
    AftError,
    AftPolicy,
    AftService,
    SqliteAftStore,
    brand_from_iin,
    compose_aft1,
    compose_rrn,
    compose_stan,
    luhn_check_digit_ok,
    mask_pan,
    money_str,
    normalize_mti,
    normalize_pan,
    normalize_processing_code,
    pan_fingerprint,
    parse_money,
    parse_aft1,
    split_aft1_file,
)


UTC = timezone.utc


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


VISA = '4111111111111111'
MC = '5555555555554444'
AMEX = '378282246310005'


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_luhn_and_iin_brand(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertTrue(luhn_check_digit_ok(MC))
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(brand_from_iin(MC), 'mastercard')
        self.assertEqual(brand_from_iin(AMEX), 'amex')
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        with self.assertRaises(AftError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')

    def test_pan_mask_and_fingerprint_never_equal_pan(self):
        masked = mask_pan(VISA)
        self.assertEqual(masked, '411111******1111')
        self.assertNotIn(VISA, masked)
        digest = pan_fingerprint(VISA)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, VISA)

    def test_aft_mti_proc_rrn_stan(self):
        self.assertEqual(normalize_mti('200'), '0200')
        self.assertEqual(normalize_mti('reversal'), '0420')
        self.assertEqual(normalize_processing_code('10'), '100000')
        self.assertEqual(normalize_processing_code('funding'), '100000')
        with self.assertRaises(AftError) as ctx:
            normalize_mti('0260')
        self.assertEqual(ctx.exception.code, 'invalid_mti')
        with self.assertRaises(AftError) as ctx:
            normalize_mti('1100')
        self.assertEqual(ctx.exception.code, 'invalid_mti')
        with self.assertRaises(AftError) as ctx:
            normalize_processing_code('260000')
        self.assertEqual(ctx.exception.code, 'invalid_processing_code')
        self.assertEqual(compose_stan(7), '000007')
        self.assertEqual(compose_rrn('20240614', 7), '240614000007')
        with self.assertRaises(AftError) as ctx:
            compose_rrn('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_rrn')

    def test_aft1_pipe_rejects_xml(self):
        record = compose_aft1(
            mti='0200', processing_code='100000', network='visa_direct',
            pan_masked='411111******1111', brand='visa', amount='10.00',
            rrn_masked='********0007', stan_masked='****07', mcc='6012', purpose='family',
        )
        parsed = parse_aft1(record)
        self.assertEqual(parsed['mti'], '0200')
        self.assertEqual(parsed['processing_code'], '100000')
        self.assertEqual(parsed['network'], 'visa_direct')
        with self.assertRaises(AftError) as ctx:
            parse_aft1('<?xml version="1.0"?><AFT/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(AftError):
            split_aft1_file('<!DOCTYPE aft><root/>')

    def test_clock_is_always_open(self):
        clock = CardNetworkClock(tz_offset_hours=0)
        sunday = ts(2024, 6, 16, 23, 0)
        snap = clock.snapshot(sunday)
        self.assertTrue(snap['always_open'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertEqual(clock.cycle_date(sunday), '20240616')


class AftServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 16, 0)]
        self.debits = []
        self.credits = []
        self.issuer = []

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def issuer_fn(pan_masked, amount, rrn):
            self.issuer.append((pan_masked, amount, rrn))
            return 'ok'

        def accounts_fn(userid):
            return {
                'checkin': {'Account': 1001, 'Balance': 5000},
                'savings': {'Account': 1002, 'Balance': 80},
                'credit': {'Account': 1003, 'Balance': -20},
            }

        policy = AftPolicy(outbound_fee=Decimal('2.00'), dual_control_threshold=Decimal('2500.00'))
        self.service = AftService(
            policy,
            MemoryAftStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            issuer_fn=issuer_fn,
            accounts_fn=accounts_fn,
            calendar=CardNetworkClock(tz_offset_hours=0),
        )

    def _add(self, nickname='Mom Visa', name='Ada Lovelace', pan=VISA, **kwargs):
        return self.service.add_source(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            pan=pan,
            expiry=kwargs.pop('expiry', '1229'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_pan_or_fingerprint(self):
        source = self._add()
        payload = source.to_dict()
        self.assertNotIn('pan', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['pan_masked'], '411111******1111')
        self.assertEqual(payload['last4'], '1111')
        self.assertEqual(payload['brand'], 'visa')
        self.assertEqual(payload['network'], 'visa_direct')
        snap = self.service.snapshot('alice')
        self.assertNotIn('pan', snap['sources'][0])
        self.assertNotIn('fingerprint', snap['sources'][0])
        payment, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='40.00', trace_id='p1',
        )
        self.assertTrue(created)
        body = payment.to_dict()
        self.assertNotIn('pan', body)
        self.assertNotIn('rrn', body)
        self.assertNotIn('stan', body)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.mti, '0200')
        self.assertEqual(payment.processing_code, '100000')
        self.assertTrue(body['rrn_masked'].endswith('0001'))
        self.assertEqual(self.credits[0], ('1001', '40.00', 'card pull Mom Visa'))
        self.assertEqual(self.debits[0][1], '2.00')
        self.assertEqual(self.debits[0][2].split()[0], 'aft')
        self.assertEqual(len(self.issuer), 1)

    def test_amex_rejected_and_mastercard_send(self):
        with self.assertRaises(AftError) as ctx:
            self._add(nickname='Amex', pan=AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')
        source = self._add(nickname='Dad MC', pan=MC)
        self.assertEqual(source.network, 'mastercard_send')
        self.assertEqual(source.brand, 'mastercard')

    def test_same_pull_is_idempotent_by_trace(self):
        source = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='25.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='25.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len([row for row in self.credits if row[1] == '25.00']), 1)

    def test_weekend_still_completes_instantly(self):
        self.now[0] = ts(2024, 6, 16, 23, 0)
        source = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='18.00', trace_id='sun',
        )
        self.assertEqual(payment.status, 'completed')
        self.assertTrue(payment.rrn.startswith('240616'))
        self.assertEqual(self.credits[0][1], '18.00')

    def test_ofac_hold_blocks_credit_until_staff_override(self):
        source = self._add(nickname='Blocked', name='Blocked Person')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(payment.status, 'held')
        self.assertTrue(payment.ofac_hit)
        self.assertEqual(self.credits, [])
        self.assertEqual(self.issuer, [])
        self.assertEqual(payment.rrn, '')
        with self.assertRaises(AftError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.credits[0][1], '80.00')

    def test_dual_control_requires_a_different_employee(self):
        source = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            source_id=source.source_id, amount='2500.00', trace_id='hv',
        )
        self.assertEqual(payment.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(AftError) as ctx:
            self.service.release_payment(payment_id=payment.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=payment.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'completed')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.credits[0][1], '2500.00')

    def test_credit_account_and_pause_and_archive(self):
        source = self._add()
        with self.assertRaises(AftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id=source.source_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_source_status(
            source_id=source.source_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(AftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id=source.source_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'source_paused')
        closed = self.service.set_source_status(
            source_id=source.source_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(AftError) as ctx:
            self.service.set_source_status(
                source_id=source.source_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_issuer_decline_does_not_assign_rrn(self):
        def declined(pan_masked, amount, rrn):
            self.issuer.append((pan_masked, amount, rrn))
            return 'Insufficient Balance'

        self.service.issuer_fn = declined
        source = self._add()
        with self.assertRaises(AftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id=source.source_id, amount='30.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].rrn, '')
        self.assertEqual(self.credits, [])

    def test_aft_cap_is_5000(self):
        source = self._add()
        with self.assertRaises(AftError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                source_id=source.source_id, amount='5000.01',
            )
        self.assertEqual(ctx.exception.code, 'aft_amount_exceeded')

    def test_customer_recall_within_24h_debits_and_sets_0420(self):
        source = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='60.00', trace_id='c1',
        )
        recalled = self.service.recall_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(recalled.mti, '0420')
        self.assertEqual(self.debits[1][1], '60.00')
        self.assertTrue(self.debits[1][2].startswith('card pull return'))
        self.assertEqual(self.credits[1][1], '2.00')

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='12.00', trace_id='late',
        )
        self.now[0] = ts(2024, 6, 16, 17, 0)
        with self.assertRaises(AftError) as ctx:
            self.service.recall_payment(
                payment_id=other.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.recall_payment(
            payment_id=other.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(staff.status, 'recalled')

    def test_customer_can_cancel_held_not_completed(self):
        source = self._add(nickname='Blocked', name='Blocked Person')
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='22.00', trace_id='q1',
        )
        cancelled = self.service.cancel_payment(
            payment_id=payment.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        live_source = self._add(nickname='Clear', pan='4012888888881881')
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=live_source.source_id, amount='22.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'completed')
        with self.assertRaises(AftError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_expired_card_and_export_masks(self):
        with self.assertRaises(AftError) as ctx:
            self._add(expiry='0120')
        self.assertEqual(ctx.exception.code, 'invalid_expiry')
        source = self._add()
        payment, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            source_id=source.source_id, amount='9.00', trace_id='exp1',
        )
        exported = self.service.export_payment(
            payment_id=payment.payment_id, actor='teller', actor_type='tier1',
        )
        self.assertTrue(exported['file'].startswith('AFT1|0200|100000|visa_direct|411111******1111|'))
        self.assertNotIn(VISA, exported['file'])
        self.assertNotIn(payment.rrn, exported['file'])
        with self.assertRaises(AftError) as ctx:
            self.service.export_payment(
                payment_id=payment.payment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'aft_forbidden')

    def test_sqlite_roundtrip_masks_pan(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteAftStore(path)
            service = AftService(
                AftPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                credit_fn=lambda account, amount, remark: 'Success',
                calendar=CardNetworkClock(tz_offset_hours=0),
            )
            source = service.add_source(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', pan=VISA,
                expiry='1229', default_account='1001',
            )
            reloaded = SqliteAftStore(path).get_source(source.source_id)
            self.assertEqual(reloaded.pan, VISA)
            self.assertNotIn('pan', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['last4'], '1111')
            self.assertNotIn('fingerprint', reloaded.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
