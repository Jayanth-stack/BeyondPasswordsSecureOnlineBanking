import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.cardhold import (
    AmountError,
    CardHoldError,
    CardHoldPolicy,
    CardHoldService,
    HoldClock,
    MemoryCardHoldStore,
    SqliteCardHoldStore,
    brand_from_iin,
    capture_limit,
    compose_approval,
    compose_auth1,
    compose_rrn,
    compose_stan,
    expiry_days_for_mcc,
    luhn_check_digit_ok,
    mask_pan,
    mask_rrn,
    mask_stan,
    money_str,
    normalize_mti,
    normalize_pan,
    normalize_rrn,
    normalize_stan,
    pan_fingerprint,
    parse_auth1,
    parse_money,
    split_auth1_file,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
MC = '5555555555554444'
AMEX = '378282246310005'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_luhn_and_brand_from_known_pans(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(brand_from_iin(MC), 'mastercard')
        self.assertEqual(normalize_pan(AMEX)[-4:], '0005')
        self.assertEqual(brand_from_iin(AMEX), 'amex')
        with self.assertRaises(CardHoldError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        with self.assertRaises(CardHoldError):
            normalize_pan('0000000000000000')

    def test_pan_fingerprint_and_mask_never_echo_digits(self):
        self.assertEqual(mask_pan(VISA), '************1111')
        digest = pan_fingerprint(VISA)
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(ch in '0123456789abcdef' for ch in digest))
        self.assertNotIn(VISA, digest)
        self.assertEqual(pan_fingerprint(VISA), pan_fingerprint('4111 1111 1111 1111'))

    def test_stan_rrn_mti_and_auth1(self):
        self.assertEqual(compose_stan(7), '000007')
        self.assertEqual(normalize_stan('42'), '000042')
        self.assertEqual(mask_stan('000042'), '***042')
        self.assertEqual(compose_rrn('20240614', 3), '202406140003')
        self.assertEqual(normalize_rrn('202406140003'), '202406140003')
        self.assertEqual(mask_rrn('202406140003'), '********0003')
        self.assertEqual(normalize_mti('0100'), '1100')
        self.assertEqual(normalize_mti('0200'), '1200')
        self.assertEqual(normalize_mti('0400'), '1400')
        with self.assertRaises(CardHoldError) as ctx:
            normalize_mti('9999')
        self.assertEqual(ctx.exception.code, 'invalid_mti')
        line = compose_auth1(
            stan='000042', rrn='202406140003', mti='1100',
            pan_masked='************1111', amount='25.00',
            merchant='COFFEE SHOP', mcc='5812',
        )
        self.assertTrue(line.startswith('AUTH1|'))
        self.assertNotIn(VISA, line)
        self.assertNotIn('000042', line)
        parsed = parse_auth1(line)
        self.assertEqual(parsed['mti'], '1100')
        with self.assertRaises(CardHoldError) as xml:
            parse_auth1('<?xml version="1.0"?><AUTH1/>')
        self.assertEqual(xml.exception.code, 'invalid_file')
        with self.assertRaises(CardHoldError):
            split_auth1_file('<!DOCTYPE html>\nAUTH1|1|2|1100|4|5.00|X|5812|USD')

    def test_mcc_expiry_and_capture_windows(self):
        self.assertEqual(expiry_days_for_mcc('5812'), 7)
        self.assertEqual(expiry_days_for_mcc('5541'), 2)
        self.assertEqual(expiry_days_for_mcc('7011'), 30)
        self.assertEqual(expiry_days_for_mcc('7512'), 30)
        self.assertEqual(capture_limit('5812', Decimal('40.00')), Decimal('40.00'))
        self.assertEqual(capture_limit('5542', Decimal('1.00')), Decimal('200.00'))
        self.assertEqual(capture_limit('7011', Decimal('100.00')), Decimal('115.00'))

    def test_hold_clock_is_always_open(self):
        clock = HoldClock(tz_offset_hours=-4)
        saturday = ts(2024, 6, 15, 23, 0)
        snap = clock.snapshot(saturday)
        self.assertTrue(snap['network_open'])
        self.assertEqual(snap['cycle_date'], '20240615')
        expires = clock.expires_at(saturday, '5812')
        self.assertEqual(expires, saturday + 7 * 86400)
        self.assertEqual(compose_approval(12), 'A00012')


class CardHoldServiceTests(unittest.TestCase):
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

        self.service = CardHoldService(
            CardHoldPolicy(dual_control_threshold=Decimal('10000.00')),
            MemoryCardHoldStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=HoldClock(tz_offset_hours=-4),
        )

    def _enroll(self, nickname='Visa Daily', pan=VISA, **kwargs):
        return self.service.add_key(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            pan=pan,
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_pan_or_fingerprint(self):
        key = self._enroll()
        payload = key.to_dict()
        self.assertNotIn('fingerprint', payload)
        self.assertNotIn('pan', payload)
        self.assertEqual(payload['last4'], '1111')
        self.assertTrue(payload['pan_masked'].endswith('1111'))
        hold, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='25.00', merchant='COFFEE SHOP', pan=VISA,
            mcc='5812', stan='000101',
        )
        self.assertTrue(created)
        self.assertEqual(hold.status, 'authorized')
        leaked = hold.to_dict()
        self.assertNotIn('stan', leaked)
        self.assertNotIn('rrn', leaked)
        self.assertNotIn('fingerprint', leaked)
        self.assertNotIn(VISA, str(leaked))
        self.assertEqual(leaked['stan_masked'], '***101')
        snap = self.service.snapshot('alice')
        self.assertNotIn('fingerprint', snap['keys'][0])
        self.assertNotIn('stan', snap['holds'][0])
        self.assertEqual(self.debits, [])

    def test_ingest_is_idempotent_by_stan(self):
        self._enroll()
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='25.00', merchant='COFFEE SHOP', pan=VISA, stan='000202',
        )
        again, created_again = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='25.00', merchant='COFFEE SHOP', pan=VISA, stan='000202',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.hold_id, again.hold_id)
        self.assertEqual(first.status, 'authorized')

    def test_unmatched_then_enroll_rematch(self):
        hold, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='18.00', merchant='BOOKSTORE', pan=VISA, stan='000303',
        )
        self.assertEqual(hold.status, 'unmatched')
        self._enroll()
        rematched = self.service.store.get_hold(hold.hold_id)
        self.assertEqual(rematched.status, 'authorized')
        self.assertEqual(rematched.userid, 'alice')
        self.assertEqual(rematched.approval_code[0], 'A')

    def test_ofac_hold_blocks_until_staff_override(self):
        self._enroll()
        hold, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='40.00', merchant='Blocked Person LLC', pan=VISA, stan='000404',
        )
        self.assertEqual(hold.status, 'held')
        self.assertTrue(hold.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(CardHoldError) as ctx:
            self.service.release_hold(hold_id=hold.hold_id, actor='checker', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            hold_id=hold.hold_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'authorized')
        self.assertEqual(self.debits, [])

    def test_dual_control_requires_a_different_employee(self):
        self._enroll()
        hold, _ = self.service.ingest(
            actor='maker', actor_type='tier1',
            amount='10000.00', merchant='JEWELER', pan=VISA, stan='000505',
        )
        self.assertEqual(hold.status, 'pending_release')
        with self.assertRaises(CardHoldError) as ctx:
            self.service.release_hold(hold_id=hold.hold_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_hold(hold_id=hold.hold_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'authorized')
        self.assertEqual(released.releaser, 'checker')

    def test_capture_debits_and_reverse_credits(self):
        self._enroll()
        hold, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='60.00', merchant='GROCER', pan=VISA, stan='000606',
        )
        captured = self.service.capture(
            hold_id=hold.hold_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(captured.status, 'captured')
        self.assertEqual(self.debits[0], ('1001', '60.00', 'card GROCER'))
        reversed_row = self.service.reverse(
            hold_id=hold.hold_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.credits[0][0], '1001')
        self.assertEqual(self.credits[0][1], '60.00')
        self.assertTrue(self.credits[0][2].startswith('card reverse '))

    def test_fuel_partial_and_hotel_overcapture(self):
        self._enroll()
        fuel, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='1.00', merchant='PUMP 4', pan=VISA, mcc='5542', stan='000707',
        )
        posted = self.service.capture(
            hold_id=fuel.hold_id, actor='teller', actor_type='tier1', amount='47.25',
        )
        self.assertEqual(posted.captured_amount, '47.25')
        hotel, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='200.00', merchant='HILTON', pan=VISA, mcc='7011', stan='000708',
        )
        over = self.service.capture(
            hold_id=hotel.hold_id, actor='teller', actor_type='tier1', amount='230.00',
        )
        self.assertEqual(over.captured_amount, '230.00')
        retail, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='20.00', merchant='STORE', pan=VISA, mcc='5411', stan='000709',
        )
        with self.assertRaises(CardHoldError) as ctx:
            self.service.capture(
                hold_id=retail.hold_id, actor='teller', actor_type='tier1', amount='21.00',
            )
        self.assertEqual(ctx.exception.code, 'amount_out_of_range')

    def test_increment_void_expire_and_credit_block(self):
        self._enroll()
        hold, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='80.00', merchant='HOTEL DESK', pan=VISA, mcc='7011', stan='000808',
        )
        bumped = self.service.increment(
            hold_id=hold.hold_id, actor='teller', actor_type='tier1', amount='120.00',
        )
        self.assertEqual(bumped.amount, '120.00')
        self.assertEqual(bumped.status, 'authorized')
        voided = self.service.void_hold(
            hold_id=hold.hold_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(voided.status, 'voided')
        self.assertEqual(self.debits, [])
        late, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='12.00', merchant='SHOP', pan=VISA, stan='000809',
        )
        self.now[0] = late.expires_at + 1
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'expired')
        with self.assertRaises(CardHoldError) as ctx:
            self.service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Credit Card', pan=MC, default_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_pause_cancel_and_customer_void_window(self):
        key = self._enroll()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        hold, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            amount='9.00', merchant='CAFE', pan=VISA, stan='000901',
        )
        self.assertEqual(hold.status, 'unmatched')
        self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='resume',
        )
        rematched = self.service.store.get_hold(hold.hold_id)
        self.assertEqual(rematched.status, 'authorized')
        self.now[0] = rematched.created_at + 25 * 3600
        with self.assertRaises(CardHoldError) as ctx:
            self.service.void_hold(
                hold_id=rematched.hold_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'void_window_closed')
        cancelled = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='cancel',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        with self.assertRaises(CardHoldError) as again:
            self.service.set_key_status(
                key_id=key.key_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(again.exception.code, 'already_cancelled')

    def test_sqlite_store_round_trip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteCardHoldStore(path)
            service = CardHoldService(
                CardHoldPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': 'None',
                    'credit': 'None',
                },
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Daily', pan=VISA, default_account='1001',
            )
            hold, _ = service.ingest(
                actor='teller', actor_type='tier1',
                amount='11.00', merchant='DELI', pan=VISA, stan='001001',
            )
            again = SqliteCardHoldStore(path)
            loaded = again.get_key(key.key_id)
            self.assertEqual(loaded.last4, '1111')
            self.assertEqual(again.get_hold_by_stan('001001').status, 'authorized')
            self.assertEqual(hold.status, 'authorized')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
