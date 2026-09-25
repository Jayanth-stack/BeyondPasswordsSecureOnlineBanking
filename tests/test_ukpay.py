import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.ukpay import (
    AmountError,
    BankOfEnglandCalendar,
    GbpUsdBook,
    MemoryUkPayStore,
    SqliteUkPayStore,
    UkPayError,
    UkPayPolicy,
    UkPayService,
    compose_gb_iban,
    compute_fee,
    easter_gregorian,
    iban_check_digit_ok,
    mask_iban,
    money_str,
    normalize_sort_code,
    parse_money,
    uk_bank_holidays,
    vocalink_modulus_ok,
)

LONDON = timezone(timedelta(hours=1))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=LONDON).timestamp()


class FoundationTests(unittest.TestCase):
    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_vocalink_accepts_computed_and_official_pairs(self):
        self.assertTrue(vocalink_modulus_ok('200000', '12345679'))
        self.assertTrue(vocalink_modulus_ok('20-00-00', '12345679'))
        self.assertEqual(normalize_sort_code('20-00-00'), '200000')
        self.assertFalse(vocalink_modulus_ok('200000', '12345678'))
        self.assertTrue(vocalink_modulus_ok('089999', '66374958'))
        self.assertTrue(vocalink_modulus_ok('107999', '88837491'))
        with self.assertRaises(UkPayError) as ctx:
            normalize_sort_code('000000')
        self.assertEqual(ctx.exception.code, 'invalid_sort_code')

    def test_gb_iban_mod97_and_mask(self):
        iban = compose_gb_iban('NWBK', '601613', '31926819')
        self.assertTrue(iban.startswith('GB'))
        self.assertEqual(len(iban), 22)
        self.assertTrue(iban_check_digit_ok(iban))
        self.assertTrue(iban_check_digit_ok('GB29NWBK60161331926819'))
        self.assertEqual(mask_iban(iban), 'GB****6819')
        self.assertFalse(iban_check_digit_ok('GB00NWBK60161331926819'))

    def test_fx_quote_and_scheme_fees(self):
        book = GbpUsdBook(Decimal('1.2500'))
        quote = book.quote(Decimal('80.00'))
        self.assertEqual(quote.amount_usd, '100.00')
        self.assertEqual(quote.amount_gbp, '80.00')
        fees = {'fps': Decimal('1.50'), 'chaps': Decimal('30.00'), 'bacs': Decimal('5.00')}
        self.assertEqual(compute_fee('fps', fees), Decimal('1.50'))
        self.assertEqual(compute_fee('chaps', fees, waived=True), Decimal('0.00'))

    def test_boe_cutoff_weekend_and_early_may(self):
        calendar = BankOfEnglandCalendar(cutoff_hour=17, tz_offset_hours=1)
        friday_before = ts(2024, 5, 3, 16, 0)
        friday_after = ts(2024, 5, 3, 17, 30)
        self.assertEqual(calendar.value_date(friday_before, scheme='chaps').isoformat(), '2024-05-03')
        self.assertEqual(calendar.value_date(friday_after, scheme='chaps').isoformat(), '2024-05-07')
        self.assertTrue(calendar.snapshot(friday_after, scheme='chaps')['after_cutoff'])
        early_may = ts(2024, 5, 6, 10, 0)
        self.assertIn(datetime(2024, 5, 6).date(), uk_bank_holidays(2024))
        self.assertEqual(easter_gregorian(2024).isoformat(), '2024-03-31')
        self.assertEqual(calendar.value_date(early_may, scheme='chaps').isoformat(), '2024-05-07')
        saturday = ts(2024, 5, 4, 11, 0)
        self.assertEqual(calendar.value_date(saturday, scheme='chaps').isoformat(), '2024-05-07')
        self.assertEqual(calendar.value_date(friday_before, scheme='fps').isoformat(), '2024-05-03')
        bacs = calendar.value_date(ts(2024, 5, 3, 15, 0), scheme='bacs')
        self.assertEqual(bacs.isoformat(), '2024-05-08')
        late_bacs = calendar.value_date(ts(2024, 5, 3, 16, 0), scheme='bacs')
        self.assertEqual(late_bacs.isoformat(), '2024-05-09')


class UkPayServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 5, 3, 11, 0)]
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

        policy = UkPayPolicy(
            fps_fee=Decimal('1.50'),
            chaps_fee=Decimal('30.00'),
            bacs_fee=Decimal('5.00'),
            dual_control_threshold=Decimal('10000.00'),
            fx_rate=Decimal('1.2500'),
        )
        self.service = UkPayService(
            policy,
            MemoryUkPayStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=BankOfEnglandCalendar(cutoff_hour=17, tz_offset_hours=1),
            fx_book=GbpUsdBook(Decimal('1.2500')),
        )

    def _add(self, nickname='Barclays Current', name='Ada Lovelace', **kwargs):
        return self.service.add_beneficiary(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            legal_name=name,
            sort_code=kwargs.pop('sort_code', '200000'),
            account_number=kwargs.pop('account_number', '12345679'),
            city=kwargs.pop('city', 'London'),
            country=kwargs.pop('country', 'GB'),
            postcode=kwargs.pop('postcode', 'EC2N4AY'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_account_number(self):
        bene = self._add()
        payload = bene.to_dict()
        self.assertNotIn('account_number', payload)
        self.assertEqual(payload['account_last4'], '5679')
        self.assertEqual(payload['sort_code'], '200000')
        self.assertEqual(payload['sort_code_formatted'], '20-00-00')
        self.assertTrue(payload['iban_masked'].startswith('GB'))
        self.assertNotIn(bene.account_number, payload['iban_masked'])
        snap = self.service.snapshot('alice')
        self.assertNotIn('account_number', snap['beneficiaries'][0])
        pay, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', scheme='fps', trace_id='u1',
        )
        self.assertTrue(created)
        self.assertNotIn('account_number', pay.to_dict())
        self.assertEqual(pay.status, 'completed')
        self.assertEqual(pay.debit_usd, '100.00')
        self.assertTrue(pay.scheme_ref.startswith('FP'))
        self.assertEqual(self.debits[0], ('1001', '100.00', 'ukpay to Barclays Current'))
        self.assertEqual(self.debits[1][1], '1.50')

    def test_same_day_fps_is_idempotent_by_trace(self):
        bene = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', scheme='fps', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='40.00', scheme='fps', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.payment_id, again.payment_id)
        self.assertEqual(len([row for row in self.debits if row[1] == '50.00']), 1)

    def test_chaps_after_cutoff_queues_until_next_business_day(self):
        self.now[0] = ts(2024, 5, 3, 17, 30)
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='50.00', scheme='chaps', trace_id='late',
        )
        self.assertEqual(pay.status, 'queued')
        self.assertEqual(pay.value_date, '20240507')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 5, 7, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        self.assertEqual(self.debits[0], ('1001', '62.50', 'ukpay to Barclays Current'))

    def test_bacs_is_tplus2_and_settles_on_value_date(self):
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='20.00', scheme='bacs', trace_id='b1',
        )
        self.assertEqual(pay.status, 'queued')
        self.assertEqual(pay.value_date, '20240508')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 5, 8, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'sent')
        completed = self.service.complete_payment(
            payment_id=pay.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(completed.status, 'completed')

    def test_ofac_hold_blocks_debit_until_staff_override(self):
        bene = self._add(nickname='Blocked', name='Blocked Person')
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='80.00', scheme='fps', trace_id='ofac',
        )
        self.assertEqual(pay.status, 'held')
        self.assertTrue(pay.ofac_hit)
        self.assertEqual(self.debits, [])
        with self.assertRaises(UkPayError) as ctx:
            self.service.release_payment(payment_id=pay.payment_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            payment_id=pay.payment_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'completed')
        self.assertEqual(self.debits[0][1], '100.00')

    def test_dual_control_uses_usd_equivalent(self):
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            beneficiary_id=bene.beneficiary_id, amount='8000.00', scheme='chaps', trace_id='hv',
        )
        self.assertEqual(pay.status, 'pending_release')
        self.assertEqual(pay.debit_usd, '10000.00')
        self.assertEqual(self.debits, [])
        with self.assertRaises(UkPayError) as ctx:
            self.service.release_payment(payment_id=pay.payment_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_payment(payment_id=pay.payment_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'sent')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.debits[0][1], '10000.00')

    def test_fps_is_irrevocable_after_send(self):
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='30.00', scheme='fps', trace_id='irr',
        )
        self.assertEqual(pay.status, 'completed')
        with self.assertRaises(UkPayError) as ctx:
            self.service.recall_payment(payment_id=pay.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'scheme_irrevocable')
        with self.assertRaises(UkPayError) as ctx:
            self.service.complete_payment(payment_id=pay.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

    def test_credit_account_and_pause_and_archive(self):
        bene = self._add()
        with self.assertRaises(UkPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(UkPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'beneficiary_paused')
        closed = self.service.set_beneficiary_status(
            beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(UkPayError) as ctx:
            self.service.set_beneficiary_status(
                beneficiary_id=bene.beneficiary_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_nsf_does_not_assign_scheme_ref(self):
        def nsf(account, amount, remark):
            self.debits.append((account, amount, remark))
            return 'Insufficient Balance'

        self.service.debit_fn = nsf
        bene = self._add()
        with self.assertRaises(UkPayError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                beneficiary_id=bene.beneficiary_id, amount='30.00', scheme='fps', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].status, 'nsf')
        self.assertEqual(ctx.exception.extra['payment'].scheme_ref, '')
        self.assertEqual(ctx.exception.extra['payment'].end_to_end_id, '')

    def test_chaps_complete_and_recall_credits_usd(self):
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='60.00', scheme='chaps', trace_id='c1',
        )
        self.assertEqual(pay.status, 'sent')
        completed = self.service.complete_payment(
            payment_id=pay.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(completed.status, 'completed')
        with self.assertRaises(UkPayError) as ctx:
            self.service.recall_payment(payment_id=pay.payment_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='15.00', scheme='chaps', trace_id='c2',
        )
        recalled = self.service.recall_payment(
            payment_id=other.payment_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual(self.credits[0][1], '18.75')

    def test_customer_can_cancel_queued_not_sent(self):
        self.now[0] = ts(2024, 5, 3, 18, 0)
        bene = self._add()
        pay, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', scheme='chaps', trace_id='q1',
        )
        cancelled = self.service.cancel_payment(
            payment_id=pay.payment_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = ts(2024, 5, 3, 11, 0)
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            beneficiary_id=bene.beneficiary_id, amount='22.00', scheme='chaps', trace_id='live2',
        )
        self.assertEqual(live.status, 'sent')
        with self.assertRaises(UkPayError) as ctx:
            self.service.cancel_payment(payment_id=live.payment_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_sqlite_roundtrip_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteUkPayStore(path)
            service = UkPayService(
                UkPayPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=BankOfEnglandCalendar(cutoff_hour=17, tz_offset_hours=1),
            )
            bene = service.add_beneficiary(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='Lloyds', legal_name='Ada Lovelace', sort_code='200000',
                account_number='12345679', city='London', country='GB',
                postcode='EC2N4AY', default_account='1001',
            )
            reloaded = SqliteUkPayStore(path).get_beneficiary(bene.beneficiary_id)
            self.assertEqual(reloaded.account_number, '12345679')
            self.assertNotIn('account_number', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['account_last4'], '5679')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
