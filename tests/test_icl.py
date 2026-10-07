import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.icl import (
    IclError,
    IclPolicy,
    IclService,
    MemoryIclStore,
    SqliteIclStore,
    compose_amount_field,
    compose_ece,
    compose_return_x937,
    compose_x937,
    image_fingerprint,
    mask_ece,
    message_from_x937,
    normalize_ece,
    parse_amount_field,
    parse_onus,
    parse_x937,
    split_x937_file,
)
from utility.wire import WireCalendar, aba_check_digit_ok, normalize_aba

ET = timezone(timedelta(hours=-4))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


SAMPLE_FILE = """01|FileHeader|20240614|011000015|021000021|CL01
10|CashLetter|CL01|20240614|021000021
20|Bundle|B1
25|Check|202406140000001||021000021|1001|4321|0000010000|011000015|Ada Lovelace
50|Image|Y|Y|
70|BundleControl|1|0000010000
90|CashLetterControl|1|0000010000
99|FileControl|1|1|0000010000
"""


class FoundationTests(unittest.TestCase):
    def test_amount_field_roundtrip(self):
        self.assertEqual(compose_amount_field(Decimal('100.00')), '0000010000')
        self.assertEqual(parse_amount_field('0000010000'), Decimal('100.00'))
        self.assertEqual(parse_amount_field('$12.355'), Decimal('12.36'))
        with self.assertRaises(IclError) as ctx:
            parse_amount_field('')
        self.assertEqual(ctx.exception.code, 'invalid_amount')

    def test_ece_layout_and_mask(self):
        self.assertEqual(compose_ece('20240614', 1), '202406140000001')
        self.assertEqual(normalize_ece('202406140000001'), '202406140000001')
        self.assertEqual(mask_ece('202406140000001'), '20240614***0001')
        with self.assertRaises(IclError) as ctx:
            compose_ece('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_ece')
        with self.assertRaises(IclError):
            normalize_ece('123')

    def test_onus_and_aba_reuse(self):
        self.assertEqual(parse_onus('1001/4321'), ('1001', '4321'))
        self.assertEqual(parse_onus('0000000000001001'), ('0000000000001001', ''))
        self.assertTrue(aba_check_digit_ok('021000021'))
        self.assertEqual(normalize_aba('21000021'), '021000021')

    def test_x937_roundtrip_and_file_split(self):
        chunks = split_x937_file(SAMPLE_FILE)
        self.assertEqual(len(chunks), 1)
        fields = parse_x937(chunks[0])
        self.assertEqual(fields['ece'], '202406140000001')
        message = message_from_x937(chunks[0])
        self.assertEqual(message['amount'], '100.00')
        self.assertEqual(message['payor_aba'], '021000021')
        self.assertEqual(message['drawer_account'], '1001')
        self.assertEqual(message['serial'], '4321')
        rebuilt = compose_x937({
            'ece': message['ece'],
            'payor_aba': message['payor_aba'],
            'on_us': message['drawer_account'],
            'serial': message['serial'],
            'amount': compose_amount_field(Decimal(message['amount'])),
            'bofd_aba': message['bofd_aba'],
            'payee_name': message['payee_name'],
        })
        again = message_from_x937(rebuilt)
        self.assertEqual(again['ece'], message['ece'])
        self.assertEqual(again['amount'], '100.00')

    def test_xml_and_type31_rejected(self):
        with self.assertRaises(IclError) as ctx:
            parse_x937('<?xml version="1.0"?><icl/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(IclError) as ctx:
            parse_x937('31|Return|202406140000001|A|021000021|1001|0000010000|011000015|NSF')
        self.assertEqual(ctx.exception.code, 'invalid_type')

    def test_image_fingerprint_stable_and_distinct(self):
        one = image_fingerprint('202406140000001', front=True, rear=True, payload='')
        two = image_fingerprint('202406140000001', front=True, rear=True, payload='')
        other = image_fingerprint('202406140000002', front=True, rear=True, payload='')
        self.assertEqual(one, two)
        self.assertNotEqual(one, other)
        self.assertEqual(len(one), 64)

    def test_return_compose_uses_x9_reason(self):
        from utility.icl import InboundIcl
        row = InboundIcl(
            inbound_id='x', ece='202406140000001', userid='alice', internal_account='1001',
            amount='100.00', payor_aba='021000021', bofd_aba='011000015', payee_name='Ada',
            drawer_account='1001', serial='4321', type_code='25', status='posted',
            value_date='20240614', actor='teller', releaser='', ofac_hit=0, ofac_match='',
            return_reason='', return_record='', image_fingerprint='', image_present=1,
            created_at=1, updated_at=1,
        )
        raw = compose_return_x937(row, reason='nsf')
        self.assertTrue(raw.startswith('31|Return|202406140000001|A|'))


class IclServiceTests(unittest.TestCase):
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

        def lookup_fn(account):
            return {'1001': 'alice', '1002': 'alice', '1003': 'alice'}.get(str(account))

        policy = IclPolicy(dual_control_threshold=Decimal('10000.00'), cutoff_hour=14)
        self.service = IclService(
            policy,
            MemoryIclStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            lookup_fn=lookup_fn,
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )

    def _payload(self, **overrides):
        body = {
            'ece': '202406140000001',
            'payor_aba': '021000021',
            'bofd_aba': '011000015',
            'drawer_account': '1001',
            'serial': '4321',
            'amount': '100.00',
            'payee_name': 'Ada Lovelace',
        }
        body.update(overrides)
        return body

    def test_snapshot_never_leaks_drawer_or_fingerprint(self):
        row, created = self.service.ingest(
            actor='teller', actor_type='tier1', values=self._payload(),
        )
        self.assertTrue(created)
        payload = row.to_dict()
        self.assertNotIn('drawer_account', payload)
        self.assertNotIn('image_fingerprint', payload)
        self.assertEqual(payload['drawer_last4'], '1001')
        self.assertEqual(payload['ece_masked'], '20240614***0001')
        self.assertNotEqual(payload['ece'], '202406140000001')
        self.assertEqual(row.status, 'posted')
        self.assertEqual(self.debits[0], ('1001', '100.00', 'icl 406140000001'))
        snap = self.service.snapshot('alice')
        self.assertNotIn('drawer_account', snap['inbounds'][0])
        self.assertNotIn('image_fingerprint', snap['inbounds'][0])

    def test_duplicate_ece_is_idempotent(self):
        first, created = self.service.ingest(
            actor='teller', actor_type='tier1', values=self._payload(),
        )
        self.assertTrue(created)
        second, created = self.service.ingest(
            actor='teller', actor_type='tier1', values=self._payload(),
        )
        self.assertFalse(created)
        self.assertEqual(first.inbound_id, second.inbound_id)
        self.assertEqual(len(self.debits), 1)

    def test_wrong_receiver(self):
        with self.assertRaises(IclError) as ctx:
            self.service.ingest(
                actor='teller', actor_type='tier1',
                values=self._payload(payor_aba='011000015'),
            )
        self.assertEqual(ctx.exception.code, 'wrong_receiver')

    def test_unmatched_assign_and_credit_blocked(self):
        row, created = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(drawer_account='9999', ece='202406140000002'),
        )
        self.assertTrue(created)
        self.assertEqual(row.status, 'unmatched')
        self.assertEqual(self.debits, [])
        assigned = self.service.assign(
            inbound_id=row.inbound_id, actor='teller', actor_type='tier1',
            customer_id='alice', internal_account='1001',
        )
        self.assertEqual(assigned.status, 'posted')
        self.assertEqual(assigned.userid, 'alice')

        blocked, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(drawer_account='1003', ece='202406140000003'),
        )
        self.assertEqual(blocked.status, 'unmatched')
        self.assertEqual(blocked.note, 'credit_not_allowed')

    def test_ofac_hold_then_override(self):
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(payee_name='Blocked Person', ece='202406140000004'),
        )
        self.assertEqual(row.status, 'held')
        self.assertEqual(self.debits, [])
        released = self.service.override_ofac(
            inbound_id=row.inbound_id, actor='boss', actor_type='tier2',
        )
        self.assertEqual(released.status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_dual_control_same_approver(self):
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(amount='15000.00', ece='202406140000005'),
        )
        self.assertEqual(row.status, 'pending_release')
        with self.assertRaises(IclError) as ctx:
            self.service.release(inbound_id=row.inbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        posted = self.service.release(inbound_id=row.inbound_id, actor='boss', actor_type='tier2')
        self.assertEqual(posted.status, 'posted')

    def test_cutoff_queue_and_run_due(self):
        self.now[0] = ts(2024, 6, 14, 15, 0)
        row, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(ece='202406140000006'),
        )
        self.assertEqual(row.status, 'queued')
        self.assertEqual(self.debits, [])
        self.now[0] = ts(2024, 6, 17, 11, 0)
        posted = self.service.run_due('alice')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].status, 'posted')
        self.assertEqual(len(self.debits), 1)

    def test_customer_return_before_and_after_post(self):
        held, _ = self.service.ingest(
            actor='teller', actor_type='tier1',
            values=self._payload(payee_name='Blocked Person', ece='202406140000007'),
        )
        returned = self.service.request_return(
            inbound_id=held.inbound_id, actor='alice', actor_type='customer', reason='stop',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertEqual(returned.return_reason, 'stop')
        self.assertTrue(returned.return_record.startswith('31|'))
        self.assertEqual(self.credits, [])

        posted, _ = self.service.ingest(
            actor='teller', actor_type='tier1', values=self._payload(ece='202406140000008'),
        )
        self.assertEqual(posted.status, 'posted')
        back = self.service.request_return(
            inbound_id=posted.inbound_id, actor='alice', actor_type='customer', reason='forged',
        )
        self.assertEqual(back.status, 'returned')
        self.assertEqual(self.credits[0], ('1001', '100.00', 'icl return 406140000008'))

    def test_return_window_closed(self):
        posted, _ = self.service.ingest(
            actor='teller', actor_type='tier1', values=self._payload(ece='202406140000009'),
        )
        self.now[0] = ts(2024, 6, 18, 11, 0)
        with self.assertRaises(IclError) as ctx:
            self.service.request_return(
                inbound_id=posted.inbound_id, actor='alice', actor_type='customer',
            )
        self.assertEqual(ctx.exception.code, 'return_window_closed')
        staff = self.service.return_inbound(
            inbound_id=posted.inbound_id, actor='boss', actor_type='tier2', reason='maker',
        )
        self.assertEqual(staff.status, 'returned')

    def test_nsf_on_presentment(self):
        def nsf_debit(account, amount, remark):
            return 'Insufficient Balance'

        self.service.debit_fn = nsf_debit
        with self.assertRaises(IclError) as ctx:
            self.service.ingest(actor='teller', actor_type='tier1', values=self._payload())
        self.assertEqual(ctx.exception.code, 'nsf')
        self.assertEqual(ctx.exception.extra['inbound'].status, 'failed')

    def test_customer_cannot_ingest(self):
        with self.assertRaises(IclError) as ctx:
            self.service.ingest(actor='alice', actor_type='customer', values=self._payload())
        self.assertEqual(ctx.exception.code, 'icl_forbidden')

    def test_ingest_file_and_sqlite_reopen(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteIclStore(path)
            service = IclService(
                IclPolicy(cutoff_hour=14),
                store,
                clock=lambda: self.now[0],
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                credit_fn=lambda account, amount, remark: 'Success',
                accounts_fn=lambda userid: {
                    'checkin': {'Account': 1001, 'Balance': 50},
                    'savings': {'Account': 1002, 'Balance': 10},
                    'credit': 'None',
                },
                lookup_fn=lambda account: 'alice' if str(account) == '1001' else None,
                calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
            )
            result = service.ingest_file(actor='teller', actor_type='tier1', text=SAMPLE_FILE)
            self.assertEqual(result['accepted_count'], 1)
            reopened = SqliteIclStore(path)
            found = reopened.get_by_ece('202406140000001')
            self.assertIsNotNone(found)
            self.assertEqual(found.status, 'posted')
            self.assertNotIn('drawer_account', found.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
