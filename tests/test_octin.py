import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.octin import (
    CardNetworkClock,
    MemoryOctinStore,
    OctinError,
    OctinPolicy,
    OctinService,
    SqliteOctinStore,
    brand_from_iin,
    compose_oct1,
    compose_rrn,
    compose_stan,
    luhn_check_digit_ok,
    mask_pan,
    mask_rrn,
    normalize_mti,
    normalize_pan,
    pan_fingerprint,
    parse_oct1,
    split_oct1_file,
)
from utility.wire import parse_money

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
MC = '5555555555554444'
AMEX = '378282246310005'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def accounts(_userid='alice'):
    return {
        'checkin': {'Account': 1001, 'Balance': 50},
        'savings': {'Account': 1002, 'Balance': 10},
        'credit': 'None',
    }


class FoundationTests(unittest.TestCase):
    def test_luhn_and_brand_and_fingerprint(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(brand_from_iin(MC), 'mastercard')
        self.assertNotEqual(pan_fingerprint(VISA), VISA)
        self.assertEqual(len(pan_fingerprint(VISA)), 64)
        with self.assertRaises(OctinError) as ctx:
            brand_from_iin(AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')
        with self.assertRaises(OctinError):
            normalize_pan('4111111111111112')

    def test_mti_rrn_stan_reject_auth_presentment(self):
        self.assertEqual(normalize_mti('260'), '0260')
        self.assertEqual(normalize_mti('aft'), '0200')
        self.assertEqual(normalize_mti('reversal'), '0420')
        with self.assertRaises(OctinError) as ctx:
            normalize_mti('1100')
        self.assertEqual(ctx.exception.code, 'invalid_mti')
        with self.assertRaises(OctinError):
            normalize_mti('1240')
        self.assertEqual(compose_rrn('20240615', 7), '240615000007')
        self.assertEqual(compose_stan(12), '000012')
        self.assertEqual(mask_rrn('240615000007'), '****0007')

    def test_oct1_parse_rejects_xml(self):
        line = compose_oct1(
            mti='0260', pan=VISA, expiry='1227', amount='25.00', mcc='6538',
            processing_code='260000', rrn='240615000001', stan='000001',
            sender='Ada', rail='oct',
        )
        parsed = parse_oct1(line)
        self.assertEqual(parsed['pan'], VISA)
        with self.assertRaises(OctinError) as ctx:
            parse_oct1('<?xml version="1.0"?><OCT/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(OctinError):
            split_oct1_file('<!DOCTYPE html>')
        with self.assertRaises(OctinError):
            parse_oct1('OCT1|0260|****1111|1227|1.00|6538|260000|240615000001|000001|Ada|oct|posted')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 15, 11, 0)]  # Saturday 11:00 ET — Fedwire would be closed
        self.credits = []
        self.debits = []

        def credit_fn(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Success'

        def debit_fn(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Amount Debited'

        self.service = OctinService(
            OctinPolicy(),
            MemoryOctinStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts,
            calendar=CardNetworkClock(tz_offset_hours=-4),
        )

    def _enroll(self, **overrides):
        body = dict(
            owner_userid='alice', actor='alice', actor_type='customer',
            nickname='Travel Visa', legal_name='Ada Lovelace',
            pan=VISA, expiry='1227', default_account='1001',
        )
        body.update(overrides)
        return self.service.add_key(**body)

    def test_weekend_posts_instantly_not_fed_cutoff(self):
        clock = CardNetworkClock(tz_offset_hours=-4)
        snap = clock.snapshot(self.now[0])
        self.assertTrue(snap['network_open'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '24/7')
        self.assertEqual(snap['weekday'], 'Saturday')
        self._enroll()
        row, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '40.00', 'sender_name': 'Payroll Co', 'mti': '0260'},
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'posted')
        self.assertEqual(len(self.credits), 1)
        self.assertIn('oct from', self.credits[0][2])

    def test_snapshot_and_to_dict_never_leak_pan(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '12.00', 'sender_name': 'Acme', 'rrn': '240615000099'},
        )
        snap = self.service.snapshot('alice')
        blob = str(snap)
        self.assertNotIn(VISA, blob)
        self.assertNotIn(pan_fingerprint(VISA), blob)
        self.assertNotIn('240615000099', blob)
        self.assertEqual(snap['keys'][0]['last4'], '1111')
        self.assertEqual(row.to_dict()['pan_masked'], mask_pan(VISA))
        self.assertNotIn('pan', row.to_dict())
        self.assertNotIn('fingerprint', row.to_dict())

    def test_duplicate_rrn_is_idempotent(self):
        self._enroll()
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '10.00', 'sender_name': 'Acme', 'rrn': '240615000010'},
        )
        again, created2 = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '99.00', 'sender_name': 'Acme', 'rrn': '240615000010'},
        )
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(first.inbound_id, again.inbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_ofac_hold_blocks_credit_until_staff_override(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '20.00', 'sender_name': 'Blocked Person'},
        )
        self.assertEqual(row.status, 'held')
        self.assertEqual(self.credits, [])
        with self.assertRaises(OctinError) as ctx:
            self.service.override_ofac(inbound_id=row.inbound_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'octin_forbidden')
        posted = self.service.override_ofac(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_dual_control_requires_a_different_employee(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '2500.00', 'sender_name': 'Acme'},
        )
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(OctinError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.credits), 1)

    def test_aft_cap_and_oct_cap(self):
        self._enroll()
        with self.assertRaises(OctinError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1',
                values={'pan': VISA, 'amount': '5000.01', 'sender_name': 'Wallet', 'rail': 'aft', 'mti': '0200'},
            )
        self.assertEqual(ctx.exception.code, 'aft_amount_exceeded')
        with self.assertRaises(OctinError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1',
                values={'pan': VISA, 'amount': '10000.01', 'sender_name': 'Push', 'mti': '0260'},
            )
        self.assertEqual(ctx.exception.code, 'oct_amount_exceeded')
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '5000.00', 'sender_name': 'Wallet', 'rail': 'aft', 'mti': '0200'},
        )
        self.assertEqual(row.status, 'pending_release')
        self.assertEqual(row.rail, 'aft')
        self.assertEqual(row.mti, '0200')
        self.assertEqual(row.processing_code, '100000')

    def test_credit_account_and_pause(self):
        def credit_accounts(_userid='alice'):
            return {
                'checkin': {'Account': 1001, 'Balance': 50},
                'savings': {'Account': 1002, 'Balance': 10},
                'credit': {'Account': 9001, 'Balance': 0},
            }

        self.service.accounts_fn = credit_accounts
        with self.assertRaises(OctinError) as ctx:
            self._enroll(default_account='9001')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        key = self._enroll()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='paused',
        )
        self.assertEqual(paused.status, 'paused')
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '15.00', 'sender_name': 'Acme'},
        )
        self.assertEqual(row.status, 'unmatched')
        self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='resume',
        )
        rematched = self.service.store.get_inbound(row.inbound_id)
        self.assertEqual(rematched.status, 'posted')

    def test_unmatched_claim_assign_and_export_masks(self):
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '18.00', 'sender_name': 'Acme', 'expiry': '1227'},
        )
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(self.credits, [])
        claimed = self.service.claim(
            actor='alice', actor_type='customer', pan=VISA, destination_account='1001',
            nickname='Claimed Visa',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(claimed.userid, 'alice')
        self.assertEqual(len(self.credits), 1)
        exported = self.service.export_oct1(inbound_id=claimed.inbound_id, actor_type='tier1')
        self.assertTrue(exported.startswith('OCT1|'))
        self.assertNotIn(VISA, exported)
        self.assertNotIn(claimed.rrn, exported)

    def test_last4_claim_and_staff_assign(self):
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': MC, 'amount': '22.00', 'sender_name': 'Acme', 'expiry': '1227'},
        )
        with self.assertRaises(OctinError):
            self.service.claim(actor='alice', actor_type='customer', last4_value='4444', brand='visa')
        claimed = self.service.claim(
            actor='alice', actor_type='customer', last4_value='4444', brand='mastercard',
            destination_account='1002',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(claimed.destination_account, '1002')
        other, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '9.00', 'sender_name': 'Acme', 'expiry': '1227'},
        )
        assigned = self.service.assign(
            inbound_id=other.inbound_id, actor='boss', actor_type='tier2',
            customer_id='alice', account='1001', nickname='Assigned Visa',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.userid, 'alice')

    def test_customer_return_window_and_staff_return(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '30.00', 'sender_name': 'Acme'},
        )
        returned = self.service.return_inbound(
            inbound_id=row.inbound_id, actor='alice', actor_type='customer', staff=False,
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(len(self.debits), 1)
        self.assertIn('oct return', self.debits[0][2])
        later, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '11.00', 'sender_name': 'Acme', 'rrn': '240615000088'},
        )
        self.now[0] = self.now[0] + 25 * 3600
        with self.assertRaises(OctinError) as ctx:
            self.service.return_inbound(
                inbound_id=later.inbound_id, actor='alice', actor_type='customer', staff=False,
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staffed = self.service.return_inbound(
            inbound_id=later.inbound_id, actor='boss', actor_type='tier2', staff=True,
        )
        self.assertEqual(staffed.status, 'returned')

    def test_network_reversal_mti_and_expire_unmatched(self):
        self._enroll()
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '14.00', 'sender_name': 'Acme', 'rrn': '240615000077'},
        )
        reversed_row, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'mti': '0420', 'rrn': '240615000077'},
        )
        self.assertFalse(created)
        self.assertEqual(reversed_row.status, 'returned')
        unmatched, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': MC, 'amount': '8.00', 'sender_name': 'Acme'},
        )
        self.assertEqual(unmatched.status, 'unmatched')
        self.now[0] = self.now[0] + 25 * 3600
        counts = self.service.run_due(actor='teller', actor_type='tier1')
        self.assertEqual(counts['expired'], 1)
        self.assertEqual(self.service.store.get_inbound(unmatched.inbound_id).status, 'expired')

    def test_customer_cannot_ingest_and_reject_open(self):
        self._enroll()
        with self.assertRaises(OctinError) as ctx:
            self.service.ingest(
                actor='alice', actor_type='customer',
                values={'pan': VISA, 'amount': '10.00', 'sender_name': 'Acme'},
            )
        self.assertEqual(ctx.exception.code, 'octin_forbidden')
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values={'pan': VISA, 'amount': '2500.00', 'sender_name': 'Acme'},
        )
        rejected = self.service.reject(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(rejected.status, 'rejected')
        self.assertEqual(self.credits, [])

    def test_sqlite_roundtrip_masks_pan(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteOctinStore(path)
            service = OctinService(
                OctinPolicy(),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda *a: 'Amount Debited',
                credit_fn=lambda *a: 'Success',
                accounts_fn=accounts,
            )
            service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Debit', legal_name='Ada Lovelace',
                pan=VISA, expiry='1227', default_account='1001',
            )
            row, _ = service.ingest(
                actor='teller', actor_type='tier1',
                values={'pan': VISA, 'amount': '7.00', 'sender_name': 'Acme'},
            )
            loaded = SqliteOctinStore(path).get_inbound(row.inbound_id)
            self.assertEqual(loaded.pan, VISA)
            self.assertNotIn(VISA, str(loaded.to_dict()))
            self.assertEqual(loaded.status, 'posted')
        finally:
            os.remove(path)

    def test_duplicate_key_and_parse_money(self):
        self._enroll()
        with self.assertRaises(OctinError) as ctx:
            self._enroll(nickname='Other')
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        self.assertEqual(parse_money('12.10'), Decimal('12.10'))


if __name__ == '__main__':
    unittest.main()
