from __future__ import annotations

import threading
import time
import unittest

from live_field import LiveField


class LiveFieldContract(unittest.TestCase):
    def test_accepts_arbitrary_shapes_without_semantic_knowledge(self):
        f = LiveField(limit=64)
        a = f.publish('user.primary', shape='utf8', payload={'value': 'x'}, stream='s', revision=1)
        b = f.publish('sensor.future-thing', shape='application/x-unknown+binary', payload=b'abc', stream='z', revision=9)
        self.assertGreater(b.serial, a.serial)
        snap = f.snapshot()
        self.assertEqual([e['channel'] for e in snap['events']], ['user.primary', 'sensor.future-thing'])
        self.assertEqual(snap['events'][1]['payload']['bytes'], 3)
        self.assertEqual(len(snap['events'][1]['payload']['sha256']), 64)

    def test_latest_is_channel_state_not_modality_state(self):
        f = LiveField(limit=64)
        f.publish('a', shape='one', payload=1, revision=1)
        f.publish('b', shape='two', payload=2, revision=1)
        f.publish('a', shape='three', payload=3, revision=2, final=True)
        snap = f.snapshot(latest=True)
        by_channel = {e['channel']: e for e in snap['events']}
        self.assertEqual(by_channel['a']['shape'], 'three')
        self.assertEqual(by_channel['a']['payload'], 3)
        self.assertTrue(by_channel['a']['final'])
        self.assertEqual(by_channel['b']['payload'], 2)

    def test_wait_after_wakes_on_any_new_event(self):
        f = LiveField(limit=64)
        seen = []
        def waiter():
            seen.append(f.wait_after(0, timeout=1.0))
        t = threading.Thread(target=waiter)
        t.start(); time.sleep(.02)
        f.publish('whatever', shape='opaque', payload={'x': 1})
        t.join(timeout=1)
        self.assertEqual(seen, [1])

    def test_ring_floor_and_after_cursor(self):
        f = LiveField(limit=64)
        for i in range(90):
            f.publish('x', payload=i, revision=i)
        snap = f.snapshot(after=80, limit=20)
        self.assertEqual([e['revision'] for e in snap['events']], list(range(80, 90)))
        self.assertGreater(snap['floor'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
