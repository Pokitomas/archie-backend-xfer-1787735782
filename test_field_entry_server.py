from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import field_transport
import phone_bridge as base
import phone_bridge_field as field
import field_entry_server as entry


class EntryServerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_token = base.TOKEN
        cls.old_selftest = dict(base.SELFTEST)
        cls.old_jget = base.jget
        cls.old_jpost = base.jpost
        cls.calls = []
        base.TOKEN = 'm' * 40
        base.SELFTEST = {'ok': True, 'checks': {}, 'error': ''}

        def get(path, timeout=2.0):
            if path == '/seat/head':
                return {'ok': True, 'active_occupant': 'seat', 'input_seq': 2, 'output_seq': 3}
            if path == '/controller/entry':
                return {'ok': False, 'status': 404}
            if path == '/controller':
                return {
                    'ok': True, 'run': 'run-x', 'activity': 7, 'mutations': 1,
                    'timebox_deadline_ns': 9, 'remaining_ms': 8,
                    'field': {'basis': 'b', 'cursor': 1, 'field': {'title': 'Edge'}},
                    'attention': {'seq': 1},
                    'sensors': {'seat': {'active_occupant': 'seat', 'input_seq': 2, 'output_seq': 3, 'latest_input': {}, 'reply': {}}},
                    'events': [],
                }
            return {'ok': True}

        def post(path, payload, timeout=3.0):
            cls.calls.append((path, dict(payload), timeout))
            return {'ok': True, 'receipt': 'r'}

        base.jget = get
        base.jpost = post
        cls.server = base.ThreadingHTTPServer(('127.0.0.1', 0), entry.EntryFieldHandler)
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
        field.WIRE = field_transport.FieldTransport(limit=128)
        field._CONTROLLER_KEY = None
        field._SCENE_KEY = None
        with field._PRESSURE_LOCK:
            field._PRESSURE_UNTIL = 0.0
            field._PRESSURE_STREAMS.clear(); field._PRESSURE_ORDER.clear()

    def req(self, message, *, auth=True):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Mcp-Method': str(message.get('method') or '')}
        if auth:
            headers['Authorization'] = 'Bearer ' + base.TOKEN
        data = json.dumps(message).encode()
        return urlopen(Request(f'http://127.0.0.1:{self.port}/mcp', data=data, headers=headers, method='POST'), timeout=2)

    def test_initialize_and_tools_list(self):
        with self.req({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}) as r:
            out = json.loads(r.read())
        self.assertEqual(out['result']['serverInfo']['name'], 'archie-live-field')
        with self.req({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}) as r:
            out = json.loads(r.read())
        names = {x['name'] for x in out['result']['tools']}
        self.assertEqual(names, {'enter_controller', 'controller_action', 'field_snapshot'})

    def test_entry_arms_below_model_and_returns_redacted_controller(self):
        with self.req({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'enter_controller', 'arguments': {}}}) as r:
            out = json.loads(r.read())
        cap = out['result']['structuredContent']
        self.assertTrue(cap['contract']['urgency'])
        self.assertEqual(cap['controller']['run'], 'run-x')
        wire = json.dumps(cap['controller']).lower()
        self.assertNotIn('timebox', wire)
        self.assertNotIn('remaining_ms', wire)
        timebox = [p for path, p, _ in self.calls if path == '/action' and p.get('action') == 'timebox']
        self.assertEqual(len(timebox), 1)
        self.assertEqual(timebox[0]['seconds'], 900.0)

    def test_action_is_only_proxied_to_controller(self):
        action = {'action': 'key', 'key': 'escape'}
        with self.req({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'controller_action', 'arguments': {'action': action}}}) as r:
            out = json.loads(r.read())
        self.assertFalse(out['result']['isError'])
        self.assertTrue(any(path == '/action' and p.get('action') == 'key' for path, p, _ in self.calls))

    def test_auth_is_required(self):
        with self.assertRaises(HTTPError) as cm:
            self.req({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'}, auth=False)
        self.assertEqual(cm.exception.code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
