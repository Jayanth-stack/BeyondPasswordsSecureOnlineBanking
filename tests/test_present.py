import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.present import (
    AmountError,
    MemoryPresentStore,
    PresentError,
    PresentPolicy,
    PresentService,
    SettlementClock,
    SqlitePresentStore,
    brand_from_iin,
    compose_arn,
    compose_ipm1,
    compute_interchange,
    luhn_check_digit_ok,
    mask_arn,
    mask_pan,
    normalize_arn,
    normalize_function,
    normalize_mti,
    normalize_pan,
    parse_ipm1,
    pan_fingerprint,
    split_ipm1_file,
)
from utility.wire import us_fed_holidays

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_luhn_and_brand(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(mask_pan(VISA), '************1111')
        self.assertEqual(len(pan_fingerprint(VISA)), 64)
        with self.assertRaises(PresentError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        self.assertEqual(brand_from_iin('5555555555554444'), 'mastercard')
        self.assertEqual(brand_from_iin('378282246310005'), 'amex')
        self.assertEqual(brand_from_iin('6011111111111117'), 'discover')

    def test_arn_layout(self):
        self.assertEqual(compose_arn('20240614', 1), '40000020240614000000001')
        self.assertEqual(normalize_arn('40000020240614000000001'), '40000020240614000000001')
        self.assertEqual(mask_arn('40000020240614000000001'), '*******************0001')
        with self.assertRaises(PresentError) as ctx:
            compose_arn('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_arn')
        with self.assertRaises(PresentError):
            normalize_arn('0' * 23)

    def test_mti_and_function(self):
        self.assertEqual(normalize_mti('0220'), '1240')
        self.assertEqual(normalize_mti('1221'), '1221')
        self.assertEqual(normalize_mti('0442'), '1442')
        self.assertEqual(normalize_function('first'), '200')
        self.assertEqual(normalize_function('205'), '205')
        with self.assertRaises(PresentError) as ctx:
            normalize_mti('1100')
        self.assertEqual(ctx.exception.code, 'invalid_mti')

    def test_interchange_by_mcc(self):
        retail = compute_interchange(Decimal('100.00'), '5999')
        self.assertEqual(retail, Decimal('1.75'))
        fuel = compute_interchange(Decimal('40.00'), '5541')
        self.assertEqual(fuel, Decimal('0.71'))
        grocery = compute_interchange(Decimal('80.00'), '5411')
        self.assertEqual(grocery, Decimal('0.97'))

    def test_ipm1_roundtrip_rejects_xml(self):
        line = compose_ipm1(
            arn='40000020240614000000001',
            mti='1240',
            function_code='200',
            pan_masked='************1111',
            amount='25.00',
            merchant='ACME MART',
            mcc='5999',
            interchange='0.51',
        )
        parsed = parse_ipm1(line)
        self.assertEqual(parsed['mti'], '1240')
        self.assertTrue(parsed['arn'].endswith('0001') or parsed['arn'].startswith('*'))
        with self.assertRaises(PresentError) as ctx:
            parse_ipm1('<?xml version="1.0"?><ipm/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(PresentError):
            split_ipm1_file('<!DOCTYPE html>')

    def test_settlement_cutoff_weekend_july4(self):
        calendar = SettlementClock(cutoff_hour=22, tz_offset_hours=-4)
        friday = ts(2024, 6, 14, 16, 0)
        friday_late = ts(2024, 6, 14, 22, 30)
        self.assertEqual(calendar.value_date(friday).isoformat(), '2024-06-14')
        self.assertEqual(calendar.value_date(friday_late).isoformat(), '2024-06-17')
        self.assertTrue(calendar.should_queue(friday_late))
        july4 = ts(2024, 7, 4, 10, 0)
        self.assertIn(datetime(2024, 7, 4).date(), us_fed_holidays(2024))
        self.assertEqual(calendar.value_date(july4).isoformat(), '2024-07-05')
        saturday = ts(2024, 6, 15, 11, 0)
        self.assertEqual(calendar.value_date(saturday).isoformat(), '2024-06-17')


class PresentServiceTests(unittest.TestCase):
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

        policy = PresentPolicy(dual_control_threshold=Decimal('10000.00'))
        self.service = PresentService(
            policy,
            MemoryPresentStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=SettlementClock(cutoff_hour=22, tz_offset_hours=-4),
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
        with self.assertRaises(PresentError) as ctx:
            self._enroll(nickname='Other')
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        with self.assertRaises(PresentError) as ctx:
            self._enroll(nickname='Backup', pan='4242424242424242', default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')

    def test_matched_first_presentment_debits_and_hides_secrets(self):
        self._enroll()
        row, created = self.service.ingest(
            actor='teller',
            actor_type='tier1',
            amount='25.00',
            merchant='ACME MART',
            pan=VISA,
            mcc='5999',
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(row.interchange, '0.51')
        self.assertEqual(self.debits, [('1001', '25.00', 'card present ACME MART')])
        body = row.to_dict()
        self.assertNotIn(VISA, str(body))
        self.assertNotIn(row.arn, str(body.values()))
        self.assertTrue(body['arn_masked'].startswith('*'))
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['ytd_posted'], '25.00')
        self.assertNotIn(VISA, str(snap))
        self.assertNotIn(pan_fingerprint(VISA), str(snap))

    def test_duplicate_arn_is_idempotent(self):
        self._enroll()
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1', amount='10.00', merchant='Cafe',
            pan=VISA, arn='40000020240614000000007',
        )
        self.assertTrue(created)
        again, created_again = self.service.ingest(
            actor='teller', actor_type='tier1', amount='10.00', merchant='Cafe',
            pan=VISA, arn='40000020240614000000007',
        )
        self.assertFalse(created_again)
        self.assertEqual(again.presentment_id, first.presentment_id)
        self.assertEqual(len(self.debits), 1)

    def test_unmatched_then_claim(self):
        row, created = self.service.ingest(
            actor='teller', actor_type='tier1', amount='12.00', merchant='Unknown',
            pan=VISA,
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(self.debits, [])
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            presentment_id=row.presentment_id, pan=VISA, default_account='1001',
            nickname='Travel Visa',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(self.debits[-1][2], 'card present Unknown')

    def test_ofac_hold_and_override(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='20.00',
            merchant='Blocked Person LLC', pan=VISA,
        )
        self.assertEqual(row.status, 'held')
        self.assertEqual(self.debits, [])
        with self.assertRaises(PresentError) as ctx:
            self.service.release(presentment_id=row.presentment_id, actor='boss', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        cleared = self.service.override_ofac(
            presentment_id=row.presentment_id, actor='boss', actor_type='tier2',
        )
        self.assertEqual(cleared.status, 'posted')

    def test_dual_control_same_approver(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='15000.00',
            merchant='Dealer', pan=VISA,
        )
        self.assertEqual(row.status, 'pending_release')
        with self.assertRaises(PresentError) as ctx:
            self.service.release(presentment_id=row.presentment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(
            presentment_id=row.presentment_id, actor='boss', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')

    def test_weekend_queues_then_run_due_posts(self):
        self._enroll()
        self.now[0] = ts(2024, 6, 15, 11, 0)
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='8.00', merchant='Saturday',
            pan=VISA,
        )
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        changed = self.service.run_due('alice')
        self.assertEqual(changed[0].status, 'posted')
        self.assertEqual(self.debits[-1][0], '1001')

    def test_reverse_and_second_presentment(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='30.00', merchant='Shop',
            pan=VISA, arn='40000020240614000000011',
        )
        reversed_row = self.service.reverse(
            presentment_id=row.presentment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.credits[-1][2], 'card reverse %s' % '40000020240614000000011'[-12:])
        second, created = self.service.ingest(
            actor='teller', actor_type='tier1', amount='30.00', merchant='Shop',
            pan=VISA, arn='40000020240614000000011', function_code='205',
        )
        self.assertTrue(created)
        self.assertEqual(second.function_code, '205')
        self.assertEqual(second.status, 'posted')
        with self.assertRaises(PresentError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1', amount='9.00', merchant='Other',
                pan=VISA, arn='40000020240614000000099', function_code='205',
            )
        self.assertEqual(ctx.exception.code, 'presentment_not_found')

    def test_second_presentment_blocked_while_first_posted(self):
        self._enroll()
        self.service.ingest(
            actor='teller', actor_type='tier1', amount='15.00', merchant='Shop',
            pan=VISA, arn='40000020240614000000021',
        )
        with self.assertRaises(PresentError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1', amount='15.00', merchant='Shop',
                pan=VISA, arn='40000020240614000000021', function_code='205',
            )
        self.assertEqual(ctx.exception.code, 'not_representable')

    def test_customer_reverse_window(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='18.00', merchant='Shop',
            pan=VISA,
        )
        self.now[0] = ts(2024, 6, 16, 17, 0)
        with self.assertRaises(PresentError) as ctx:
            self.service.reverse(
                presentment_id=row.presentment_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.reverse(
            presentment_id=row.presentment_id, actor='boss', actor_type='tier2',
        )
        self.assertEqual(staff.status, 'reversed')

    def test_nsf_does_not_post(self):
        self._enroll()

        def nsf(account, amount, remark):
            return 'Insufficient funds'

        self.service.debit_fn = nsf
        with self.assertRaises(PresentError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1', amount='40.00', merchant='Shop',
                pan=VISA,
            )
        self.assertEqual(ctx.exception.code, 'nsf')

    def test_unmatched_expires(self):
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1', amount='11.00', merchant='Ghost',
            pan=VISA,
        )
        self.assertEqual(row.status, 'unmatched')
        self.now[0] = ts(2024, 6, 15, 17, 0)
        changed = self.service.run_due()
        self.assertEqual(changed[0].status, 'expired')

    def test_sqlite_survives_reload(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqlitePresentStore(path)
            service = PresentService(
                PresentPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda a, b, c: 'Amount Debited',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                calendar=SettlementClock(cutoff_hour=22, tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Travel Visa', pan=VISA, default_account='1001',
            )
            reloaded = SqlitePresentStore(path).get_key(key.key_id)
            self.assertEqual(reloaded.nickname, 'Travel Visa')
            self.assertEqual(reloaded.fingerprint, pan_fingerprint(VISA))
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
