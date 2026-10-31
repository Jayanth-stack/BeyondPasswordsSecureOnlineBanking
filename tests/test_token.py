import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.token import (
    MemoryTokenStore,
    SqliteTokenStore,
    TokenClock,
    TokenError,
    TokenPolicy,
    TokenService,
    brand_from_iin,
    compose_par,
    compose_tavv,
    compose_token1,
    compose_token_pan,
    compose_token_ref,
    compute_fee,
    luhn_check_digit_ok,
    mask_pan,
    mask_token_ref,
    network_for_brand,
    normalize_action,
    normalize_expiry,
    normalize_pan,
    pan_fingerprint,
    parse_token1,
    split_token1_file,
)

ET = timezone(timedelta(hours=-4))
VISA = '4111111111111111'
MC = '5555555555554444'
AMEX = '378282246310005'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_luhn_brand_par_and_token_pan(self):
        self.assertTrue(luhn_check_digit_ok(VISA))
        self.assertTrue(luhn_check_digit_ok(MC))
        self.assertEqual(normalize_pan('4111-1111-1111-1111'), VISA)
        self.assertEqual(brand_from_iin(VISA), 'visa')
        self.assertEqual(brand_from_iin(MC), 'mastercard')
        self.assertEqual(network_for_brand('visa'), 'visa_vts')
        self.assertEqual(network_for_brand('mastercard'), 'mastercard_mdes')
        with self.assertRaises(TokenError) as ctx:
            brand_from_iin(AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')
        with self.assertRaises(TokenError) as ctx:
            normalize_pan('4111111111111112')
        self.assertEqual(ctx.exception.code, 'invalid_pan')
        masked = mask_pan(VISA)
        self.assertNotIn(VISA, masked)
        self.assertTrue(masked.endswith('1111'))
        self.assertEqual(len(pan_fingerprint(VISA)), 64)
        par = compose_par(VISA, 'visa')
        self.assertEqual(len(par), 29)
        self.assertTrue(par.startswith('V'))
        token = compose_token_pan(VISA, 'apple_pay', 1)
        self.assertTrue(luhn_check_digit_ok(token))
        self.assertNotEqual(token, VISA)
        self.assertTrue(token.startswith('4'))
        tavv = compose_tavv(token, par, '20240614')
        self.assertEqual(len(tavv), 28)

    def test_expiry_action_and_token_ref(self):
        self.assertEqual(normalize_expiry('12/27'), '1227')
        self.assertEqual(normalize_action('provision'), 'P')
        self.assertEqual(normalize_action('suspend'), 'S')
        self.assertEqual(normalize_action('reprovision'), 'X')
        with self.assertRaises(TokenError) as ctx:
            normalize_action('auth')
        self.assertEqual(ctx.exception.code, 'invalid_action')
        self.assertEqual(compose_token_ref('20240614', 1), 'TKN20240614000001')
        self.assertEqual(mask_token_ref('TKN20240614000001'), 'TKN****0001')
        with self.assertRaises(TokenError) as ctx:
            compose_token_ref('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_reference')
        self.assertEqual(compute_fee(Decimal('0.15')), Decimal('0.15'))
        self.assertEqual(compute_fee(Decimal('0.15'), waived=True), Decimal('0.00'))

    def test_token1_parse_rejects_xml(self):
        line = compose_token1(
            action='P', pan=VISA, token_pan='4111111111111111', expiry='1227',
            brand='visa', network='visa_vts', trid='40000012345', wallet='apple_pay',
            reference='inq-1', status='posted',
        )
        parsed = parse_token1(line)
        self.assertEqual(parsed['pan'], VISA)
        with self.assertRaises(TokenError) as ctx:
            parse_token1('<?xml version="1.0"?><token/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(TokenError):
            split_token1_file('<!DOCTYPE html>')
        lines = split_token1_file(line + '\n\n' + line)
        self.assertEqual(len(lines), 2)
        with self.assertRaises(TokenError) as ctx:
            parse_token1(compose_token1(
                action='P', pan='visa ****1111', expiry='1227', brand='visa',
                network='visa_vts',
            ))
        self.assertEqual(ctx.exception.code, 'invalid_pan')

    def test_token_clock_is_24_7_not_fed_or_vau(self):
        clock = TokenClock(tz_offset_hours=-4)
        sunday = ts(2024, 6, 16, 2, 0)
        friday_late = ts(2024, 6, 14, 22, 30)
        weekday_fed_closed = ts(2024, 6, 14, 18, 0)
        self.assertTrue(clock.is_open(sunday, 'visa_vts'))
        self.assertTrue(clock.is_open(friday_late, 'visa_vts'))
        self.assertTrue(clock.is_open(weekday_fed_closed, 'mastercard_mdes'))
        snap = clock.snapshot(friday_late)
        self.assertTrue(snap['always_open'])
        self.assertFalse(snap['after_cutoff'])
        self.assertEqual(snap['cutoff'], 'none')
        self.assertEqual(clock.cycle_date(sunday), '20240616')


class TokenServiceTests(unittest.TestCase):
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

        policy = TokenPolicy(provision_fee=Decimal('0.15'))
        self.service = TokenService(
            policy,
            MemoryTokenStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=TokenClock(tz_offset_hours=-4),
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

    def test_snapshot_and_to_dict_never_leak_secrets(self):
        key = self._add()
        payload = key.to_dict()
        self.assertNotIn('pan', payload)
        self.assertNotIn('fingerprint', payload)
        self.assertEqual(payload['last4'], '1111')
        self.assertIn('****', payload['pan_masked'])
        event, created = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='p1',
        )
        self.assertTrue(created)
        self.assertEqual(event.status, 'posted')
        dumped = event.to_dict()
        self.assertNotIn('pan', dumped)
        self.assertNotIn('token_pan', dumped)
        self.assertNotIn('par', dumped)
        self.assertNotIn('tavv', dumped)
        self.assertNotIn('token_ref', dumped)
        self.assertTrue(dumped['token_ref_masked'].startswith('TKN****'))
        self.assertEqual(event.token_status, 'provisioned')
        self.assertTrue(luhn_check_digit_ok(event.token_pan))
        self.assertNotEqual(event.token_pan, VISA)
        self.assertEqual(len(event.par), 29)
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '0.15')
        self.assertTrue(self.debits[0][2].startswith('token fee '))
        snap = self.service.snapshot('alice')
        self.assertNotIn(VISA, str(snap))
        self.assertNotIn(event.token_pan, str(snap))
        self.assertNotIn(event.tavv, str(snap))
        self.assertNotIn(event.par, str(snap))

    def test_merchant_provision_is_idempotent_by_trace(self):
        key = self._add()
        first, created = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='dup',
        )
        again, created_again = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.event_id, again.event_id)
        self.assertEqual(len(self.debits), 1)

    def test_weekend_and_after_fed_cutoff_still_post(self):
        self.now[0] = ts(2024, 6, 16, 2, 0)
        key = self._add()
        posted, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='sun',
        )
        self.assertEqual(posted.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_device_provision_dual_control_requires_a_different_employee(self):
        key = self._add()
        event, _ = self.service.provision(
            owner_userid='alice', actor='maker', actor_type='tier1',
            key_id=key.key_id, wallet_kind='apple_pay', trace_id='dev',
        )
        self.assertEqual(event.status, 'pending_release')
        self.assertEqual(self.debits, [])
        with self.assertRaises(TokenError) as ctx:
            self.service.release_event(event_id=event.event_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_event(
            event_id=event.event_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(released.token_status, 'provisioned')
        self.assertNotIn(released.token_pan, released.to_dict())

    def test_ofac_hold_blocks_fee_until_staff_override(self):
        key = self._add(nickname='Blocked', name='Blocked Person')
        event, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='ofac',
        )
        self.assertEqual(event.status, 'held')
        self.assertTrue(event.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(TokenError) as ctx:
            self.service.release_event(event_id=event.event_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            event_id=event.event_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(self.debits[0][1], '0.15')

    def test_suspend_resume_deactivate_and_reverse(self):
        key = self._add()
        posted, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='live',
        )
        suspended, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', action='S',
            event_id=posted.event_id, trace_id='sus',
        )
        self.assertEqual(suspended.status, 'posted')
        self.assertEqual(self.service.store.get_event(posted.event_id).token_status, 'suspended')
        resumed, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', action='R',
            event_id=posted.event_id, trace_id='res',
        )
        self.assertEqual(self.service.store.get_event(posted.event_id).token_status, 'provisioned')
        deactivated, _ = self.service.provision(
            owner_userid='alice', actor='maker', actor_type='tier1',
            key_id=key.key_id, wallet_kind='merchant', action='D',
            event_id=posted.event_id, trace_id='deact',
        )
        self.assertEqual(deactivated.status, 'pending_release')
        posted_d = self.service.release_event(
            event_id=deactivated.event_id, actor='checker', actor_type='tier2',
        )
        self.assertEqual(posted_d.status, 'posted')
        self.assertEqual(self.service.store.get_event(posted.event_id).token_status, 'deactivated')
        reversed_row = self.service.reverse_event(
            event_id=posted_d.event_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(self.service.store.get_event(posted.event_id).token_status, 'provisioned')

    def test_credit_account_and_pause(self):
        with self.assertRaises(TokenError) as ctx:
            self._add(default_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        key = self._add()
        paused = self.service.set_key_status(
            key_id=key.key_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(TokenError) as ctx:
            self.service.provision(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, wallet_kind='merchant',
            )
        self.assertEqual(ctx.exception.code, 'key_paused')

    def test_nsf_does_not_assign_token_ref(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        key = self._add()
        with self.assertRaises(TokenError) as ctx:
            self.service.provision(
                owner_userid='alice', actor='alice', actor_type='customer',
                key_id=key.key_id, wallet_kind='merchant', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['event'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['event'].token_ref, '')
        self.assertEqual(ctx.exception.extra['event'].token_pan, '')

    def test_customer_reverse_window_and_cancel_open(self):
        key = self._add()
        posted, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='merchant', trace_id='live',
        )
        reversed_row = self.service.reverse_event(
            event_id=posted.event_id, actor='alice', actor_type='customer',
            customer_request=True,
        )
        self.assertEqual(reversed_row.status, 'reversed')
        self.assertEqual(reversed_row.token_status, 'deactivated')
        self.assertEqual(self.credits[0][1], '0.15')

        later, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='google_pay', trace_id='late-rev',
        )
        self.assertEqual(later.status, 'pending_release')
        released = self.service.release_event(
            event_id=later.event_id, actor='teller', actor_type='tier1',
        )
        self.assertEqual(released.status, 'posted')
        self.now[0] = ts(2024, 6, 16, 16, 0)
        with self.assertRaises(TokenError) as ctx:
            self.service.reverse_event(
                event_id=released.event_id, actor='alice', actor_type='customer',
                customer_request=True,
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')

        pending, _ = self.service.provision(
            owner_userid='alice', actor='alice', actor_type='customer',
            key_id=key.key_id, wallet_kind='samsung_pay', trace_id='q1',
        )
        cancelled = self.service.cancel_event(
            event_id=pending.event_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')

    def test_wallet_inquiry_unmatched_claim_and_export_masks(self):
        wallet = self.service.add_wallet(
            actor='teller', actor_type='tier1', nickname='Apple Wallet',
            legal_name='Apple Inc', trid='40000012345', network='visa',
            wallet_kind='apple_pay',
        )
        dumped = wallet.to_dict()
        self.assertNotIn('trid', dumped)
        self.assertEqual(dumped['trid_masked'], '****2345')
        inquiry, created = self.service.ingest_inquiry(
            actor='teller', actor_type='tier1', pan=VISA, expiry='1227',
            trid='40000012345', trace_id='inq-1',
        )
        self.assertTrue(created)
        self.assertEqual(inquiry.status, 'unmatched')
        self.assertEqual(self.debits, [])
        claimed = self.service.claim(
            owner_userid='alice', actor='alice', actor_type='customer',
            pan=VISA, default_account='1001', nickname='Travel Visa',
            legal_name='Ada Lovelace',
        )
        self.assertIn(claimed.status, {'posted', 'pending_release'})
        self.assertEqual(claimed.action, 'P')
        self.assertEqual(claimed.userid, 'alice')
        if claimed.status == 'pending_release':
            claimed = self.service.release_event(
                event_id=claimed.event_id, actor='checker', actor_type='tier2',
            )
        exported = self.service.export_file(actor='teller', actor_type='tier2', userid='alice')
        self.assertIn('TOKEN1|', exported)
        self.assertNotIn(VISA, exported)
        self.assertNotIn(wallet.trid, exported)
        self.assertNotIn(claimed.token_pan, exported)

    def test_duplicate_key_and_amex_rejected(self):
        self._add()
        with self.assertRaises(TokenError) as ctx:
            self._add()
        self.assertEqual(ctx.exception.code, 'key_duplicate')
        with self.assertRaises(TokenError) as ctx:
            self._add(nickname='Amex', pan=AMEX)
        self.assertEqual(ctx.exception.code, 'invalid_brand')

    def test_sqlite_roundtrip_masks_pan(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteTokenStore(path)
            service = TokenService(
                TokenPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=TokenClock(tz_offset_hours=-4),
            )
            key = service.add_key(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', legal_name='Ada Lovelace', pan=VISA,
                expiry='1227', default_account='1001',
            )
            reloaded = SqliteTokenStore(path).get_key(key.key_id)
            self.assertEqual(reloaded.pan, VISA)
            self.assertNotIn('pan', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['last4'], '1111')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
