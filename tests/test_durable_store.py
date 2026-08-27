import os
import tempfile
import unittest

from utility.durable_store import DurableStore


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'store.sqlite')
        self.store = DurableStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_count_and_oldest(self):
        self.store.append('login:user:a', 100.0)
        self.store.append('login:user:a', 150.0)
        self.store.append('login:user:b', 150.0)
        self.assertEqual(self.store.count_since('login:user:a', 90.0), 2)
        self.assertEqual(self.store.count_since('login:user:a', 120.0), 1)
        self.assertEqual(self.store.oldest_since('login:user:a', 90.0), 100.0)

    def test_survives_reopen(self):
        self.store.append('k', 10.0)
        again = DurableStore(self.path)
        self.assertEqual(again.count_since('k', 0.0), 1)

    def test_prune_and_clear(self):
        self.store.append('k', 1.0)
        self.store.append('k', 5.0)
        self.store.prune('k', 3.0)
        self.assertEqual(self.store.count_since('k', 0.0), 1)
        self.store.clear('k')
        self.assertEqual(self.store.count_since('k', 0.0), 0)

    def test_kv_roundtrip(self):
        self.assertIsNone(self.store.get('meta'))
        self.store.set('meta', 'locked')
        self.assertEqual(self.store.get('meta'), 'locked')
        self.store.set('meta', 'open')
        self.assertEqual(self.store.get('meta'), 'open')


if __name__ == '__main__':
    unittest.main()
