import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.lockbox import (
    AmountError,
    LockboxError,
    LockboxPolicy,
    LockboxService,
    MemoryLockboxStore,
    SqliteLockboxStore,
    compose_amount_field,
    compose_bai2,
    compose_bank_ref,
    invoice_fingerprint,
    mask_bank_ref,
    message_from_values,
    normalize_lockbox_id,
    parse_amount_field,
    parse_bai2,
    split_bai2_file,
)
from utility.wire import WireCalendar

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


SAMPLE = (
    "01,LOCKBOX1,021000021,240614,1000,000001,,,2/\n"
    "02,LOCKBOX1,1,240614,1000,USD,2/\n"
    "03,1234567,USD,072,0000012500,1,V/\n"
    "16,174,0000012500,V,202406140000001,INV-1001,ACME CORP/\n"
    "88,SERIAL 4521/\n"
    "49,0000012500,3/\n"
    "98,0000012500,1,5/\n"
    "99,0000012500,1,7/\n"
)


class FoundationTests(unittest.TestCase):
    def test_amount_field_roundtrip(self):
        self.assertEqual(compose_amount_field(Decimal('125.00')), '0000012500')
        self.assertEqual(parse_amount_field('12500'), Decimal('125.00'))
        with self.assertRaises(AmountError):
            parse_amount_field('nope')

    def test_lockbox_id_and_bank_ref(self):
        self.assertEqual(normalize_lockbox_id('1234567'), '1234567')
        self.assertEqual(normalize_lockbox_id('42'), '0000042')
        with self.assertRaises(LockboxError) as ctx:
            normalize_lockbox_id('0000000')
        self.assertEqual(ctx.exception.code, 'invalid_lockbox')
        self.assertEqual(compose_bank_ref('20240614', 1), '202406140000001')
        self.assertEqual(mask_bank_ref('202406140000001'), '20240614***0001')

    def test_invoice_fingerprint(self):
        self.assertEqual(invoice_fingerprint('inv-1001'), 'INV1001')
        self.assertEqual(invoice_fingerprint('INV 1001'), 'INV1001')

    def test_bai2_parse_compose_and_xml_rejected(self):
        parsed = parse_bai2(SAMPLE)
        self.assertEqual(parsed['receiver'], '021000021')
        self.assertEqual(len(parsed['items']), 1)
        self.assertEqual(parsed['items'][0]['amount'], '125.00')
        self.assertEqual(parsed['items'][0]['serial'], '4521')
        self.assertEqual(parsed['items'][0]['invoice'], 'INV-1001')
        rebuilt = compose_bai2(parsed['items'], receiver='021000021', as_of='20240614', lockbox_id='1234567')
        self.assertIn('16,174,', rebuilt)
        self.assertIn('03,1234567,USD,072,', rebuilt)
        with self.assertRaises(LockboxError) as ctx:
            parse_bai2('<?xml version="1.0"?><bai/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(LockboxError):
            parse_bai2('<!DOCTYPE bai><file/>')

    def test_message_from_values_and_split(self):
        entries = split_bai2_file(SAMPLE)
        self.assertEqual(len(entries), 1)
        message = message_from_values({'file': SAMPLE})
        self.assertEqual(message['amount'], '125.00')
        self.assertEqual(message['lockbox_id'], '1234567')


class LockboxServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = [ts(2024, 6, 14, 11, 0)]
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

        policy = LockboxPolicy(dual_control_threshold=Decimal('10000.00'))
        self.service = LockboxService(
            policy,
            MemoryLockboxStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )

    def _enroll(self, nickname='AR Box', lockbox_id='1234567', **kwargs):
        return self.service.enroll(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            lockbox_id=lockbox_id,
            credit_account=kwargs.pop('credit_account', '1001'),
            require_invoice=kwargs.pop('require_invoice', False),
            **kwargs,
        )

    def _ingest(self, **kwargs):
        payload = {
            'lockbox_id': '1234567',
            'amount': '125.00',
            'remitter_name': 'Acme Corp',
            'serial': '4521',
            'invoice': 'INV-1001',
            'receiver_aba': '021000021',
        }
        payload.update(kwargs)
        return self.service.ingest(actor='teller', actor_type='tier1', values=payload)

    def test_snapshot_never_leaks_credit_account_or_raw_ref(self):
        box = self._enroll()
        payload = box.to_dict()
        self.assertNotIn('credit_account', payload)
        self.assertEqual(payload['account_last4'], '1001')
        item, created = self._ingest(bank_ref='202406140000001')
        self.assertTrue(created)
        snap = item.to_dict()
        self.assertNotIn('credit_account', snap)
        self.assertNotIn('bank_ref', snap)
        self.assertEqual(snap['bank_ref_masked'], '20240614***0001')
        self.assertEqual(item.status, 'posted')
        self.assertEqual(self.credits[0], ('1001', '125.00', 'lockbox 406140000001'))
        overview = self.service.snapshot('alice')
        self.assertNotIn('credit_account', overview['enrollments'][0])
        self.assertNotIn('bank_ref', overview['items'][0])

    def test_ingest_is_idempotent_by_bank_ref(self):
        self._enroll()
        first, created = self._ingest(bank_ref='DUPREF000000001')
        again, created_again = self._ingest(bank_ref='DUPREF000000001', amount='50.00')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.item_id, again.item_id)
        self.assertEqual(len(self.credits), 1)

    def test_unenrolled_and_wrong_receiver(self):
        item, created = self._ingest(bank_ref='UNMATCH00000001')
        self.assertTrue(created)
        self.assertEqual(item.status, 'unmatched')
        self.assertEqual(self.credits, [])
        with self.assertRaises(LockboxError) as ctx:
            self._ingest(receiver_aba='026009593', bank_ref='WRONG0000000001')
        self.assertEqual(ctx.exception.code, 'wrong_receiver')

    def test_ofac_hold_blocks_credit_until_staff_override(self):
        self._enroll()
        item, _ = self._ingest(remitter_name='Blocked Person', bank_ref='OFAC00000000001')
        self.assertEqual(item.status, 'held')
        self.assertEqual(self.credits, [])
        with self.assertRaises(LockboxError) as ctx:
            self.service.release(item_id=item.item_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            item_id=item.item_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(self.credits[0][1], '125.00')

    def test_dual_control_requires_a_different_employee(self):
        self._enroll()
        item, _ = self._ingest(amount='10000.00', bank_ref='HV0000000000001')
        self.assertEqual(item.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(LockboxError) as ctx:
            self.service.release(item_id=item.item_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release(item_id=item.item_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'posted')
        self.assertEqual(released.releaser, 'checker')

    def test_after_cutoff_queues_until_next_business_day(self):
        self.now[0] = ts(2024, 6, 14, 15, 0)
        self._enroll()
        item, _ = self._ingest(bank_ref='LATE00000000001')
        self.assertEqual(item.status, 'queued')
        self.assertEqual(self.credits, [])
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'posted')
        self.assertEqual(self.credits[0][0], '1001')

    def test_require_invoice_then_rematch(self):
        box = self._enroll(require_invoice=True)
        item, _ = self._ingest(bank_ref='INV000000000001')
        self.assertEqual(item.status, 'unmatched')
        self.assertEqual(item.reason, 'invoice_not_found')
        self.assertEqual(self.credits, [])
        self.service.add_invoice(
            enrollment_id=box.enrollment_id, actor='alice', actor_type='customer',
            invoice_number='INV-1001', amount='125.00',
        )
        rematched = self.service.get_item(item_id=item.item_id, actor='alice', actor_type='customer')
        self.assertEqual(rematched.status, 'posted')
        self.assertTrue(rematched.invoice_matched)
        self.assertEqual(self.credits[0][1], '125.00')

    def test_credit_account_pause_archive_and_return(self):
        with self.assertRaises(LockboxError) as ctx:
            self._enroll(nickname='Card', lockbox_id='7654321', credit_account='1003')
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        box = self._enroll()
        paused = self.service.set_enrollment_status(
            enrollment_id=box.enrollment_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        item, _ = self._ingest(bank_ref='PAUSED000000001')
        self.assertEqual(item.status, 'unmatched')
        self.service.set_enrollment_status(
            enrollment_id=box.enrollment_id, actor='alice', actor_type='customer', status='resume',
        )
        live, _ = self._ingest(bank_ref='LIVE00000000001')
        self.assertEqual(live.status, 'posted')
        returned = self.service.request_return(
            item_id=live.item_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(self.debits[0][1], '125.00')
        self.assertTrue(self.debits[0][2].startswith('lockbox return'))
        closed = self.service.set_enrollment_status(
            enrollment_id=box.enrollment_id, actor='alice', actor_type='customer', status='archive',
        )
        self.assertEqual(closed.status, 'archived')
        with self.assertRaises(LockboxError) as ctx:
            self.service.set_enrollment_status(
                enrollment_id=box.enrollment_id, actor='alice', actor_type='customer', status='resume',
            )
        self.assertEqual(ctx.exception.code, 'already_archived')

    def test_assign_unmatched_and_file_ingest(self):
        item, _ = self._ingest(bank_ref='ASSIGN000000001')
        self.assertEqual(item.status, 'unmatched')
        assigned = self.service.assign(
            item_id=item.item_id, actor='teller', actor_type='tier1',
            customer_id='alice', credit_account='1001', nickname='AR Box',
            lockbox_id='1234567',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.userid, 'alice')
        result = self.service.ingest_file(actor='teller', actor_type='tier1', text=SAMPLE)
        self.assertEqual(result['accepted_count'] + result['duplicate_count'], 1)
        exported = self.service.export_file(actor='teller', actor_type='tier2', customer_id='alice')
        self.assertIn('16,174,', exported)
        self.assertIn('1234567', exported)

    def test_return_window_and_failed_credit(self):
        self._enroll()
        item, _ = self._ingest(bank_ref='WIN000000000001')
        self.now[0] = ts(2024, 6, 18, 11, 0)
        with self.assertRaises(LockboxError) as ctx:
            self.service.request_return(item_id=item.item_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_item(item_id=item.item_id, actor='teller', actor_type='tier2')
        self.assertEqual(staff.status, 'returned')

        def boom(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'Ledger down'

        self.service.credit_fn = boom
        with self.assertRaises(LockboxError) as ctx:
            self._ingest(bank_ref='FAIL00000000001', amount='10.00')
        self.assertEqual(ctx.exception.code, 'failed')
        self.assertEqual(ctx.exception.extra['item'].status, 'failed')

    def test_sqlite_roundtrip_masks_account(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteLockboxStore(path)
            service = LockboxService(
                LockboxPolicy(),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                credit_fn=lambda account, amount, remark: 'Success',
                calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
            )
            box = service.enroll(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', lockbox_id='8888888', credit_account='1001',
            )
            reloaded = SqliteLockboxStore(path).get_enrollment(box.enrollment_id)
            self.assertEqual(reloaded.credit_account, '1001')
            self.assertNotIn('credit_account', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['account_last4'], '1001')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
