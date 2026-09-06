import os
import tempfile
import unittest

from utility.notify import (
    AmountError,
    Contact,
    FailingChannel,
    MemoryNotifyStore,
    NotifyPolicy,
    NotifyService,
    RecordingChannel,
    SqliteNotifyStore,
    device_key,
    ip_network,
    mask_account,
    mask_ip,
    parse_money,
    parse_user_agent,
)


def service(policy=None, channels=None, contacts=None, store=None):
    people = contacts or {}

    def resolve(userid, usertype):
        return people.get(userid, Contact(userid=userid, usertype=usertype, email=f'{userid}@bank.test',
                                          phone='+15555550100'))

    return NotifyService(
        store=store or MemoryNotifyStore(),
        policy=policy or NotifyPolicy(cooldown_seconds=0),
        channels=channels or [RecordingChannel('email'), RecordingChannel('sms')],
        contact_resolver=resolve,
    )


class ParseHelpersTest(unittest.TestCase):
    def test_parse_money_rejects_scientific_and_zero(self):
        for bad in (None, True, '1e2', '1.001', '0', '-5', '+12', ''):
            with self.assertRaises(AmountError):
                parse_money(bad)
        self.assertEqual(str(parse_money('500.00')), '500.00')

    def test_user_agent_and_ip(self):
        self.assertEqual(parse_user_agent('Mozilla/5.0 (Windows NT 10.0) Chrome/120.0'), ('Chrome', 'Windows'))
        self.assertEqual(parse_user_agent('Mozilla/5.0 (Macintosh) Version/17 Safari/605'), ('Safari', 'macOS'))
        self.assertEqual(ip_network('203.0.113.45'), '203.0.113.0')
        self.assertEqual(ip_network('203.0.113.45, 10.0.0.1'), '203.0.113.0')
        self.assertEqual(mask_ip('203.0.113.45'), '203.0.113.x')
        self.assertEqual(mask_account('1234567890'), '7890')

    def test_device_key_stable_across_same_family(self):
        first = device_key('ada', 'Chrome/120 Windows', '203.0.113.10')
        second = device_key('ada', 'Mozilla Chrome/121 Windows NT', '203.0.113.99')
        other = device_key('ada', 'Firefox/120 Windows', '203.0.113.10')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


class NotifyServiceTest(unittest.TestCase):
    def test_new_device_then_known_skip(self):
        svc = service()
        first = svc.note_login_success('ada', ip='203.0.113.10', user_agent='Chrome/120 Windows')
        self.assertIsNotNone(first)
        self.assertEqual(first.event, 'new_device')
        self.assertIn('inbox', first.channels)
        second = svc.note_login_success('ada', ip='203.0.113.11', user_agent='Chrome/121 Windows')
        self.assertIsNone(second)
        self.assertEqual(svc.store.unread_count('ada'), 1)

    def test_employee_skipped_by_default(self):
        svc = service()
        item = svc.emit('password_reset', userid='teller1', usertype='tier1')
        self.assertIsNone(item)

    def test_login_failure_threshold(self):
        svc = service(policy=NotifyPolicy(login_failure_threshold=3, cooldown_seconds=0))
        self.assertIsNone(svc.note_login_failure('ada', ip='1.1.1.1'))
        self.assertIsNone(svc.note_login_failure('ada', ip='1.1.1.1'))
        item = svc.note_login_failure('ada', ip='1.1.1.1')
        self.assertIsNotNone(item)
        self.assertEqual(item.event, 'login_failure')
        self.assertEqual(item.details['count'], 3)

    def test_success_clears_failures(self):
        svc = service(policy=NotifyPolicy(login_failure_threshold=3, cooldown_seconds=0))
        svc.note_login_failure('ada')
        svc.note_login_failure('ada')
        svc.note_login_success('ada', ip='203.0.113.8', user_agent='Firefox Linux')
        self.assertIsNone(svc.note_login_failure('ada'))

    def test_high_value_threshold(self):
        svc = service()
        self.assertIsNone(svc.emit_if_high_value('ada', 'customer', 'transfer', '499.99', '1111', '2222'))
        item = svc.emit_if_high_value('ada', 'customer', 'transfer', '500.00', '11112222', '33334444')
        self.assertIsNotNone(item)
        self.assertEqual(item.event, 'high_value')
        self.assertEqual(item.details['amount'], '500.00')
        self.assertEqual(item.details['from_account'], '2222')
        self.assertNotIn('11112222', item.body)

    def test_prefs_mute_event(self):
        svc = service()
        svc.update_prefs('ada', events=['new_device', 'login_failure'])
        self.assertIsNone(svc.emit('password_reset', userid='ada'))
        self.assertIsNotNone(svc.emit('new_device', userid='ada', ip='1.2.3.4', user_agent='Chrome Linux'))

    def test_email_pref_skips_email_channel(self):
        email = RecordingChannel('email')
        sms = RecordingChannel('sms')
        svc = service(channels=[email, sms])
        svc.update_prefs('ada', email_enabled=False, sms_enabled=True)
        svc.emit('password_reset', userid='ada')
        self.assertEqual(email.sent, [])
        self.assertEqual(len(sms.sent), 1)

    def test_cooldown_suppresses_duplicate(self):
        svc = service(policy=NotifyPolicy(cooldown_seconds=60))
        first = svc.emit('password_reset', userid='ada', fingerprint='reset')
        second = svc.emit('password_reset', userid='ada', fingerprint='reset')
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_failing_channel_still_writes_inbox(self):
        svc = service(channels=[FailingChannel('email')])
        item = svc.emit('profile_change', userid='ada')
        self.assertIsNotNone(item)
        self.assertEqual(item.status, 'inbox')
        self.assertTrue(any(ch.endswith(':failed') for ch in item.channels))
        self.assertEqual(svc.store.unread_count('ada'), 1)

    def test_disabled_policy(self):
        svc = service(policy=NotifyPolicy(enabled=False))
        self.assertIsNone(svc.emit('new_device', userid='ada'))

    def test_emit_quietly_swallows(self):
        class BoomStore(MemoryNotifyStore):
            def save_notification(self, item):
                raise RuntimeError('disk')

        svc = service(store=BoomStore())
        self.assertIsNone(svc.emit_quietly('password_reset', userid='ada', usertype='customer'))

    def test_mark_read_and_unknown(self):
        svc = service()
        item = svc.emit('profile_change', userid='ada')
        self.assertEqual(svc.mark_read('ada', item.notification_id), 1)
        self.assertEqual(svc.store.unread_count('ada'), 0)
        with self.assertRaises(Exception) as ctx:
            svc.mark_read('ada', 'missing')
        self.assertEqual(ctx.exception.code, 'notification_not_found')

    def test_sqlite_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'notify.sqlite')
            store = SqliteNotifyStore(path)
            svc = service(store=store)
            svc.note_login_success('ada', ip='198.51.100.4', user_agent='Safari Mac OS X')
            reopened = NotifyService(
                store=SqliteNotifyStore(path),
                policy=NotifyPolicy(cooldown_seconds=0),
                channels=[RecordingChannel('email')],
                contact_resolver=lambda u, t: Contact(userid=u, email='a@b.c'),
            )
            self.assertEqual(reopened.store.unread_count('ada'), 1)
            again = reopened.note_login_success('ada', ip='198.51.100.9', user_agent='Safari Macintosh')
            self.assertIsNone(again)

    def test_details_never_store_secrets(self):
        svc = service()
        item = svc.emit('password_reset', userid='ada', details={'otp': '123456', 'newPassword': 'Secret1'})
        self.assertNotIn('otp', item.details)
        self.assertNotIn('newPassword', item.details)


if __name__ == '__main__':
    unittest.main()
