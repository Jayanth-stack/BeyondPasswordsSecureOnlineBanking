"""Tests for the idempotency store and service."""

import os
import tempfile
import unittest

from utility.idempotency import (
    IdempotencyKeyError,
    IdempotencyService,
    MemoryIdempotencyStore,
    SqliteIdempotencyStore,
    fingerprint_payload,
    validate_key,
)


class FingerprintTests(unittest.TestCase):
    def test_amount_canonicalized(self):
        a = fingerprint_payload({'amount': '10', 'fromAccount': 1})
        b = fingerprint_payload({'amount': '10.00', 'fromAccount': '1'})
        self.assertEqual(a, b)

    def test_idempotency_key_excluded(self):
        a = fingerprint_payload({'amount': '10.00', 'idempotency_key': 'aaa-aaa-a'})
        b = fingerprint_payload({'amount': '10.00', 'idempotency_key': 'bbb-bbb-b'})
        self.assertEqual(a, b)

    def test_different_amount_differs(self):
        a = fingerprint_payload({'amount': '10.00'})
        b = fingerprint_payload({'amount': '10.01'})
        self.assertNotEqual(a, b)

    def test_account_int_vs_string(self):
        a = fingerprint_payload({'fromAccount': 1001, 'toAccount': 2002, 'amount': '5.00'})
        b = fingerprint_payload({'fromAccount': '1001', 'toAccount': '2002', 'amount': '5.00'})
        self.assertEqual(a, b)


class KeyValidationTests(unittest.TestCase):
    def test_accepts_uuid(self):
        self.assertEqual(
            validate_key('550e8400-e29b-41d4-a716-446655440000'),
            '550e8400-e29b-41d4-a716-446655440000',
        )

    def test_rejects_short(self):
        with self.assertRaises(IdempotencyKeyError):
            validate_key('abc')

    def test_rejects_spaces(self):
        with self.assertRaises(IdempotencyKeyError):
            validate_key('not a valid key!!')


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class _ServiceMixin:
    def _service(self, store, pending_timeout=60, ttl=86_400):
        self.clock = _Clock()
        return IdempotencyService(
            store, ttl_seconds=ttl, pending_timeout=pending_timeout, clock=self.clock,
        )

    def test_first_begin_is_miss(self):
        svc = self._service(self.store)
        outcome = svc.begin('key-aaaa', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'miss')

    def test_replay_after_complete(self):
        svc = self._service(self.store)
        svc.begin('key-bbbb', 'alice:transfer', 'fp1')
        svc.complete('key-bbbb', 'alice:transfer', 200, '{"message":"queued"}')
        outcome = svc.begin('key-bbbb', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'replay')
        self.assertEqual(outcome.record.status, 200)
        self.assertIn('queued', outcome.record.body)

    def test_conflict_on_different_fingerprint(self):
        svc = self._service(self.store)
        svc.begin('key-cccc', 'alice:transfer', 'fp1')
        svc.complete('key-cccc', 'alice:transfer', 200, '{}')
        outcome = svc.begin('key-cccc', 'alice:transfer', 'fp2')
        self.assertEqual(outcome.kind, 'conflict')

    def test_in_progress_blocks_second_caller(self):
        svc = self._service(self.store)
        svc.begin('key-dddd', 'alice:transfer', 'fp1')
        outcome = svc.begin('key-dddd', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'in_progress')

    def test_release_allows_retry(self):
        svc = self._service(self.store)
        svc.begin('key-eeee', 'alice:transfer', 'fp1')
        svc.release('key-eeee', 'alice:transfer')
        outcome = svc.begin('key-eeee', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'miss')

    def test_stale_pending_same_fingerprint_taken_over(self):
        svc = self._service(self.store, pending_timeout=30)
        svc.begin('key-ffff', 'alice:transfer', 'fp1')
        self.clock.now += 31
        outcome = svc.begin('key-ffff', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'miss')

    def test_stale_pending_different_fingerprint_is_conflict(self):
        svc = self._service(self.store, pending_timeout=30)
        svc.begin('key-gggg', 'alice:transfer', 'fp1')
        self.clock.now += 31
        outcome = svc.begin('key-gggg', 'alice:transfer', 'fp2')
        self.assertEqual(outcome.kind, 'conflict')

    def test_scopes_are_isolated(self):
        svc = self._service(self.store)
        svc.begin('key-hhhh', 'alice:transfer', 'fp1')
        svc.complete('key-hhhh', 'alice:transfer', 200, '{"who":"alice"}')
        outcome = svc.begin('key-hhhh', 'bob:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'miss')

    def test_expired_record_is_pruned(self):
        svc = self._service(self.store, ttl=10)
        svc.begin('key-iiii', 'alice:transfer', 'fp1')
        svc.complete('key-iiii', 'alice:transfer', 200, '{}')
        self.clock.now += 11
        outcome = svc.begin('key-iiii', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'miss')


class MemoryIdempotencyTests(_ServiceMixin, unittest.TestCase):
    def setUp(self):
        self.store = MemoryIdempotencyStore()


class SqliteIdempotencyTests(_ServiceMixin, unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.sqlite')
        os.close(fd)
        os.unlink(self.path)
        self.store = SqliteIdempotencyStore(self.path)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_survives_reopen(self):
        svc = self._service(self.store)
        svc.begin('key-jjjj', 'alice:transfer', 'fp1')
        svc.complete('key-jjjj', 'alice:transfer', 200, '{"ok":true}')
        self.store.close()
        self.store = SqliteIdempotencyStore(self.path)
        svc = self._service(self.store)
        outcome = svc.begin('key-jjjj', 'alice:transfer', 'fp1')
        self.assertEqual(outcome.kind, 'replay')
        self.assertIn('ok', outcome.record.body)


if __name__ == '__main__':
    unittest.main()
