from __future__ import annotations

import unittest

import field_protocol as p
from field_transport import FieldTransport
from live_field import LiveField


class FieldProtocolContract(unittest.TestCase):
    def test_canonical_and_transport_normalize_identically(self):
        canonical = LiveField(limit=64).publish(
            ' weird channel!? ', shape='application/x future+binary', payload=b'abc',
            stream=' s!? ', revision='7', final=True, meta={'x': b'12'},
        )
        transport = FieldTransport(limit=64).append(
            ' weird channel!? ', shape='application/x future+binary', payload=b'abc',
            stream=' s!? ', revision='7', final=True, meta={'x': b'12'},
        )
        for key in ('channel', 'shape', 'stream', 'revision', 'final', 'payload', 'meta'):
            self.assertEqual(getattr(canonical, key), getattr(transport, key), key)

    def test_protocol_is_shape_agnostic(self):
        value = p.record(1, 'future.sensor', shape_value='x/custom+thing', payload={'whatever': [1, 2]})
        self.assertEqual(value.channel, 'future.sensor')
        self.assertEqual(value.shape, 'x/custom+thing')
        self.assertEqual(value.payload, {'whatever': [1, 2]})

    def test_binary_wire_identity_does_not_assume_codec(self):
        value = p.safe(b'\x00\x01\x02')
        self.assertTrue(value['binary'])
        self.assertEqual(value['bytes'], 3)
        self.assertEqual(len(value['sha256']), 64)


if __name__ == '__main__':
    unittest.main(verbosity=2)
