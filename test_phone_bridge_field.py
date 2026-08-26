from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import phone_bridge as base
import phone_bridge_field as field
from field_transport import FieldTransport


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

        def fake_jget(path, timeout=2.5):
            if path == '/seat/head':
                return {'ok': True, 'active_occupant': 'test', 'input_seq': 7, 'output_seq': 8}
            if path == '/controller':
                return cls.controller()
            return {'ok': True}

        def fake_jpost(path, payload, timeout=3.0):
            cls.calls.append((path, dict(payload), timeout))
            if path == '/phone/audio/begin':
                return {'ok': True, 'accepted': True, 'call_id': 'call-1', 'ack': {'seq': 2}}
            return {'ok': True, 'path': path, 'payload': payload}

        base.jget = fake_jget
        base.jpost = fake_jpost
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

    @staticmethod
    def controller(*, source='phone_text', activity=9, reply_text='visible delta'):
        return {
            'ok': True,
            'run': 'run-a',
            'activity': activity,
            'mutations': 2,
            'phase': 'EFFECT',
            'receipt': 'receipt-a',
            'timebox_started_ns': 111,
            'timebox_deadline_ns': 999999,
            'remaining_ms': 81234,
            'field': {
                'basis': 'basis-a', 'cursor': 4,
                'field': {'hwnd': 12, 'process': 'Code.exe', 'title': 'Code — ARCHIE'},
            },
            'attention': {'seq': 5, 'kind': 'seat_input', 'at': 22.0},
            'sensors': {
                'seat': {
                    'active_occupant': 'test', 'input_seq': 7, 'output_seq': 8,
                    'latest_input': {'seq': 7, 'source': source, 'kind': 'text', 'chars': 3},
                    'reply': {'seq': 8, 'revision': 3, 'text': reply_text, 'done': False, 'sha256': 'abc'},
                    'time': {'deadline': {'remaining_ms': 12345}, 'run_age_ms': 50},
                    'body': {'time': {'deadline_ns': 123, 'remaining_ms': 99}},
                    'voice': {'seq': 0}, 'ack_seq': 2,
                }
            },
            'events': [
                {'seq': 100, 'phase': 'DISPATCHED', 'action': 'workspace_patch', 'intent': 'field work', 'at': 10.0},
                {'seq': 101, 'phase': 'EFFECT', 'action': 'workspace_patch', 'intent': 'field work', 'at': 11.0},
            ],
        }

    def setUp(self):
        self.calls.clear()
        field.WIRE = FieldTransport(limit=128)
        field._CONTROLLER_KEY = None
        field._SCENE_KEY = None
        field._LAST_DEFAULT_CHAT_SEQ = -1
        field._PRESENCE_SHOWN = False
        with field._PRESSURE_LOCK:
            field._PRESSURE_UNTIL = 0.0
            field._PRESSURE_STREAMS.clear()
            field._PRESSURE_ORDER.clear()
        with field._CALL_LOCK:
            field._CALLS.clear()
        with base.LOCK:
            base.SCENE.clear()
            base.SCENE.update({'revision': 7, 'css': '#root{}', 'layers': [], 'nodes': [], 'features': {'fieldProtocol': True}})

    def req(self, path, *, method='GET', value=None, raw=None, headers=None, auth=True):
        if raw is None and value is not None:
            raw = json.dumps(value).encode()
        h = {}
        if auth:
            h['Authorization'] = 'Bearer ' + base.TOKEN
        if value is not None:
            h['Content-Type'] = 'application/json'
        if headers:
            h.update(headers)
        return urlopen(Request(f'http://127.0.0.1:{self.port}{path}', data=raw, headers=h, method=method), timeout=2)

    def timebox_calls(self):
        return [p for path, p, _ in self.calls if path == '/action' and p.get('action') == 'timebox']

    def test_controller_livefield_is_authority_and_pressure_clock_is_hidden(self):
        snap = field.sample_controller(force=True)
        self.assertEqual(snap['run'], 'run-a')
        wire = field.WIRE.replay()['events'][-1]
        self.assertEqual(wire['channel'], 'controller.state')
        self.assertEqual(wire['meta']['authority'], 'controller')
        public = wire['payload']
        self.assertFalse(public['urgency'])
        encoded = json.dumps(public).lower()
        self.assertNotIn('timebox', encoded)
        self.assertNotIn('remaining_ms', encoded)
        self.assertNotIn('deadline_ns', encoded)
        self.assertNotIn('"time"', json.dumps(public['sensors']['seat']).lower())
        self.assertEqual(len(self.timebox_calls()), 0)

    def test_default_chat_arms_pressure_from_canonical_seat_without_reading_text(self):
        old = base.jget
        try:
            base.jget = lambda path, timeout=2.5: self.controller(source='default-chat') if path == '/controller' else {'ok': True, 'active_occupant': 'test', 'input_seq': 7, 'output_seq': 8}
            field.sample_controller(force=True)
            self.assertEqual(len(self.timebox_calls()), 1)
            field.sample_controller(force=True)
            self.assertEqual(len(self.timebox_calls()), 1)
        finally:
            base.jget = old

    def test_aperture_seed_contains_scene_and_passive_screen_as_field_values(self):
        field.sample_controller(force=True)
        field.project_aperture(force=True)
        by_channel = {e['channel']: e for e in field.WIRE.replay()['events']}
        self.assertEqual(by_channel['machine.screen']['payload']['path'], '/api/screen.mjpg')
        self.assertTrue(by_channel['machine.screen']['payload']['passive'])
        self.assertEqual(by_channel['surface.scene']['payload']['revision'], 7)

    def test_unknown_shape_is_accepted_without_becoming_controller_semantics(self):
        value = {'channel': 'future.sensor', 'shape': 'application/x-never-seen-before', 'stream': 'z', 'revision': 4, 'payload': {'anything': [1, 2, 3]}}
        with self.req('/api/field', method='POST', value=value) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['ok'])
        self.assertFalse(out['adapted'])
        events = field.WIRE.replay()['events']
        self.assertEqual(events[0]['channel'], 'future.sensor')
        self.assertEqual(events[0]['payload']['anything'], [1, 2, 3])
        self.assertEqual(events[1]['channel'], 'future.sensor.receipt')

    def test_text_edits_cross_field_immediately_but_only_final_commits_controller(self):
        partial = {'channel': 'user.primary', 'shape': 'utf8', 'stream': 'turn-x', 'revision': 1, 'final': False, 'payload': {'value': 'hi'}}
        with self.req('/api/field', method='POST', value=partial) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['adapted'])
        self.assertFalse(out['result']['committed'])
        self.assertFalse(any(path == '/phone/text' for path, _, _ in self.calls))
        self.assertEqual(len(self.timebox_calls()), 1)

        final = {**partial, 'revision': 2, 'final': True, 'payload': {'value': 'hi there'}}
        with self.req('/api/field', method='POST', value=final) as r:
            out = json.loads(r.read().decode())
        self.assertTrue(out['result']['committed'])
        phone = [p for path, p, _ in self.calls if path == '/phone/text']
        self.assertEqual(phone[-1]['text'], 'hi there')
        self.assertEqual(len(self.timebox_calls()), 1)

    def test_hold_uses_controller_begin_preview_final_pipeline(self):
        contact = {'channel': 'user.contact', 'shape': 'application/vnd.archie.contact+json', 'stream': 'hold-a', 'revision': 1, 'payload': {'active': True}}
        with self.req('/api/field', method='POST', value=contact) as r:
            self.assertEqual(r.status, 202)
        self.assertTrue(any(path == '/phone/audio/begin' for path, _, _ in self.calls))

        raw = b'\x00\x01' * 3200
        common = {'Content-Type': 'application/octet-stream', 'X-Field-Channel': 'user.primary', 'X-Field-Shape': 'pcm_s16le', 'X-Field-Stream': 'hold-a', 'X-Field-Rate': '16000'}
        with self.req('/api/field', method='POST', raw=raw, headers={**common, 'X-Field-Preview': '1', 'X-Field-Revision': '2'}) as r:
            self.assertEqual(r.status, 202)
        preview = [p for path, p, _ in self.calls if path == '/phone/audio/preview']
        self.assertEqual(preview[-1]['call_id'], 'call-1')

        with self.req('/api/field', method='POST', raw=raw, headers={**common, 'X-Field-Final': '1', 'X-Field-Revision': '3'}) as r:
            self.assertEqual(r.status, 202)
        final = [p for path, p, _ in self.calls if path == '/phone/audio']
        self.assertEqual(final[-1]['call_id'], 'call-1')

    def test_pcm_below_controller_window_is_refused_before_controller(self):
        before = len(self.calls)
        headers = {'Content-Type': 'application/octet-stream', 'X-Field-Channel': 'user.primary', 'X-Field-Shape': 'pcm_s16le', 'X-Field-Stream': 'tiny', 'X-Field-Rate': '16000'}
        with self.assertRaises(HTTPError) as cm:
            self.req('/api/field', method='POST', raw=b'\x00\x00' * 100, headers=headers)
        self.assertEqual(cm.exception.code, 503)
        self.assertFalse(any(path in {'/phone/audio', '/phone/audio/preview'} for path, _, _ in self.calls[before:]))

    def test_field_stream_is_reseeded_from_controller_not_transport_latest(self):
        with self.req('/api/field.ndjson') as r:
            line = json.loads(r.readline().decode())
        self.assertEqual(line['type'], 'field')
        self.assertEqual(line['seeded_from'], 'controller')
        channels = {e['channel'] for e in line['events']}
        self.assertIn('controller.state', channels)
        self.assertIn('surface.scene', channels)
        self.assertIn('machine.screen', channels)

    def test_legacy_phone_control_endpoints_are_not_part_of_field_aperture(self):
        for path in ('/api/action', '/api/pointer', '/api/text-stream', '/api/audio', '/api/scene'):
            with self.assertRaises(HTTPError) as cm:
                self.req(path, method='POST', value={'action': 'click'})
            self.assertEqual(cm.exception.code, 404)

    def test_preflight_allows_generic_binary_field_headers(self):
        req = Request(f'http://127.0.0.1:{self.port}/api/field', method='OPTIONS', headers={
            'Origin': 'https://example.test',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type,x-field-shape,x-field-preview',
        })
        with urlopen(req, timeout=2) as r:
            allowed = r.headers.get('Access-Control-Allow-Headers', '')
        self.assertIn('X-Field-Shape', allowed)
        self.assertIn('X-Field-Preview', allowed)

    def test_unauthorized_field_is_closed(self):
        with self.assertRaises(HTTPError) as cm:
            self.req('/api/field', auth=False)
        self.assertEqual(cm.exception.code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
