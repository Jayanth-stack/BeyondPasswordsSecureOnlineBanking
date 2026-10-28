import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.vau import (
    MemoryVauStore,
    SqliteVauStore,
    UpdaterClock,
    VauError,
    VauPolicy,
    VauService,
    brand_from_iin,
    compose_vau1,
    compose_vau_ref,
    compute_fee,
    luhn_check_digit_ok,
    mask_pan,
    mask_vau_ref,
    network_for_brand,
    normalize_expiry,
    normalize_pan,
    normalize_reason,
    pan_fingerprint,
    parse_vau1,
    split_vau1_file,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
VISA_NEW = '4012888888881881'
MC = '5555555555554444'
AMEX = '378282246310005'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_luhn_and_brand_and_fingerprint(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertTrue(luhn_check_digit_ok(MC))
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(brand_from_iin(MC), 'mastercard')
        self.assertEqual(network_for_brand('visa'), 'visa_vau')
        self.assertEqual(network_for_brand('mastercard'), 'mastercard_abu')
        with self.assertRaises(VauError) as ctx:
            brand_from_iin(AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')
        with self.assertRaises(VauError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        masked = mask_pan(VISA)
        self.assertNotIn(VISA, masked)
        self.assertTrue(masked.endswith('1111'))
        self.assertNotEqual(pan_fingerprint(VISA), pan_fingerprint(VISA_NEW))
        self.assertEqual(len(pan_fingerprint(VISA)), 64)

    def test_expiry_reason_and_vau_ref(self):
        self.assertEqual(normalize_expiry('12/27'), '1227')
        self.assertEqual(normalize_expiry('122027'), '1227')
        with self.assertRaises(VauError) as ctx:
            normalize_expiry('13/27')
        self.assertEqual(ctx.exception.code, 'invalid_expiry')
        self.assertEqual(normalize_reason('reissue'), 'A')
        self.assertEqual(normalize_reason('expiry'), 'E')
        with self.assertRaises(VauError) as ctx:
            normalize_reason('P')
        self.assertEqual(ctx.exception.code, 'invalid_reason')
        self.assertEqual(normalize_reason('inquiry', allow_participating=True), 'P')
        self.assertEqual(compose_vau_ref('20240614', 1), 'VAU20240614000001')
        self.assertEqual(mask_vau_ref('VAU20240614000001'), 'VAU****0001')
        with self.assertRaises(VauError) as ctx:
            compose_vau_ref('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        self.assertEqual(compute_fee(Decimal('0.10')), Decimal('0.10'))
        self.assertEqual(compute_fee(Decimal('0.10'), waived=True), Decimal('0.00'))

    def test_vau1_parse_rejects_xml(self):
        line = compose_vau1(
            reason='P', pan=VISA, expiry='1227', brand='visa', network='visa_vau',
            bid='123456', merchant='Acme', reference='inq-1', status='posted',
        )
        parsed = parse_vau1(line)
        self.assertEqual(parsed['pan'], VISA)
        with self.assertRaises(VauError) as ctx:
            parse_vau1('<?xml version="1.0"?><vau/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(VauError):
            split_vau1_file('<!DOCTYPE html>')
        lines = split_vau1_file(line + '\n\n' + line)
        self.assertEqual(len(lines), 2)
        with self.assertRaises(VauError) as ctx:
            parse_vau1(compose_vau1(
                reason='P', pan='visa ****1111', expiry='1227', brand='visa',
                network='visa_vau',
            ))
        self.assertEqual(ctx.exception.code, 'invalid_pan')

    def test_visa_weekly_friday_clock_not_fed_cutoff(self):
        clock = UpdaterClock(cutoff_hour=22, tz_offset_hours=-4)
        friday_open = ts(2024, 6, 14, 16, 0)
        friday_late = ts(2024, 6, 14, 22, 30)
        thursday = ts(2024, 6, 13, 11, 0)
        self.assertTrue(clock.is_visa_window(friday_open))
        self.assertTrue(clock.is_open(friday_open, 'visa_vau'))
        self.assertTrue(clock.is_open(friday_late, 'mastercard_abu'))
        self.assertFalse(clock.is_open(friday_late, 'visa_vau'))
        self.assertFalse(clock.is_open(thursday, 'visa_vau'))
        self.assertEqual(clock.cycle_date(friday_open, 'visa_vau'), '20240614')
        self.assertEqual(clock.cycle_date(friday_late, 'visa_vau'), '20240621')
        self.assertEqual(clock.cycle_date(thursday, 'visa_vau'), '20240614')
        snap = clock.snapshot(friday_late)
        self.assertTrue(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], '22:00')
        self.assertTrue(snap['mastercard_open'])
        # Distinct from Fedwire 17:00 ET: 18:00 Friday is still inside the VAU window.
        self.assertTrue(clock.is_open(ts(2024, 6, 14, 18, 0), 'visa_vau'))


class VauServiceTests(unittest.TestCase):
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

        policy = VauPolicy(advice_fee=Decimal('0.10'))
        self.service = VauService(
            policy,
            MemoryVauStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=UpdaterClock(cutoff_hour=22, tz_offset_hours=-4),
        )

    def _add(self, nickname='Travel Visa', name='Ada Lovelace', pan=VISA, **kwargs):
        return self.service.add_key(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            pan=pan,
            expiry=kwargs.pop('expiry', '1227'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_pan(self):
        key = self._add()
        payload = key.to_dict()
        self.assertNotIn('pan', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['last4'], '1111')
        self.assertIn('****', payload['pan_masked'])
        snap = self.service.snapshot('alice')
        self.assertNotIn('pan', snap['keys'][0])
        self.assertNotIn('fingerprint', str(snap))
        advice, created = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='e1',
        )
        self.assertTrue(created)
        self.assertEqual(advice.status, 'posted')
        dumped = advice.to_dict()
        self.assertNotIn('pan', dumped)
        self.assertNotIn('vau_ref', dumped)
        self.assertTrue(dumped['vau_ref_masked'].startswith('VAU****'))
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '0.10')
        self.assertTrue(self.debits[0][2].startswith('vau fee '))

    def test_expiry_update_is_idempotent_by_trace(self):
        key = self._add()
        first, created = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='dup',
        )
        again, created_again = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.advice_id, again.advice_id)
        self.assertEqual(len(self.debits), 1)
        reloaded = self.service.store.get_key(key.key_id)
        self.assertEqual(reloaded.expiry, '1228')

    def test_visa_after_friday_cutoff_queues_mastercard_posts(self):
        self.now[0] = ts(2024, 6, 14, 22, 30)
        visa = self._add()
        queued, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=visa.key_id, reason='E', new_expiry='1228', trace_id='late',
        )
        self.assertEqual(queued.status, 'queued')
        self.assertEqual(queued.value_date, '20240621')
        self.assertEqual(self.debits, [])
        mc = self._add(nickname='MC Debit', pan=MC)
        posted, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=mc.key_id, reason='E', new_expiry='1228', trace_id='mc-late',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)
        self.now[0] = ts(2024, 6, 21, 10, 0)
        due = self.service.run_due('alice')
        statuses = {row.advice_id: row.status for row in due}
        self.assertEqual(statuses[queued.advice_id], 'posted')

    def test_thursday_visa_queues_until_friday_window(self):
        self.now[0] = ts(2024, 6, 13, 11, 0)
        key = self._add()
        queued, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='Q', trace_id='thu',
        )
        self.assertEqual(queued.status, 'queued')
        self.now[0] = ts(2024, 6, 14, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'posted')

    def test_ofac_hold_blocks_fee_until_staff_override(self):
        key = self._add(nickname='Blocked', name='Blocked Person')
        advice, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='ofac',
        )
        self.assertEqual(advice.status, 'held')
        self.assertTrue(advice.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(VauError) as ctx:
            self.service.release_advice(advice_id=advice.advice_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            advice_id=advice.advice_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(self.debits[0][1], '0.10')

    def test_pan_change_dual_control_requires_a_different_employee(self):
        key = self._add()
        advice, _ = self.service.report(
            owner_userid='alice', actor='maker', actor_type='tier1',
            key_id=key.key_id, reason='A', new_pan=VISA_NEW, new_expiry='1228', trace_id='hv',
        )
        self.assertEqual(advice.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(VauError) as ctx:
            self.service.release_advice(advice_id=advice.advice_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_advice(
            advice_id=advice.advice_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(released.releaser, 'checker')
        reloaded = self.service.store.get_key(key.key_id)
        self.assertEqual(reloaded.pan, VISA_NEW)
        self.assertNotIn(VISA_NEW, reloaded.to_dict())

    def test_closed_card_is_sensitive_and_restored_on_reverse(self):
        key = self._add()
        advice, _ = self.service.report(
            owner_userid='alice', actor='maker', actor_type='tier1',
            key_id=key.key_id, reason='C', trace_id='close',
        )
        self.assertEqual(advice.status, 'pending_release')
        posted = self.service.release_advice(
            advice_id=advice.advice_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(self.service.store.get_key(key.key_id).status, 'cancelled')
        reversed_row = self.service.reverse_advice(
            advice_id=advice.advice_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.service.store.get_key(key.key_id).status, 'active')
        self.assertEqual(self.credits[0][1], '0.10')

    def test_credit_account_and_pause(self):
        with self.assertRaises(VauError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        key = self._add()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(VauError) as ctx:
            self.service.report(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, reason='E', new_expiry='1228',
            )
        self.assertEqual(ctx.exception.code, 'key_paused')

    def test_nsf_does_not_assign_vau_ref(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        key = self._add()
        with self.assertRaises(VauError) as ctx:
            self.service.report(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, reason='E', new_expiry='1228', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['advice'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['advice'].vau_ref, '')
        self.assertEqual(self.service.store.get_key(key.key_id).expiry, '1227')

    def test_customer_reverse_window_and_cancel_open(self):
        key = self._add()
        posted, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='live',
        )
        reversed_row = self.service.reverse_advice(
            advice_id=posted.advice_id, actor='alice', actor_type='customer',
            customer_request=True,
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.service.store.get_key(key.key_id).expiry, '1227')

        later, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='E', new_expiry='1228', trace_id='late-rev',
        )
        self.now[0] = ts(2024, 6, 16, 16, 0)
        with self.assertRaises(VauError) as ctx:
            self.service.reverse_advice(
                advice_id=later.advice_id, actor='alice', actor_type='customer',
                customer_request=True,
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

        self.now[0] = ts(2024, 6, 13, 11, 0)
        queued, _ = self.service.report(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, reason='Q', trace_id='q1',
        )
        cancelled = self.service.cancel_advice(
            advice_id=queued.advice_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')

    def test_merchant_inquiry_unmatched_claim_and_export_masks(self):
        merchant = self.service.add_merchant(
            actor='teller', actor_type='tier1', nickname='Acme Store',
            legal_name='Acme LLC', bid='1234567', network='visa',
        )
        dumped = merchant.to_dict()
        self.assertNotIn('bid', dumped)
        self.assertEqual(dumped['bid_masked'], '****4567')
        inquiry, created = self.service.ingest_inquiry(
            actor='teller', actor_type='tier1', pan=VISA, expiry='1227',
            bid='1234567', trace_id='inq-1',
        )
        self.assertTrue(created)
        self.assertEqual(inquiry.status, 'unmatched')
        self.assertEqual(self.debits, [])
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            pan=VISA, default_account='1001', nickname='Travel Visa',
            legal_name='Ada Lovelace',
        )
        self.assertEqual(claimed.status, 'posted')
        self.assertEqual(claimed.reason, 'P')
        self.assertEqual(claimed.userid, 'alice')
        exported = self.service.export_file(actor='teller', actor_type='tier2', userid='alice')
        self.assertIn('VAU1|', exported)
        self.assertNotIn(VISA, exported)
        self.assertNotIn(merchant.bid, exported)

    def test_duplicate_key_and_amex_rejected(self):
        self._add()
        with self.assertRaises(VauError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        with self.assertRaises(VauError) as ctx:
            self._add(nickname='Amex', pan=AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')

    def test_sqlite_roundtrip_masks_pan(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteVauStore(path)
            service = VauService(
                VauPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=UpdaterClock(cutoff_hour=22, tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', pan=VISA,
                expiry='1227', default_account='1001',
            )
            reloaded = SqliteVauStore(path).get_key(key.key_id)
            self.assertEqual(reloaded.pan, VISA)
            self.assertNotIn('pan', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['last4'], '1111')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
