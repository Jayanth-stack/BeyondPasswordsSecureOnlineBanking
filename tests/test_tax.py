import os
import tempfile
import unittest
from decimal import Decimal

from utility.tax import (
    AmountError,
    MemoryTaxStore,
    SqliteTaxStore,
    TaxError,
    TaxPolicy,
    TaxService,
    classify_box,
    money_str,
    parse_money,
    resolve_tax_year,
)


class TaxServiceTests(unittest.TestCase):
    def setUp(self):
        # 2024-06-15 UTC — mid-year so current=2024, prior=2023
        self.now = [1_718_409_600.0]
        self.service = TaxService(
            TaxPolicy(),
            MemoryTaxStore(),
            clock=lambda: self.now[0],
            recipient_fn=lambda userid: {
                'name': 'Ada Lovelace',
                'address': '1 Analytical Engine',
                'tin_last4': '4321',
            },
        )

    def _post(self, amount='40.00', box='interest', year=2023, account='1001', source_id=None):
        return self.service.post(
            owner_userid='alice',
            actor='teller',
            actor_type='tier1',
            account=account,
            amount=amount,
            box=box,
            tax_year=year,
            description='staff interest',
            source_id=source_id,
        )

    def test_parse_money_rejects_junk(self):
        with self.assertRaises(AmountError):
            parse_money('nope')
        self.assertEqual(money_str(parse_money('$12.355')), '12.36')

    def test_classify_skips_ordinary_deposits(self):
        self.assertIsNone(classify_box('credit', 'credit', 'direct deposit'))
        self.assertIsNone(classify_box('open', 'credit', 'new account bonus'))
        self.assertEqual(classify_box('interest', 'credit', 'interest credited for 2023-01'), 'interest')
        self.assertEqual(classify_box('credit', 'credit', 'interest credited for 2023-01'), 'interest')
        self.assertIsNone(classify_box('interest', 'debit', 'interest reversal attempt'))
        self.assertEqual(classify_box('withdraw', 'debit', 'early withdrawal penalty'), 'early_withdrawal')

    def test_observe_is_idempotent_by_source_id(self):
        first = self.service.observe(
            '1001', '12.50', 'interest', direction='credit',
            userid='alice', description='interest credited for 2023-01',
            source_id='int:1001:2023-01', created_at=1_686_787_200.0,
        )
        self.now[0] += 10
        second = self.service.observe(
            '1001', '12.50', 'interest', direction='credit',
            userid='alice', description='interest credited for 2023-01',
            source_id='int:1001:2023-01', created_at=1_686_787_200.0,
        )
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(len(self.service.store.list_entries('alice', 2023)), 1)

    def test_observe_ignores_direct_deposits(self):
        self.assertIsNone(self.service.observe(
            '1001', '250', 'credit', direction='credit',
            userid='alice', description='direct deposit', source_id='cr:1',
        ))
        self.assertEqual(self.service.store.list_entries('alice'), [])

    def test_customer_cannot_post(self):
        with self.assertRaises(TaxError) as ctx:
            self.service.post(
                owner_userid='alice', actor='alice', actor_type='customer',
                account='1001', amount='10', box='interest',
            )
        self.assertEqual(ctx.exception.code, 'tax_forbidden')

    def test_generate_idempotent_then_force_rebuild(self):
        self._post('40.00')
        form, created = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer', tax_year=2023,
        )
        self.assertTrue(created)
        self.assertEqual(form.box1, '40.00')
        self.assertEqual(form.status, 'issued')
        self.assertTrue(form.required)
        self.assertEqual(form.recipient_tin_last4, '4321')

        again, created_again = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer', tax_year=2023,
        )
        self.assertFalse(created_again)
        self.assertEqual(again.form_id, form.form_id)

        self._post('15.00', source_id='extra')
        rebuilt, created_force = self.service.generate(
            owner_userid='alice', actor='teller', actor_type='tier1',
            tax_year=2023, force=True,
        )
        self.assertFalse(created_force)
        self.assertEqual(rebuilt.form_id, form.form_id)
        self.assertEqual(rebuilt.box1, '55.00')

    def test_below_threshold_is_not_required(self):
        self._post('9.99')
        form, _ = self.service.generate(
            owner_userid='alice', actor='teller', actor_type='tier1', tax_year=2023,
        )
        self.assertEqual(form.box1, '9.99')
        self.assertFalse(form.required)

    def test_current_year_is_interim_and_cannot_file(self):
        self._post('20.00', year=2024)
        form, _ = self.service.generate(
            owner_userid='alice', actor='teller', actor_type='tier1', tax_year=2024,
        )
        self.assertEqual(form.status, 'interim')
        with self.assertRaises(TaxError) as ctx:
            self.service.file(form_id=form.form_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'year_not_closed')

    def test_file_then_correct_after_void(self):
        posted = self._post('25.00')
        form, _ = self.service.generate(
            owner_userid='alice', actor='teller', actor_type='tier1', tax_year=2023,
        )
        filed = self.service.file(form_id=form.form_id, actor='mgr', actor_type='tier2')
        self.assertEqual(filed.status, 'filed')
        with self.assertRaises(TaxError) as ctx:
            self.service.generate(
                owner_userid='alice', actor='teller', actor_type='tier1',
                tax_year=2023, force=True,
            )
        self.assertEqual(ctx.exception.code, 'already_filed')

        self.service.void_entry(entry_id=posted.entry_id, actor='teller', actor_type='tier1')
        correction = self.service.correct(
            form_id=form.form_id, actor='mgr', actor_type='tier2',
        )
        self.assertEqual(correction.correction_of, form.form_id)
        self.assertEqual(correction.box1, '0.00')
        self.assertEqual(self.service.store.get_form(form.form_id).status, 'corrected')

    def test_copy_request_limit_and_fulfill(self):
        self._post('12.00')
        form, _ = self.service.generate(
            owner_userid='alice', actor='alice', actor_type='customer', tax_year=2023,
        )
        copy = self.service.request_copy(
            form_id=form.form_id, actor='alice', actor_type='customer', channel='mail',
        )
        self.assertEqual(copy.status, 'pending')
        with self.assertRaises(TaxError) as ctx:
            self.service.request_copy(
                form_id=form.form_id, actor='alice', actor_type='customer', channel='mail',
            )
        self.assertEqual(ctx.exception.code, 'request_duplicate')
        fulfilled = self.service.decide_copy(
            request_id=copy.request_id, actor='teller', actor_type='tier1',
            decision='fulfill',
        )
        self.assertEqual(fulfilled.status, 'fulfilled')

    def test_future_year_rejected(self):
        with self.assertRaises(TaxError) as ctx:
            self.service.generate(
                owner_userid='alice', actor='teller', actor_type='tier1', tax_year=2025,
            )
        self.assertEqual(ctx.exception.code, 'year_in_future')

    def test_resolve_tax_year_aliases(self):
        self.assertEqual(resolve_tax_year('prior', now=self.now[0]), 2023)
        self.assertEqual(resolve_tax_year('current', now=self.now[0]), 2024)
        self.assertEqual(resolve_tax_year(None, now=self.now[0], default='prior'), 2023)

    def test_sqlite_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteTaxStore(path)
            service = TaxService(TaxPolicy(), store, clock=lambda: self.now[0])
            service.post(
                owner_userid='alice', actor='teller', actor_type='tier1',
                account='1001', amount='11.00', box='interest', tax_year=2023,
                source_id='sql:1',
            )
            form, _ = service.generate(
                owner_userid='alice', actor='teller', actor_type='tier1', tax_year=2023,
            )
            reloaded = SqliteTaxStore(path)
            found = reloaded.get_form(form.form_id)
            self.assertIsNotNone(found)
            self.assertEqual(found.box1, '11.00')
            self.assertEqual(len(reloaded.list_entries('alice', 2023)), 1)
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


if __name__ == '__main__':
    unittest.main()
