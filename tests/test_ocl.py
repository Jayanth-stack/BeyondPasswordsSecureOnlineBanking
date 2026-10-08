import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from utility.ocl import (
    MemoryOclStore,
    OclError,
    OclPolicy,
    OclService,
    SqliteOclStore,
    compose_amount_field,
    compose_ece,
    compose_return_x937,
    compose_x937,
    image_fingerprint,
    mask_ece,
    message_from_x937,
    parse_amount_field,
    parse_onus,
    parse_x937,
    split_x937_file,
)
from utility.wire import WireCalendar, money_str, screen_name

ET = timezone(timedelta(hours=-4))
PAYOR = '026009593'
BOFD = '021000021'


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


class FoundationTests(unittest.TestCase):
    def test_amount_field_is_10_digit_cents(self):
        self.assertEqual(compose_amount_field(Decimal('12.35')), '0000001235')
        self.assertEqual(parse_amount_field('0000001235'), Decimal('12.35'))
        self.assertEqual(money_str(parse_amount_field('$40.00')), '40.00')
        with self.assertRaises(OclError) as ctx:
            parse_amount_field('')
        self.assertEqual(ctx.exception.code, 'invalid_amount')

    def test_ece_layout_and_mask(self):
        self.assertEqual(compose_ece('20240614', 1), '202406140000001')
        self.assertEqual(mask_ece('202406140000001'), '20240614***0001')
        with self.assertRaises(OclError) as ctx:
            compose_ece('2024-06-14', 1)
        self.assertEqual(ctx.exception.code, 'invalid_ece')

    def test_onus_and_x937_roundtrip_rejects_xml_and_type31(self):
        self.assertEqual(parse_onus('77881234/1001'), ('77881234', '1001'))
        raw = compose_x937({
            'ece': '202406140000001',
            'aux_on_us': '',
            'payor_aba': PAYOR,
            'on_us': '77881234',
            'serial': '1001',
            'amount': '0000004000',
            'bofd_aba': BOFD,
            'payee_name': 'ADA LOVELACE',
        })
        fields = parse_x937(raw)
        self.assertEqual(fields['payor_aba'], PAYOR)
        self.assertEqual(fields['bofd_aba'], BOFD)
        msg = message_from_x937(raw)
        self.assertEqual(msg['amount'], '40.00')
        self.assertTrue(msg['image_fingerprint'])
        self.assertNotIn(msg['image_fingerprint'], raw)
        with self.assertRaises(OclError) as ctx:
            parse_x937('<?xml version="1.0"?><icl/>')
        self.assertEqual(ctx.exception.code, 'invalid_file')
        with self.assertRaises(OclError) as ctx:
            parse_x937('31|Return|202406140000001|A')
        self.assertEqual(ctx.exception.code, 'invalid_type')
        chunks = split_x937_file(raw + '\n' + raw.replace('0000001', '0000002'))
        self.assertEqual(len(chunks), 2)
        ret = compose_return_x937(
            ece='202406140000001', payor_aba=PAYOR, drawer_last4='1234',
            amount=Decimal('40.00'), bofd_aba=BOFD, reason='nsf',
        )
        self.assertTrue(ret.startswith('31|Return|202406140000001|A|'))

    def test_image_fingerprint_never_embeds_bytes(self):
        digest = image_fingerprint('202406140000001', front=True, rear=True, payload='not-bytes')
        self.assertEqual(len(digest), 64)
        self.assertNotIn('not-bytes', digest)

    def test_ofac_hits_phrase(self):
        hit = screen_name('Mr Blocked Person LLC')
        self.assertTrue(hit.hit)


class OclServiceTests(unittest.TestCase):
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

        policy = OclPolicy(
            outbound_fee=Decimal('1.00'),
            dual_control_threshold=Decimal('10000.00'),
            bofd_aba=BOFD,
            cutoff_hour=14,
        )
        self.service = OclService(
            policy,
            MemoryOclStore(),
            clock=lambda: self.now[0],
            debit_fn=debit_fn,
            credit_fn=credit_fn,
            accounts_fn=accounts_fn,
            calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
        )

    def _add(self, nickname='Payroll check', name='Ada Lovelace', **kwargs):
        return self.service.add_profile(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            nickname=nickname,
            payee_name=name,
            payor_aba=kwargs.pop('payor_aba', PAYOR),
            drawer_account=kwargs.pop('drawer_account', '77881234'),
            serial=kwargs.pop('serial', '1001'),
            default_account=kwargs.pop('default_account', '1001'),
            **kwargs,
        )

    def test_snapshot_and_to_dict_never_leak_drawer_ece_or_fingerprint(self):
        profile = self._add()
        payload = profile.to_dict()
        self.assertNotIn('drawer_account', payload)
        self.assertEqual(payload['drawer_last4'], '1234')
        self.assertEqual(payload['payor_aba'], PAYOR)
        snap = self.service.snapshot('alice')
        self.assertNotIn('drawer_account', snap['profiles'][0])
        item, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='40.00', trace_id='c1',
        )
        self.assertTrue(created)
        body = item.to_dict()
        self.assertNotIn('drawer_account', body)
        self.assertNotIn('image_fingerprint', body)
        self.assertNotIn('raw_x937', body)
        self.assertNotIn('ece', body)
        self.assertTrue(body['ece_masked'].startswith('20240614***'))
        self.assertEqual(item.status, 'submitted')
        self.assertEqual(self.credits[0], ('1001', '40.00', 'icl from Payroll check'))
        self.assertEqual(self.debits[0][0], '1001')
        self.assertEqual(self.debits[0][1], '1.00')
        self.assertIn('25|Check|', item.raw_x937)
        self.assertIn(PAYOR, item.raw_x937)
        self.assertIn(BOFD, item.raw_x937)

    def test_same_day_send_is_idempotent_by_trace(self):
        profile = self._add()
        first, created = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='40.00', trace_id='dup',
        )
        again, created_again = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='40.00', trace_id='dup',
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.outbound_id, again.outbound_id)
        self.assertEqual(len(self.credits), 1)

    def test_after_cutoff_queues_until_next_business_day(self):
        self.now[0] = ts(2024, 6, 14, 14, 30)
        profile = self._add()
        item, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='50.00', trace_id='late',
        )
        self.assertEqual(item.status, 'queued')
        self.assertEqual(item.value_date, '20240617')
        self.assertEqual(self.credits, [])
        self.assertEqual(item.ece, '')
        self.now[0] = ts(2024, 6, 17, 10, 0)
        due = self.service.run_due('alice')
        self.assertEqual(due[0].status, 'submitted')
        self.assertEqual(self.credits[0], ('1001', '50.00', 'icl from Payroll check'))
        self.assertTrue(due[0].ece.startswith('20240617'))

    def test_ofac_hold_blocks_credit_until_staff_override(self):
        profile = self._add(nickname='Blocked', name='Blocked Person')
        item, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='80.00', trace_id='ofac',
        )
        self.assertEqual(item.status, 'held')
        self.assertTrue(item.ofac_hit)
        self.assertEqual(self.credits, [])
        with self.assertRaises(OclError) as ctx:
            self.service.release_outbound(outbound_id=item.outbound_id, actor='teller', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'ofac_hold')
        released = self.service.override_ofac(
            outbound_id=item.outbound_id, actor='teller', actor_type='tier1', note='cleared',
        )
        self.assertEqual(released.status, 'submitted')
        self.assertEqual(self.credits[0][1], '80.00')

    def test_dual_control_requires_a_different_employee(self):
        profile = self._add()
        item, _ = self.service.originate(
            owner_userid='alice', actor='maker', actor_type='tier1',
            profile_id=profile.profile_id, amount='10000.00', trace_id='hv',
        )
        self.assertEqual(item.status, 'pending_release')
        self.assertEqual(self.credits, [])
        with self.assertRaises(OclError) as ctx:
            self.service.release_outbound(outbound_id=item.outbound_id, actor='maker', actor_type='tier1')
        self.assertEqual(ctx.exception.code, 'same_approver')
        released = self.service.release_outbound(outbound_id=item.outbound_id, actor='checker', actor_type='tier2')
        self.assertEqual(released.status, 'submitted')
        self.assertEqual(released.releaser, 'checker')
        self.assertEqual(self.credits[0][1], '10000.00')

    def test_on_us_payor_and_credit_account_and_pause(self):
        with self.assertRaises(OclError) as ctx:
            self._add(payor_aba=BOFD)
        self.assertEqual(ctx.exception.code, 'on_us_not_allowed')
        profile = self._add()
        with self.assertRaises(OclError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                profile_id=profile.profile_id, amount='20.00',
                internal_account='1003',
            )
        self.assertEqual(ctx.exception.code, 'credit_not_allowed')
        paused = self.service.set_profile_status(
            profile_id=profile.profile_id, actor='alice', actor_type='customer', status='pause',
        )
        self.assertEqual(paused.status, 'paused')
        with self.assertRaises(OclError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                profile_id=profile.profile_id, amount='20.00',
            )
        self.assertEqual(ctx.exception.code, 'profile_paused')

    def test_failed_credit_does_not_assign_ece(self):
        def boom(account, amount, remark):
            self.credits.append((account, amount, remark))
            return 'fail'

        self.service.credit_fn = boom
        profile = self._add()
        with self.assertRaises(OclError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                profile_id=profile.profile_id, amount='30.00', trace_id='nsf-1',
            )
        self.assertEqual(ctx.exception.code, 'failed')
        self.assertEqual(ctx.exception.extra['outbound'].status, 'failed')
        self.assertEqual(ctx.exception.extra['outbound'].ece, '')

    def test_complete_recall_and_type31_return(self):
        profile = self._add()
        item, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='60.00', trace_id='c1',
        )
        completed = self.service.complete_outbound(
            outbound_id=item.outbound_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(completed.status, 'completed')
        with self.assertRaises(OclError) as ctx:
            self.service.recall_outbound(outbound_id=item.outbound_id, actor='teller', actor_type='tier2')
        self.assertEqual(ctx.exception.code, 'already_completed')

        returned = self.service.return_outbound(
            outbound_id=item.outbound_id, actor='teller', actor_type='tier2', reason='nsf',
        )
        self.assertEqual(returned.status, 'returned')
        self.assertTrue(returned.return_record.startswith('31|Return|'))
        clawbacks = [row for row in self.debits if row[1] == '60.00']
        self.assertEqual(clawbacks[0][2].startswith('icl return '), True)

        other, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=self._add(nickname='Rent', serial='2002').profile_id,
            amount='15.00', trace_id='c2',
        )
        recalled = self.service.recall_outbound(
            outbound_id=other.outbound_id, actor='teller', actor_type='tier2',
        )
        self.assertEqual(recalled.status, 'recalled')
        self.assertEqual([row for row in self.debits if row[1] == '15.00'][0][1], '15.00')

    def test_customer_can_cancel_queued_not_submitted(self):
        self.now[0] = ts(2024, 6, 14, 15, 0)
        profile = self._add()
        item, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='22.00', trace_id='q1',
        )
        cancelled = self.service.cancel_outbound(
            outbound_id=item.outbound_id, actor='alice', actor_type='customer',
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = ts(2024, 6, 14, 11, 0)
        live, _ = self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='22.00', trace_id='live2',
        )
        self.assertEqual(live.status, 'submitted')
        with self.assertRaises(OclError) as ctx:
            self.service.cancel_outbound(outbound_id=live.outbound_id, actor='alice', actor_type='customer')
        self.assertEqual(ctx.exception.code, 'not_cancelable')

    def test_duplicate_micr_presentment_rejected(self):
        profile = self._add()
        self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='10.00', trace_id='p1',
        )
        with self.assertRaises(OclError) as ctx:
            self.service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                profile_id=profile.profile_id, amount='10.00', trace_id='p2',
            )
        self.assertEqual(ctx.exception.code, 'already_presented')

    def test_export_file_is_type25_and_deposit_check_untouched(self):
        profile = self._add()
        self.service.originate(
            owner_userid='alice', actor='alice', actor_type='customer',
            profile_id=profile.profile_id, amount='18.00', trace_id='x1',
        )
        blob = self.service.export_file('alice')
        self.assertIn('25|Check|', blob)
        self.assertIn('50|Image|', blob)
        parsed = parse_x937(blob)
        self.assertEqual(parsed['payor_aba'], PAYOR)

    def test_sqlite_roundtrip_masks_drawer(self):
        handle, path = tempfile.mkstemp(suffix='.sqlite')
        os.close(handle)
        try:
            store = SqliteOclStore(path)
            service = OclService(
                OclPolicy(bofd_aba=BOFD, outbound_fee=Decimal('1.00')),
                store,
                clock=lambda: self.now[0],
                accounts_fn=lambda userid: {'checkin': {'Account': 1001, 'Balance': 10}},
                credit_fn=lambda account, amount, remark: 'Success',
                debit_fn=lambda account, amount, remark: 'Amount Debited',
                calendar=WireCalendar(cutoff_hour=14, tz_offset_hours=-4),
            )
            profile = service.add_profile(
                owner_userid='alice', actor='alice', actor_type='customer',
                nickname='BoA', payee_name='Ada Lovelace', payor_aba=PAYOR,
                drawer_account='99887766', serial='55', default_account='1001',
            )
            reloaded = SqliteOclStore(path).get_profile(profile.profile_id)
            self.assertEqual(reloaded.drawer_account, '99887766')
            self.assertNotIn('drawer_account', reloaded.to_dict())
            self.assertEqual(reloaded.to_dict()['drawer_last4'], '7766')
            item, _ = service.originate(
                owner_userid='alice', actor='alice', actor_type='customer',
                profile_id=profile.profile_id, amount='9.00', trace_id='sql1',
            )
            again = SqliteOclStore(path).get_outbound(item.outbound_id)
            self.assertTrue(again.ece)
            self.assertNotIn('ece', again.to_dict())
            self.assertNotIn('image_fingerprint', again.to_dict())
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
