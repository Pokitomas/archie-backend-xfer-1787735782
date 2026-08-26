from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import acoustic_field
import phone_bridge as base
import phone_bridge_fast as fast


class FastBridgeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_token = base.TOKEN
        cls.old_selftest = dict(base.SELFTEST)
        cls.old_jget = base.jget
        cls.old_jpost = base.jpost
        cls.calls = []
        base.TOKEN = 'z' * 40
        base.SELFTEST = {'ok': True, 'at': 1.0, 'checks': {}, 'error': ''}
        base.jget = lambda path, timeout=2.5: {
            'ok': True,
            'active_occupant': 'test',
            'input_seq': 0,
            'output_seq': 0,
            'reply': {},
            'latest_input': {},
            'time': {'deadline': {'remaining_ms': 123456}},
        }

        def fake_jpost(path, payload, timeout=3.0):
            cls.calls.append((path, dict(payload), timeout))
            return {'ok': True, 'path': path, 'payload': payload}

        base.jpost = fake_jpost
        cls.server = base.ThreadingHTTPServer(('127.0.0.1', 0), fast.FastHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        base.TOKEN = cls.old_token
        base.SELFTEST = cls.old_selftest
        base.jget = cls.old_jget
        base.jpost = cls.old_jpost

    def setUp(self):
        self.calls.clear()
        fast.FIELD = acoustic_field.AcousticField(min_chars=3, max_phrase_chars=40, resume_guard_ms=0)
        fast.GESTURES.clear()
        fast.VOICE_ACTIVE = False
        fast._LAST_RESPONSE_ID = None
        fast._LAST_DEFAULT_CHAT_SEQ = -1
        with fast._ENTRY_LOCK:
            fast._ENTRY_UNTIL = 0.0
            fast._ENTRY_STREAM_ORDER.clear()
            fast._ENTRY_STREAMS.clear()
        with fast.FAST_LOCK:
            fast.LATEST.update({'serial': 0, 'reply': {}, 'seat': {}, 'acoustic': fast.FIELD.snapshot(), 'at': 0.0})
        base.BUS['text_active'] = False
        base.BUS['stream_id'] = ''
        base.BUS['revision'] = 0
        base.BUS['committed_revision'] = 0

    def req(self, path, *, method='GET', value=None, raw=None, content_type='application/json'):
        if raw is None and value is not None:
            raw = json.dumps(value).encode()
        headers = {'Authorization': 'Bearer ' + base.TOKEN}
        if raw is not None:
            headers['Content-Type'] = content_type
        return urlopen(Request(f'http://127.0.0.1:{self.port}{path}', data=raw, headers=headers, method=method), timeout=2)

    @staticmethod
    def seat(text, *, seq=3, revision=1, done=False, input_id='i', latest_input=None):
        return {
            'ok': True,
            'active_occupant': 'test',
            'input_seq': 4,
            'output_seq': seq,
            'reply': {
                'seq': seq,
                'revision': revision,
                'stream_id': 's',
                'input_id': input_id,
                'text': text,
                'done': done,
            },
            'latest_input': latest_input or {},
            'time': {'deadline': {'remaining_ms': 123456}, 'run_age_ms': 99},
        }

    def timebox_calls(self):
        return [payload for path, payload, _ in self.calls if path == '/action' and payload.get('action') == 'timebox']

    def test_observer_exposes_text_and_acoustic_prime(self):
        snap = fast.observe_once(self.seat('partial answer, more'))
        self.assertEqual(snap['reply']['text'], 'partial answer, more')
        self.assertTrue(any(g['kind'] == 'prime' for g in snap['gestures']))
        self.assertGreater(snap['serial'], 0)

    def test_reply_ndjson_pushes_current_snapshot_immediately(self):
        fast.observe_once(self.seat('first visible delta'))
        with self.req('/api/reply.ndjson') as r:
            line = r.readline()
        value = json.loads(line.decode())
        self.assertEqual(value['type'], 'reply')
        self.assertEqual(value['reply']['text'], 'first visible delta')
        self.assertIn('acoustic', value)

    def test_voice_state_cuts_optional_acoustics_without_touching_text(self):
        fast.observe_once(self.seat('one phrase. unfinished tail'))
        generation = fast.FIELD.generation
        with self.req('/api/voice-state', method='POST', value={'active': True}) as r:
            value = json.loads(r.read().decode())
        self.assertTrue(value['voice_active'])
        self.assertGreater(value['acoustic']['generation'], generation)
        self.assertTrue(any(g['kind'] == 'cut' and g['reason'] == 'user-onset' for g in value['gestures']))
        self.assertEqual(value['reply']['text'], 'one phrase. unfinished tail')
        self.assertEqual(len(self.timebox_calls()), 1)

    def test_typing_suppresses_optional_phrase(self):
        base.BUS['text_active'] = True
        snap = fast.observe_once(self.seat('do not speak over typing.'))
        self.assertTrue(snap['text_active'])
        self.assertFalse(any(g['kind'] == 'phrase' for g in snap['gestures']))

    def test_new_response_identity_supersedes_old_generation(self):
        first = fast.observe_once(self.seat('old phrase. tail', seq=2, input_id='old'))
        old_generation = first['acoustic']['generation']
        second = fast.observe_once(self.seat('new phrase.', seq=3, revision=0, input_id='new'))
        self.assertGreater(second['acoustic']['generation'], old_generation)
        self.assertTrue(any(g['reason'] == 'response-superseded' for g in second['gestures']))
        self.assertEqual(second['reply']['text'], 'new phrase.')

    def test_first_text_delta_arms_before_base_bridge_and_only_once_per_stream(self):
        value = {'stream_id': 'turn-a', 'revision': 1, 'text': 'h', 'final': False}
        with self.req('/api/text-stream', method='POST', value=value) as r:
            self.assertEqual(r.status, 202)
        calls = self.timebox_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['seconds'], 900.0)

        value.update({'revision': 2, 'text': 'hello'})
        with self.req('/api/text-stream', method='POST', value=value) as r:
            self.assertEqual(r.status, 202)
        self.assertEqual(len(self.timebox_calls()), 1)

        value.update({'stream_id': 'turn-b', 'revision': 1, 'text': 'new'})
        with self.req('/api/text-stream', method='POST', value=value) as r:
            self.assertEqual(r.status, 202)
        self.assertEqual(len(self.timebox_calls()), 2)

    def test_default_chat_source_arms_without_examining_message_text(self):
        latest = {'seq': 77, 'source': 'default-chat', 'chars': 999, 'sha256': 'x'}
        fast.observe_once(self.seat('reply', latest_input=latest))
        self.assertEqual(len(self.timebox_calls()), 1)
        fast.observe_once(self.seat('reply', latest_input=latest))
        self.assertEqual(len(self.timebox_calls()), 1)

    def test_model_facing_state_has_urgency_but_no_deadline_or_timebox_fields(self):
        self.assertTrue(fast._arm_entry_pressure())
        snap = fast.observe_once(self.seat('answer'))
        self.assertIs(snap['seat']['urgency'], True)
        self.assertNotIn('time', snap['seat'])
        wire = json.dumps(snap['seat']).lower()
        self.assertNotIn('deadline', wire)
        self.assertNotIn('remaining', wire)
        self.assertNotIn('timebox', wire)
        self.assertNotIn('900', wire)

        with self.req('/api/state') as r:
            state = json.loads(r.read().decode())
        wire = json.dumps(state['seat']).lower()
        self.assertTrue(state['seat']['urgency'])
        self.assertNotIn('time', state['seat'])
        self.assertNotIn('deadline', wire)
        self.assertNotIn('remaining', wire)
        self.assertNotIn('timebox', wire)
        self.assertNotIn('900', wire)

    def test_audio_never_crosses_without_pressure(self):
        original = fast._arm_entry_pressure
        try:
            fast._arm_entry_pressure = lambda: False
            with self.assertRaises(HTTPError) as cm:
                self.req('/api/audio?rate=16000', method='POST', raw=b'\x00\x00', content_type='application/octet-stream')
            self.assertEqual(cm.exception.code, 503)
            self.assertFalse(any(path == '/phone/audio' for path, _, _ in self.calls))
        finally:
            fast._arm_entry_pressure = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
