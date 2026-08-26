from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import live_field
import phone_bridge as base
import phone_bridge_fast as fast
import phone_bridge_field as field


class FieldBridgeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_token = base.TOKEN
        cls.old_selftest = dict(base.SELFTEST)
        cls.old_jget = base.jget
        cls.old_jpost = base.jpost
        cls.calls = []
        base.TOKEN = 'f' * 40
        base.SELFTEST = {'ok': True, 'checks': {}, 'error': ''}
        base.jget = lambda path, timeout=2.5: {
            'ok': True,
            'active_occupant': 'test',
            'input_seq': 1,
            'output_seq': 0,
            'reply': {},
            'latest_input': {},
        }
        def fake_post(path, payload, timeout=3.0):
            cls.calls.append((path, dict(payload), timeout))
            return {'ok': True, 'path': path}
        base.jpost = fake_post
        cls.server = base.ThreadingHTTPServer(('127.0.0.1', 0), field.FieldHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        base.TOKEN = cls.old_token
        base.SELFTEST = cls.old_selftest
        base.jget = cls.old_jget
        base.jpost = cls.old_jpost

    def setUp(self):
        self.calls.clear()
        field.LIVE = live_field.LiveField(limit=128)
        fast._LAST_DEFAULT_CHAT_SEQ = -1
        with fast._ENTRY_LOCK:
            fast._ENTRY_UNTIL = 0.0
            fast._ENTRY_STREAM_ORDER.clear()
            fast._ENTRY_STREAMS.clear()
        with base.LOCK:
            base.BUS.update({
                'stream_id': '', 'revision': 0, 'text': '', 'text_active': False,
                'updated': 0.0, 'committed_revision': 0, 'audio_seq': 0, 'event': '',
            })

    def req(self, path, *, method='GET', value=None, raw=None, headers=None):
        if raw is None and value is not None:
            raw = json.dumps(value).encode()
        h = {'Authorization': 'Bearer ' + base.TOKEN}
        if value is not None:
            h['Content-Type'] = 'application/json'
        if headers:
            h.update(headers)
        return urlopen(Request(f'http://127.0.0.1:{self.port}{path}', data=raw, headers=h, method=method), timeout=2)

    def test_unknown_shape_is_still_accepted_into_field(self):
        value = {
            'channel': 'future.sensor',
            'shape': 'application/x-never-seen-before',
            'stream': 'z',
            'revision': 4,
            'payload': {'anything': [1, 2, 3]},
        }
        with self.req('/api/field', method='POST', value=value) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['ok'])
        self.assertFalse(out['adapted'])
        snap = field.LIVE.snapshot()
        self.assertEqual(snap['events'][0]['channel'], 'future.sensor')
        self.assertEqual(snap['events'][0]['shape'], 'application/x-never-seen-before')
        self.assertEqual(snap['events'][0]['payload']['anything'], [1, 2, 3])

    def test_utf8_is_only_an_adapter_not_the_field_protocol(self):
        value = {
            'channel': 'user.primary', 'shape': 'utf8', 'stream': 'turn-x',
            'revision': 1, 'final': False, 'payload': {'value': 'hi'},
        }
        with self.req('/api/field', method='POST', value=value) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['adapted'])
        self.assertEqual(base.BUS['text'], 'hi')
        self.assertTrue(base.BUS['text_active'])
        self.assertTrue(any(path == '/action' and p.get('action') == 'timebox' for path, p, _ in self.calls))

    def test_binary_shape_uses_same_endpoint(self):
        raw = b'\x00\x01\x02\x03'
        headers = {
            'Content-Type': 'application/octet-stream',
            'X-Field-Channel': 'user.primary',
            'X-Field-Shape': 'pcm_s16le',
            'X-Field-Stream': 'samples-a',
            'X-Field-Revision': '2',
            'X-Field-Rate': '16000',
        }
        with self.req('/api/field', method='POST', raw=raw, headers=headers) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['adapted'])
        self.assertTrue(any(path == '/phone/audio' for path, _, _ in self.calls))
        ingress = field.LIVE.snapshot()['events'][0]
        self.assertEqual(ingress['payload']['bytes'], len(raw))

    def test_field_stream_starts_with_channel_latest_snapshot(self):
        field.LIVE.publish('a', shape='one', payload=1)
        field.LIVE.publish('a', shape='two', payload=2)
        field.LIVE.publish('b', shape='three', payload=3)
        with self.req('/api/field.ndjson') as r:
            line = json.loads(r.readline().decode())
        self.assertEqual(line['type'], 'field')
        self.assertTrue(line['snapshot'])
        by_channel = {e['channel']: e for e in line['events']}
        self.assertEqual(by_channel['a']['payload'], 2)
        self.assertEqual(by_channel['b']['payload'], 3)

    def test_unauthorized_field_is_closed(self):
        req = Request(f'http://127.0.0.1:{self.port}/api/field')
        with self.assertRaises(HTTPError) as cm:
            urlopen(req, timeout=2)
        self.assertEqual(cm.exception.code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
