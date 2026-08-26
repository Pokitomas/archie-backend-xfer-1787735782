from __future__ import annotations

import threading
import time
import unittest

from field_transport import FieldTransport


class FieldTransportContract(unittest.TestCase):
    def test_orders_arbitrary_shapes_without_latest_authority(self):
        wire = FieldTransport(limit=64)
        a = wire.append('alpha', shape='application/x-one', payload={'x': 1}, stream='s', revision=1)
        b = wire.append('alpha', shape='application/x-two', payload={'x': 2}, stream='s', revision=2)
        c = wire.append('future.sensor', shape='weird/+shape', payload=[3, 4])
        self.assertLess(a.serial, b.serial)
        self.assertLess(b.serial, c.serial)
        self.assertFalse(hasattr(wire, 'latest'))
        self.assertFalse(hasattr(wire, 'snapshot'))
        self.assertEqual([e['shape'] for e in wire.replay()['events']], ['application/x-one', 'application/x-two', 'weird/+shape'])

    def test_binary_payload_is_only_size_and_digest(self):
        wire = FieldTransport(limit=64)
        wire.append('binary', shape='application/octet-stream', payload=b'abcdef')
        payload = wire.replay()['events'][0]['payload']
        self.assertEqual(payload['bytes'], 6)
        self.assertEqual(len(payload['sha256']), 64)
        self.assertNotIn('abcdef', str(payload))

    def test_cursor_replay_and_ring_floor(self):
        wire = FieldTransport(limit=64)
        for i in range(90):
            wire.append('x', payload=i, revision=i)
        replay = wire.replay(after=80, limit=20)
        self.assertEqual([e['revision'] for e in replay['events']], list(range(80, 90)))
        self.assertGreater(replay['floor'], 1)

    def test_wait_after_wakes_on_any_packet(self):
        wire = FieldTransport(limit=64)
        seen = []
        t = threading.Thread(target=lambda: seen.append(wire.wait_after(0, .8)))
        t.start(); time.sleep(.02)
        wire.append('anything', payload={'v': 1})
        t.join(timeout=1)
        self.assertEqual(seen, [1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
