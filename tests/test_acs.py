import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.acs import (
    AcsClock,
    AcsError,
    AcsPolicy,
    AcsService,
    MemoryAcsStore,
    SqliteAcsStore,
    brand_from_iin,
    compose_3ds1,
    compose_acs_code,
    compose_cavv,
    eci_for,
    hash_acs_code,
    luhn_check_digit_ok,
    mask_cavv,
    mask_pan,
    mask_uuid,
    normalize_channel,
    normalize_message_type,
    normalize_pan,
    normalize_protocol_version,
    normalize_uuid,
    pan_fingerprint,
    parse_3ds1,
    split_3ds1_file,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
PINNED_DS = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_luhn_and_brand(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(mask_pan(VISA), '************1111')
        self.assertEqual(len(pan_fingerprint(VISA)), 64)
        with self.assertRaises(AcsError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        self.assertEqual(brand_from_iin('5555555555554444'), 'mastercard')
        self.assertEqual(brand_from_iin('378282246310005'), 'amex')
        self.assertEqual(brand_from_iin('6011111111111117'), 'discover')

    def test_protocol_and_channel(self):
        self.assertEqual(normalize_protocol_version('2.1'), '2.1.0')
        self.assertEqual(normalize_protocol_version('220'), '2.2.0')
        self.assertEqual(normalize_message_type('areq'), 'AReq')
        self.assertEqual(normalize_message_type('CReq'), 'CReq')
        self.assertEqual(normalize_channel('browser'), 'brw')
        self.assertEqual(normalize_channel('01'), 'app')
        self.assertEqual(normalize_channel('3RI'), '3ri')
        with self.assertRaises(AcsError) as ctx:
            normalize_message_type('1100')
        self.assertEqual(ctx.exception.code, 'invalid_message_type')
        with self.assertRaises(AcsError):
            normalize_protocol_version('1.0.2')

    def test_eci_and_cavv(self):
        self.assertEqual(eci_for('visa', 'Y'), '05')
        self.assertEqual(eci_for('visa', 'A'), '06')
        self.assertEqual(eci_for('visa', 'N'), '07')
        self.assertEqual(eci_for('mastercard', 'Y'), '02')
        self.assertEqual(eci_for('mastercard', 'A'), '01')
        self.assertEqual(eci_for('mastercard', 'N'), '00')
        cavv = compose_cavv(brand='visa', acs_trans_id='abc', amount='10.00', at=1)
        self.assertEqual(len(cavv), 28)
        self.assertTrue(mask_cavv(cavv).startswith('*'))
        self.assertEqual(len(mask_uuid(PINNED_DS)), 36)
        self.assertEqual(normalize_uuid(PINNED_DS.replace('-', '')), PINNED_DS)

    def test_3ds1_roundtrip_rejects_xml(self):
        line = compose_3ds1(
            ds_trans_id=PINNED_DS,
            message_type='AReq',
            version='2.2.0',
            channel='brw',
            category='pa',
            pan_masked='************1111',
            amount='25.00',
            merchant='ACME MART',
            mcc='5999',
            trans_status='Y',
            eci='05',
        )
        parsed = parse_3ds1(line)
        self.assertEqual(parsed['message_type'], 'AReq')
        self.assertTrue(parsed['ds_trans_id'].startswith('*') or parsed['ds_trans_id'].endswith('eeee'))
        with self.assertRaises(AcsError) as ctx:
            parse_3ds1('<?xml version="1.0"?><threeDS/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(AcsError):
            split_3ds1_file('<!DOCTYPE html>')

    def test_acs_clock_is_24_7_and_challenge_expires(self):
        calendar = AcsClock(challenge_seconds=600, tz_offset_hours=-4)
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertFalse(calendar.should_queue(saturday))
        started = ts(2024, 6, 14, 16, 0)
        still_open = ts(2024, 6, 14, 16, 9)
        expired = ts(2024, 6, 14, 16, 11)
        self.assertFalse(calendar.challenge_expired(started, still_open))
        self.assertTrue(calendar.challenge_expired(started, expired))
        self.assertEqual(calendar.remaining_seconds(started, still_open), 60)


class AcsServiceTests(unittest.TestCase):
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

        policy = AcsPolicy(
            dual_control_threshold=Decimal('10000.00'),
            frictionless_cap=Decimal('250.00'),
        )
        self.service = AcsService(
            policy,
            MemoryAcsStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=AcsClock(challenge_seconds=600, tz_offset_hours=-4),
        )

    def _enroll(self, **kwargs):
        payload = {
            'owner_userid': 'alice',
            'actor': 'alice',
            'actor_type': 'customer',
            'nickname': 'Travel Visa',
            'pan': VISA,
            'default_account': '1001',
        }
        payload.update(kwargs)
        return self.service.add_key(**payload)

    def test_enroll_rejects_credit_and_duplicate(self):
        self._enroll()
        with self.assertRaises(AcsError) as ctx:
            self._enroll(nickname='Other')
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        with self.assertRaises(AcsError) as ctx:
            self._enroll(nickname='Backup', pan='4242424242424242', default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_frictionless_issues_cavv_without_debit(self):
        self._enroll()
        row, created = self.service.ingest(
            actor='teller',
            actor_type='tier1',
            amount='25.00',
            merchant='ACME MART',
            pan=VISA,
            mcc='5999',
            channel='app',
            method_completed=True,
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'authenticated')
        self.assertEqual(row.trans_status, 'Y')
        self.assertEqual(row.eci, '05')
        self.assertEqual(len(row.cavv), 28)
        self.assertEqual(self.debits, [])
        body = row.to_dict()
        self.assertNotIn(VISA, str(body))
        self.assertNotIn(row.cavv, str(body.values()))
        self.assertNotIn(row.ds_trans_id, str(body.values()))
        self.assertTrue(body['cavv_masked'].startswith('*'))
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_authenticated'], '25.00')
        self.assertNotIn(VISA, str(snap))
        self.assertNotIn(pan_fingerprint(VISA), str(snap))
        self.assertNotIn(row.cavv, str(snap))

    def test_browser_without_method_challenges(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00', merchant='Shop',
            pan=VISA, channel='brw', method_completed=False, acs_code='123456',
        )
        self.assertEqual(row.status, 'challenge')
        self.assertEqual(row.trans_status, 'C')
        self.assertEqual(row.cavv, '')
        self.assertEqual(row.acs_code_hash, hash_acs_code('123456'))
        completed = self.service.respond(
            auth_id=row.auth_id, actor='alice', actor_type='customer', acs_code='123456',
        )
        self.assertEqual(completed.status, 'authenticated')
        self.assertEqual(completed.trans_status, 'Y')
        self.assertEqual(len(completed.cavv), 28)
        self.assertEqual(completed.acs_code_hash, '')
        self.assertEqual(self.debits, [])

    def test_three_ri_above_cap_is_attempt_not_challenge(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='400.00', merchant='Recurring',
            pan=VISA, channel='3ri',
        )
        self.assertEqual(row.status, 'attempted')
        self.assertEqual(row.trans_status, 'A')
        self.assertEqual(row.eci, '06')
        self.assertEqual(len(row.cavv), 28)

    def test_duplicate_ds_trans_id_is_idempotent(self):
        self._enroll()
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1', amount='10.00', merchant='Cafe',
            pan=VISA, channel='app', ds_trans_id=PINNED_DS,
        )
        self.assertTrue(created)
        again, created_again = self.service.ingest(
            actor='teller', actor_type='tier1', amount='10.00', merchant='Cafe',
            pan=VISA, channel='app', ds_trans_id=PINNED_DS,
        )
        self.assertFalse(created_again)
        self.assertEqual(again.auth_id, first.auth_id)

    def test_unmatched_then_claim(self):
        row, created = self.service.ingest(
            actor='teller', actor_type='tier1', amount='12.00', merchant='Unknown',
            pan=VISA, channel='app',
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'unmatched')
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            auth_id=row.auth_id, pan=VISA, default_account='1001',
            nickname='Travel Visa',
        )
        self.assertEqual(claimed.status, 'authenticated')
        self.assertEqual(claimed.trans_status, 'Y')

    def test_ofac_hold_and_override(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00',
            merchant='Blocked Person LLC', pan=VISA, channel='app',
        )
        self.assertEqual(row.status, 'held')
        with self.assertRaises(AcsError) as ctx:
            self.service.release(auth_id=row.auth_id, actor='boss', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        cleared = self.service.override_ofac(auth_id=row.auth_id, actor='boss', actor_type='tier2')
        self.assertEqual(cleared.status, 'authenticated')

    def test_dual_control_same_approver(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='15000.00',
            merchant='Dealer', pan=VISA, channel='app',
        )
        self.assertEqual(row.status, 'pending_release')
        with self.assertRaises(AcsError) as ctx:
            self.service.release(auth_id=row.auth_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(auth_id=row.auth_id, actor='boss', actor_type='tier2')
        self.assertEqual(released.status, 'challenge')
        self.assertEqual(released.trans_status, 'C')

    def test_weekend_does_not_queue(self):
        self._enroll()
        self.now[0] = ts(2024, 6, 15, 11, 0)
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='8.00', merchant='Saturday',
            pan=VISA, channel='app',
        )
        self.assertEqual(row.status, 'authenticated')
        self.assertEqual(row.trans_status, 'Y')

    def test_challenge_expiry_and_bad_code(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00', merchant='Shop',
            pan=VISA, channel='brw', acs_code='123456',
        )
        self.assertEqual(row.status, 'challenge')
        with self.assertRaises(AcsError) as ctx:
            self.service.respond(
                auth_id=row.auth_id, actor='alice', actor_type='customer', acs_code='000001',
            )
        self.assertEqual(ctx.exception.code, 'invalid_code')
        self.now[0] = ts(2024, 6, 14, 16, 11)
        with self.assertRaises(AcsError) as ctx:
            self.service.respond(
                auth_id=row.auth_id, actor='alice', actor_type='customer', acs_code='123456',
            )
        self.assertEqual(ctx.exception.code, 'challenge_expired')
        expired = self.service.store.get_auth(row.auth_id)
        self.assertEqual(expired.status, 'expired')
        self.assertEqual(expired.trans_status, 'U')

    def test_run_due_expires_challenge_and_unmatched(self):
        self._enroll()
        challenged, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00', merchant='Shop',
            pan=VISA, channel='brw', acs_code='123456',
        )
        ghost, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='11.00', merchant='Ghost',
            pan='4242424242424242', channel='app',
        )
        self.assertEqual(ghost.status, 'unmatched')
        self.now[0] = ts(2024, 6, 15, 17, 0)
        changed = {row.auth_id: row.status for row in self.service.run_due()}
        self.assertEqual(changed[challenged.auth_id], 'expired')
        self.assertEqual(changed[ghost.auth_id], 'expired')

    def test_reverse_windows(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='18.00', merchant='Shop',
            pan=VISA, channel='app',
        )
        reversed_row = self.service.reverse(
            auth_id=row.auth_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(reversed_row.cavv, '')
        self.assertEqual(reversed_row.trans_status, 'N')
        later, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='19.00', merchant='Shop2',
            pan=VISA, channel='app',
        )
        self.now[0] = ts(2024, 6, 16, 17, 0)
        with self.assertRaises(AcsError) as ctx:
            self.service.reverse(auth_id=later.auth_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.reverse(auth_id=later.auth_id, actor='boss', actor_type='tier2')
        self.assertEqual(staff.status, 'reversed')

    def test_cancel_challenge(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00', merchant='Shop',
            pan=VISA, channel='brw', acs_code='123456',
        )
        cancelled = self.service.cancel(
            auth_id=row.auth_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'rejected')
        self.assertEqual(cancelled.trans_status, 'N')
        self.assertEqual(cancelled.cavv, '')

    def test_sqlite_survives_reload(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteAcsStore(path)
            service = AcsService(
                AcsPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=AcsClock(challenge_seconds=600, tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Travel Visa', pan=VISA, default_account='1001',
            )
            auth, _ = service.ingest(
                actor='teller', actor_type='tier1', amount='9.00', merchant='Cafe',
                pan=VISA, channel='app',
            )
            reloaded_store = SqliteAcsStore(path)
            reloaded = reloaded_store.get_key(key.key_id)
            self.assertEqual(reloaded.nickname, 'Travel Visa')
            self.assertEqual(reloaded.fingerprint, pan_fingerprint(VISA))
            reloaded_auth = reloaded_store.get_auth(auth.auth_id)
            self.assertEqual(reloaded_auth.status, 'authenticated')
            self.assertEqual(len(reloaded_auth.cavv), 28)
        finally:
            os.unlink(path)

    def test_compose_acs_code_never_zero(self):
        self.assertEqual(len(compose_acs_code('seed')), 6)
        self.assertNotEqual(compose_acs_code('seed'), '000000')


if __name__ == '__main__':
    unittest.main()
