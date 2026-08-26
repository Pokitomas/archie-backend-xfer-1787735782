from __future__ import annotations

import json
import threading
import unittest
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
        base.TOKEN = 'z' * 40
        base.SELFTEST = {'ok': True, 'at': 1.0, 'checks': {}, 'error': ''}
        base.jpost = lambda path, payload, timeout=3.0: {'ok': True, 'path': path, 'payload': payload}
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
        fast.FIELD = acoustic_field.AcousticField(min_chars=3, max_phrase_chars=40, resume_guard_ms=0)
        fast.GESTURES.clear()
        fast.VOICE_ACTIVE = False
        fast._LAST_RESPONSE_ID = None
        with fast.FAST_LOCK:
            fast.LATEST.update({'serial': 0, 'reply': {}, 'seat': {}, 'acoustic': fast.FIELD.snapshot(), 'at': 0.0})
        base.BUS['text_active'] = False

    def req(self, path, *, method='GET', value=None):
        raw = None if value is None else json.dumps(value).encode()
        headers = {'Authorization': 'Bearer ' + base.TOKEN}
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        return urlopen(Request(f'http://127.0.0.1:{self.port}{path}', data=raw, headers=headers, method=method), timeout=2)

    @staticmethod
    def seat(text, *, seq=3, revision=1, done=False, input_id='i'):
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
            'time': {},
        }

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
