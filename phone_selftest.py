from __future__ import annotations
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import phone_bridge as bridge


class PhoneBridgeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_jget = bridge.jget
        cls.old_jpost = bridge.jpost
        cls.old_token = bridge.TOKEN
        cls.old_selftest = dict(bridge.SELFTEST)
        bridge.TOKEN = 't' * 40
        bridge.SELFTEST = {'ok': True, 'at': 1.0, 'checks': {'controller': True, 'seat_contract': True, 'screen': True, 'token': True}, 'error': ''}

        def fake_get(path, timeout=2.5):
            if path == '/seat/head':
                return {'ok': True, 'active_occupant': 'test-seat', 'input_seq': 4, 'output_seq': 3}
            if path == '/seat':
                return {'ok': True, 'active_occupant': 'test-seat', 'input_seq': 4, 'output_seq': 3,
                        'reply': {'seq': 3, 'text': 'live', 'done': False}, 'latest_input': {'seq': 4}}
            return {'ok': True}

        def fake_post(path, payload, timeout=3.0):
            return {'ok': True, 'path': path, 'payload': payload}

        bridge.jget = fake_get
        bridge.jpost = fake_post
        cls.server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        bridge.jget = cls.old_jget
        bridge.jpost = cls.old_jpost
        bridge.TOKEN = cls.old_token
        bridge.SELFTEST = cls.old_selftest

    def request(self, path, *, method='GET', value=None, auth=True):
        data = None if value is None else json.dumps(value).encode()
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + bridge.TOKEN
        req = Request(f'http://127.0.0.1:{self.port}{path}', data=data, headers=headers, method=method)
        with urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode() or '{}')

    def test_unauthorized_is_closed(self):
        with self.assertRaises(HTTPError) as cm:
            self.request('/api/health', auth=False)
        self.assertEqual(cm.exception.code, 401)

    def test_selftest_gates_session(self):
        code, value = self.request('/api/selftest')
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        code, value = self.request('/api/session', method='POST', value={})
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])

    def test_text_is_live_before_commit(self):
        code, value = self.request('/api/text-stream', method='POST', value={
            'stream_id': 's', 'revision': 1, 'text': 'typing now', 'final': False,
        })
        self.assertEqual(code, 202)
        self.assertTrue(value['live'])
        self.assertEqual(bridge.BUS['text'], 'typing now')
        self.assertTrue(bridge.BUS['text_active'])

    def test_scene_is_hot_mutable_without_restart(self):
        code, value = self.request('/api/scene', method='POST', value={
            'css': '#dock{opacity:.2}', 'layers': [{'id': 'r', 'bind': 'reply.text'}], 'features': {'screenTap': False},
        })
        self.assertEqual(code, 200)
        self.assertIn('#dock', value['scene']['css'])
        code, value = self.request('/api/scene')
        self.assertFalse(value['scene']['features']['screenTap'])

    def test_pointer_routes_as_controller_action(self):
        code, value = self.request('/api/pointer', method='POST', value={'nx': .5, 'ny': .5, 'button': 'left'})
        self.assertEqual(code, 200)
        self.assertTrue(value['result']['ok'])
        self.assertEqual(value['result']['path'], '/action')
        self.assertEqual(value['result']['payload']['action'], 'click')


if __name__ == '__main__':
    unittest.main(verbosity=2)
