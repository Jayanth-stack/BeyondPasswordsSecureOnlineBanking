import os
import tempfile
import unittest

from utility.rate_limit import RateLimitExceeded, RateLimiter


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.limiter = RateLimiter.from_path(
            os.path.join(self.tmp.name, 'rl.sqlite'),
            clock=self.clock,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_until_limit_then_blocks(self):
        for _ in range(3):
            result = self.limiter.consume('user:a', limit=3, window_seconds=60)
            self.assertTrue(result.allowed)
        with self.assertRaises(RateLimitExceeded) as ctx:
            self.limiter.consume('user:a', limit=3, window_seconds=60)
        self.assertGreaterEqual(ctx.exception.retry_after, 1)

    def test_window_expiry_unblocks(self):
        for _ in range(2):
            self.limiter.consume('user:a', limit=2, window_seconds=30)
        self.clock.advance(31)
        result = self.limiter.consume('user:a', limit=2, window_seconds=30)
        self.assertTrue(result.allowed)

    def test_keys_are_independent(self):
        self.limiter.consume('ip:1', limit=1, window_seconds=60)
        result = self.limiter.consume('ip:2', limit=1, window_seconds=60)
        self.assertTrue(result.allowed)

    def test_over_limit_peek_does_not_consume(self):
        self.limiter.consume('u', limit=1, window_seconds=60)
        self.assertTrue(self.limiter.over_limit('u', limit=1, window_seconds=60))
        self.assertEqual(self.limiter.remaining('u', limit=5, window_seconds=60), 4)

    def test_reset_clears_bucket(self):
        self.limiter.consume('u', limit=1, window_seconds=60)
        self.limiter.reset('u')
        self.assertFalse(self.limiter.over_limit('u', limit=1, window_seconds=60))
        self.limiter.consume('u', limit=1, window_seconds=60)

    def test_restart_reuses_same_file(self):
        self.limiter.consume('u', limit=2, window_seconds=60)
        revived = RateLimiter.from_path(
            self.limiter.store.path,
            clock=self.clock,
        )
        revived.consume('u', limit=2, window_seconds=60)
        with self.assertRaises(RateLimitExceeded):
            revived.consume('u', limit=2, window_seconds=60)


if __name__ == '__main__':
    unittest.main()
