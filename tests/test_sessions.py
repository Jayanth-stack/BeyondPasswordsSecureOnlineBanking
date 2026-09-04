"""Unit tests for the session store, policy, device registry, and timeouts."""

import os
import tempfile
import unittest

from utility.sessions import (
    MemorySessionStore,
    SessionPolicy,
    SessionRecord,
    SessionService,
    SqliteSessionStore,
    device_key,
    device_label,
    handle_list_request,
    handle_revoke_others_request,
    handle_revoke_request,
    resolve_secret_key,
    set_service,
)


class FrozenClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class DeviceLabelTests(unittest.TestCase):
    def test_chrome_linux(self):
        ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        self.assertEqual(device_label(ua), 'Chrome on Linux')
        self.assertEqual(len(device_key(ua)), 16)

    def test_same_family_shares_key(self):
        a = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'
        b = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0.0.0'
        self.assertEqual(device_key(a), device_key(b))


class SecretKeyTests(unittest.TestCase):
    def test_creates_and_reuses_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '.session_secret')
            os.environ.pop('SECRET_KEY', None)
            first = resolve_secret_key(path)
            second = resolve_secret_key(path)
            self.assertEqual(first, second)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_env_wins(self):
        os.environ['SECRET_KEY'] = 'from-env'
        try:
            self.assertEqual(resolve_secret_key('/does/not/matter'), 'from-env')
        finally:
            os.environ.pop('SECRET_KEY', None)


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FrozenClock()
        self.store = MemorySessionStore()
        self.svc = SessionService(
            store=self.store,
            policy=SessionPolicy(idle_seconds=60, absolute_seconds=300, max_concurrent=2),
            clock=self.clock,
        )

    def _save(self, sid, userid='alice', usertype='customer', ua='Chrome/120 Linux', ip='10.0.0.1'):
        record = self.svc.create(sid)
        class Req:
            remote_addr = ip
            headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) %s' % ua}
        self.svc.persist(record, {'userid': userid, 'usertype': usertype}, request=Req())
        return record.sid

    def test_idle_expiry(self):
        sid = self._save('sid-idle')
        self.assertIsNotNone(self.svc.load(sid))
        self.clock.advance(61)
        self.assertIsNone(self.svc.load(sid))

    def test_absolute_expiry_even_if_touched(self):
        sid = self._save('sid-abs')
        for _ in range(4):
            self.clock.advance(50)
            record = self.svc.load(sid)
            self.assertIsNotNone(record)
            self.svc.persist(record, record.data)
        self.clock.advance(120)
        self.assertIsNone(self.svc.load(sid))

    def test_new_device_then_known(self):
        self._save('sid-a')
        rec = self.store.get('sid-a')
        self.assertTrue(rec.new_device)
        self._save('sid-b')
        rec_b = self.store.get('sid-b')
        self.assertFalse(rec_b.new_device)

    def test_concurrent_revokes_oldest(self):
        self._save('s1')
        self.clock.advance(1)
        self._save('s2')
        self.clock.advance(1)
        self._save('s3')
        self.assertIsNone(self.svc.load('s1'))
        self.assertIsNotNone(self.svc.load('s2'))
        self.assertIsNotNone(self.svc.load('s3'))
        self.assertEqual(len(self.svc.list_for('alice')), 2)

    def test_employee_limit_is_separate(self):
        svc = SessionService(
            store=MemorySessionStore(),
            policy=SessionPolicy(idle_seconds=60, absolute_seconds=300, max_concurrent=1, max_concurrent_employee=3),
            clock=self.clock,
        )
        self.svc = svc
        self.store = svc.store
        self._save('e1', userid='teller', usertype='employee')
        self._save('e2', userid='teller', usertype='employee')
        self._save('e3', userid='teller', usertype='employee')
        self.assertEqual(len(svc.list_for('teller')), 3)

    def test_revoke_and_revoke_others(self):
        self._save('s1')
        self.clock.advance(1)
        self._save('s2')
        self.svc.revoke('s1', 'alice')
        self.assertIsNone(self.svc.load('s1'))
        self._save('s3')
        n = self.svc.revoke_others('alice', 's3')
        self.assertEqual(n, 1)
        self.assertIsNone(self.svc.load('s2'))
        self.assertIsNotNone(self.svc.load('s3'))

    def test_cannot_revoke_someone_elses_session(self):
        self._save('s1', userid='alice')
        from utility.sessions import SessionError
        with self.assertRaises(SessionError) as ctx:
            self.svc.revoke('s1', 'bob')
        self.assertEqual(ctx.exception.status, 404)


class SqliteRestartTests(unittest.TestCase):
    def test_reopen_sees_session_and_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sessions.sqlite')
            clock = FrozenClock()
            store = SqliteSessionStore(path)
            svc = SessionService(store=store, policy=SessionPolicy(idle_seconds=60, absolute_seconds=300), clock=clock)
            record = svc.create('persist-me')

            class Req:
                remote_addr = '127.0.0.1'
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Safari/537.36'}

            svc.persist(record, {'userid': 'alice', 'usertype': 'customer'}, request=Req())
            store2 = SqliteSessionStore(path)
            svc2 = SessionService(store=store2, policy=SessionPolicy(idle_seconds=60, absolute_seconds=300), clock=clock)
            loaded = svc2.load('persist-me')
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.userid, 'alice')
            self.assertEqual(loaded.device_label, 'Safari on macOS')
            self.assertIsNotNone(store2.get_device('alice', loaded.device_key))


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FrozenClock()
        self.svc = SessionService(
            store=MemorySessionStore(),
            policy=SessionPolicy(idle_seconds=60, absolute_seconds=300, max_concurrent=3),
            clock=self.clock,
        )
        set_service(self.svc)

        class Sess(dict):
            def __init__(self, sid, **kwargs):
                super().__init__(**kwargs)
                self.sid = sid

            def clear(self):
                dict.clear(self)

        self.Sess = Sess
        rec = self.svc.create('cur')
        class Req:
            remote_addr = '10.1.1.1'
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0) Chrome/120.0'}
        self.svc.persist(rec, {'userid': 'alice', 'usertype': 'customer'}, request=Req())
        rec2 = self.svc.create('other')
        self.clock.advance(1)
        self.svc.persist(rec2, {'userid': 'alice', 'usertype': 'customer'}, request=Req())
        self.sess = Sess('cur', userid='alice', usertype='customer', _new_device=True)

    def tearDown(self):
        set_service(None)

    def test_list_requires_auth(self):
        result = handle_list_request({}, {})
        self.assertEqual(result['status'], 401)

    def test_list_rejects_mismatch(self):
        result = handle_list_request(self.sess, {'userid': 'bob'})
        self.assertEqual(result['status'], 403)

    def test_list_ok(self):
        result = handle_list_request(self.sess, {'userid': 'alice'})
        self.assertEqual(result['status'], 200)
        self.assertEqual(len(result['body']['sessions']), 2)
        self.assertTrue(result['body']['new_device'])

    def test_revoke_other_and_current(self):
        result = handle_revoke_request(self.sess, {'sid': 'other'})
        self.assertEqual(result['status'], 200)
        self.assertFalse(result['body']['current'])
        result = handle_revoke_others_request(self.sess, {})
        self.assertEqual(result['status'], 200)
        result = handle_revoke_request(self.sess, {'sid': 'cur'})
        self.assertEqual(result['status'], 200)
        self.assertTrue(result['body']['current'])
        self.assertNotIn('userid', self.sess)


if __name__ == '__main__':
    unittest.main()
