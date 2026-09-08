import os
import tempfile
import unittest

from utility.schedule import (
    AccountError,
    AmountError,
    MemoryScheduleStore,
    ScheduleError,
    SchedulePolicy,
    ScheduleService,
    SqliteScheduleStore,
    WhenError,
    add_calendar_months,
    canonical_amount,
    next_occurrence,
    normalize_account,
    normalize_interval,
    occurrence_id_for,
    own_accounts_from_customer_payload,
    parse_money,
    parse_when,
)


class ScheduleCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_000.0]
        self.executed = []
        self.policy = SchedulePolicy(min_lead_seconds=60, max_open=5, max_amount=parse_money('5000.00'))
        self.service = ScheduleService(
            self.policy,
            MemoryScheduleStore(),
            clock=lambda: self.now[0],
            executor=self._exec,
        )

    def _exec(self, payload):
        self.executed.append(payload)
        return 'Request to be approved by tier1 employee'

    def create(self, **kwargs):
        defaults = dict(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            from_account='1001',
            to_account='2002',
            amount='10.00',
            interval='once',
            start_at=self.now[0] + 120,
            own_accounts=['1001', '1002'],
        )
        defaults.update(kwargs)
        return self.service.create(**defaults)

    def test_parse_money_and_canonical(self):
        self.assertEqual(canonical_amount('10.50'), '10.50')
        with self.assertRaises(AmountError):
            parse_money('-1')
        with self.assertRaises(AmountError):
            parse_money('1e2')

    def test_normalize_account_strips_float_suffix(self):
        self.assertEqual(normalize_account('1001.0'), '1001')
        with self.assertRaises(AccountError):
            normalize_account('abc')

    def test_parse_when_iso_and_epoch(self):
        self.assertEqual(parse_when(1500), 1500.0)
        stamp = parse_when('2026-01-15T12:00:00Z')
        self.assertEqual(stamp, parse_when('2026-01-15T12:00:00+00:00'))
        with self.assertRaises(WhenError):
            parse_when('not-a-date')

    def test_monthly_end_of_month(self):
        jan31 = parse_when('2026-01-31T12:00:00Z')
        feb = add_calendar_months(jan31, 1)
        self.assertEqual(parse_when('2026-02-28T12:00:00Z'), feb)
        self.assertGreater(next_occurrence(jan31, 'monthly'), jan31)

    def test_create_once_not_due_yet(self):
        schedule = self.create()
        self.assertEqual(schedule.status, 'active')
        self.assertEqual(schedule.interval, 'once')
        self.assertEqual(self.service.run_due(), [])
        self.assertEqual(self.executed, [])

    def test_due_once_runs_existing_handler_then_completes(self):
        schedule = self.create(start_at=self.now[0] + 60)
        self.now[0] = 1_070.0
        ran = self.service.run_due()
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0].status, 'settled')
        self.assertEqual(self.executed[0]['from_account'], '1001')
        self.assertEqual(self.executed[0]['to_account'], '2002')
        self.assertEqual(self.executed[0]['amount'], '10.00')
        stored = self.service.store.get_schedule(schedule.schedule_id)
        self.assertEqual(stored.status, 'completed')
        self.assertEqual(stored.run_count, 1)
        self.now[0] = 2_000.0
        self.assertEqual(self.service.run_due(), [])
        self.assertEqual(len(self.executed), 1)

    def test_recurring_daily_advances_next_run(self):
        schedule = self.create(interval='daily', start_at=self.now[0] + 60)
        self.now[0] = 1_070.0
        self.service.run_due()
        stored = self.service.store.get_schedule(schedule.schedule_id)
        self.assertEqual(stored.status, 'active')
        self.assertEqual(stored.run_count, 1)
        self.assertGreater(stored.next_run, self.now[0])
        self.service.run_due()
        self.assertEqual(len(self.executed), 1)
        self.now[0] = stored.next_run
        self.service.run_due()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self.service.store.get_schedule(schedule.schedule_id).run_count, 2)

    def test_catch_up_one_does_not_triple_charge(self):
        schedule = self.create(interval='daily', start_at=self.now[0] + 60)
        self.now[0] = 1_070.0 + 3 * 86400
        ran = self.service.run_due()
        self.assertEqual(len(self.executed), 1)
        stored = self.service.store.get_schedule(schedule.schedule_id)
        self.assertGreater(stored.next_run, self.now[0])
        self.assertEqual(ran[0].status, 'settled')

    def test_catch_up_all_fires_missed(self):
        service = ScheduleService(
            SchedulePolicy(min_lead_seconds=60, catch_up='all'),
            MemoryScheduleStore(),
            clock=lambda: self.now[0],
            executor=self._exec,
        )
        service.create(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            from_account='1001',
            to_account='2002',
            amount='1.00',
            interval='daily',
            start_at=self.now[0] + 60,
            own_accounts=['1001'],
        )
        self.now[0] = 1_070.0 + 2 * 86400
        service.run_due()
        self.assertEqual(len(self.executed), 3)

    def test_idempotent_occurrence(self):
        schedule = self.create(start_at=self.now[0] + 60)
        self.now[0] = 1_070.0
        self.service.run_due()
        occ_id = occurrence_id_for(schedule.schedule_id, schedule.next_run)
        self.assertIsNotNone(self.service.store.get_occurrence(occ_id))
        self.service.run_due()
        self.assertEqual(len(self.executed), 1)

    def test_cancel_then_not_due(self):
        schedule = self.create(start_at=self.now[0] + 60)
        cancelled = self.service.cancel(
            schedule_id=schedule.schedule_id, actor='alice', actor_type='customer', owner_userid='alice'
        )
        self.assertEqual(cancelled.status, 'cancelled')
        self.now[0] = 1_070.0
        self.assertEqual(self.service.run_due(), [])

    def test_pause_and_resume(self):
        schedule = self.create(interval='daily', start_at=self.now[0] + 60)
        paused = self.service.pause(
            schedule_id=schedule.schedule_id, actor='alice', actor_type='customer'
        )
        self.assertEqual(paused.status, 'paused')
        self.now[0] = 1_070.0
        self.assertEqual(self.service.run_due(), [])
        resumed = self.service.resume(
            schedule_id=schedule.schedule_id, actor='alice', actor_type='customer'
        )
        self.assertEqual(resumed.status, 'active')
        self.assertGreater(resumed.next_run, self.now[0])

    def test_customer_cannot_schedule_unowned_account(self):
        with self.assertRaises(ScheduleError) as ctx:
            self.create(from_account='9999')
        self.assertEqual(ctx.exception.code, 'schedule_forbidden')

    def test_employee_can_schedule_without_own_accounts(self):
        schedule = self.service.create(
            owner_userid='alice',
            actor='emp1',
            actor_type='tier1',
            from_account='1001',
            to_account='2002',
            amount='5.00',
            start_at=self.now[0] + 60,
        )
        self.assertEqual(schedule.actor, 'emp1')

    def test_duplicate_active(self):
        self.create()
        with self.assertRaises(ScheduleError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.code, 'schedule_duplicate')

    def test_same_account_rejected(self):
        with self.assertRaises(ScheduleError) as ctx:
            self.create(to_account='1001')
        self.assertEqual(ctx.exception.code, 'schedule_same_account')

    def test_amount_over_policy(self):
        with self.assertRaises(ScheduleError) as ctx:
            self.create(amount='5000.01')
        self.assertEqual(ctx.exception.code, 'per_txn_exceeded')

    def test_too_soon(self):
        with self.assertRaises(ScheduleError) as ctx:
            self.create(start_at=self.now[0] + 10)
        self.assertEqual(ctx.exception.code, 'schedule_too_soon')

    def test_max_open(self):
        for i in range(5):
            self.create(to_account=str(2002 + i), start_at=self.now[0] + 120 + i)
        with self.assertRaises(ScheduleError) as ctx:
            self.create(to_account='3000', start_at=self.now[0] + 200)
        self.assertEqual(ctx.exception.code, 'schedule_limit')

    def test_max_occurrences_completes(self):
        schedule = self.create(interval='daily', start_at=self.now[0] + 60, max_occurrences=2)
        self.now[0] = 1_070.0
        self.service.run_due()
        stored = self.service.store.get_schedule(schedule.schedule_id)
        self.now[0] = stored.next_run
        self.service.run_due()
        self.assertEqual(self.service.store.get_schedule(schedule.schedule_id).status, 'completed')
        self.assertEqual(len(self.executed), 2)

    def test_skip_on_error_advances(self):
        def boom(_payload):
            raise RuntimeError('mysql down')

        service = ScheduleService(
            SchedulePolicy(min_lead_seconds=60, skip_on_error=True),
            MemoryScheduleStore(),
            clock=lambda: self.now[0],
            executor=boom,
        )
        schedule = service.create(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            from_account='1001',
            to_account='2002',
            amount='1.00',
            interval='daily',
            start_at=self.now[0] + 60,
            own_accounts=['1001'],
        )
        self.now[0] = 1_070.0
        ran = service.run_due()
        self.assertEqual(ran[0].status, 'failed')
        stored = service.store.get_schedule(schedule.schedule_id)
        self.assertEqual(stored.status, 'active')
        self.assertGreater(stored.next_run, self.now[0])

    def test_fail_closed_marks_failed(self):
        def boom(_payload):
            raise RuntimeError('mysql down')

        service = ScheduleService(
            SchedulePolicy(min_lead_seconds=60, skip_on_error=False),
            MemoryScheduleStore(),
            clock=lambda: self.now[0],
            executor=boom,
        )
        schedule = service.create(
            owner_userid='alice',
            actor='alice',
            actor_type='customer',
            from_account='1001',
            to_account='2002',
            amount='1.00',
            start_at=self.now[0] + 60,
            own_accounts=['1001'],
        )
        self.now[0] = 1_070.0
        with self.assertRaises(RuntimeError):
            service.run_due()
        self.assertEqual(service.store.get_schedule(schedule.schedule_id).status, 'failed')

    def test_disabled_policy(self):
        service = ScheduleService(SchedulePolicy(enabled=False), MemoryScheduleStore(), clock=lambda: self.now[0])
        with self.assertRaises(ScheduleError) as ctx:
            service.create(
                owner_userid='alice',
                actor='alice',
                actor_type='customer',
                from_account='1001',
                to_account='2002',
                amount='1.00',
                own_accounts=['1001'],
            )
        self.assertEqual(ctx.exception.code, 'schedule_disabled')
        self.assertEqual(service.run_due(executor=self._exec), [])

    def test_bob_cannot_cancel_alice(self):
        schedule = self.create()
        with self.assertRaises(ScheduleError) as ctx:
            self.service.cancel(
                schedule_id=schedule.schedule_id, actor='bob', actor_type='customer', owner_userid='bob'
            )
        self.assertEqual(ctx.exception.code, 'schedule_forbidden')

    def test_employee_can_cancel(self):
        schedule = self.create()
        cancelled = self.service.cancel(
            schedule_id=schedule.schedule_id, actor='emp1', actor_type='tier2'
        )
        self.assertEqual(cancelled.status, 'cancelled')

    def test_snapshot_lists_open(self):
        self.create()
        snap = self.service.snapshot('alice')
        self.assertEqual(snap['open_count'], 1)
        self.assertEqual(len(snap['schedules']), 1)
        self.assertTrue(snap['enabled'])

    def test_normalize_interval_aliases(self):
        self.assertEqual(normalize_interval('7d'), 'weekly')
        self.assertEqual(normalize_interval('month'), 'monthly')
        with self.assertRaises(ScheduleError):
            normalize_interval('yearly')

    def test_own_accounts_helper(self):
        payload = {'savings': {'Account': 1001, 'Balance': 1}, 'checkin': 'None', 'credit': 'None'}
        self.assertEqual(own_accounts_from_customer_payload(payload), ['1001'])

    def test_sqlite_reopen(self):
        handle = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False)
        handle.close()
        path = handle.name
        try:
            store = SqliteScheduleStore(path)
            service = ScheduleService(
                SchedulePolicy(min_lead_seconds=0),
                store,
                clock=lambda: self.now[0],
                executor=self._exec,
            )
            created = service.create(
                owner_userid='alice',
                actor='alice',
                actor_type='customer',
                from_account='1001',
                to_account='2002',
                amount='2.00',
                start_at=self.now[0],
                own_accounts=['1001'],
            )
            reopened = SqliteScheduleStore(path)
            found = reopened.get_schedule(created.schedule_id)
            self.assertIsNotNone(found)
            self.assertEqual(found.amount, '2.00')
            service2 = ScheduleService(
                SchedulePolicy(min_lead_seconds=0),
                reopened,
                clock=lambda: self.now[0],
                executor=self._exec,
            )
            self.now[0] = 1_010.0
            ran = service2.run_due()
            self.assertEqual(len(ran), 1)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
