from __future__ import annotations

import threading
import unittest

import field_controller_adapters as adapters
import field_mcp
import phone_bridge as base
import phone_bridge_field as field
from field_transport import FieldTransport


class _FakeControllerBase:
    LOCK = threading.RLock()
    SCENE = {}
    BUS = {}
    calls = []
    result = {'ok': True, 'receipt': 'r'}

    @classmethod
    def controller_action(cls, payload):
        cls.calls.append(payload)
        return dict(cls.result)

    @classmethod
    def jpost(cls, path, payload, timeout=3.0):
        raise AssertionError(f'unexpected legacy controller adapter path: {path}')


class FullPcParityContract(unittest.TestCase):
    def setUp(self):
        _FakeControllerBase.calls = []
        _FakeControllerBase.result = {'ok': True, 'receipt': 'r'}

    @staticmethod
    def registry():
        table = {}

        def register(shape):
            def deco(fn):
                table[shape] = fn
                return fn
            return deco

        adapters.install(
            register,
            base=_FakeControllerBase,
            arm_stream=lambda stream: True,
            project_aperture=lambda **kwargs: None,
        )
        return table

    def test_field_action_adapter_has_no_remote_action_whitelist(self):
        table = self.registry()
        action = table['application/vnd.archie.action+json']
        payload = {
            'action': 'future_controller_capability',
            'intent': 'prove opaque parity',
            'basis': {'revision': 77, 'digest': 'abc'},
            'coordinates': {'x': 11, 'y': 29},
            'future': {
                'nested': [1, {'anything': True}],
                'bytes_hint': 1234,
                'policy_owned_by_controller': True,
            },
        }
        result = action({'stream': 'parity-a', 'payload': payload}, b'')
        self.assertTrue(result['ok'])
        self.assertEqual(_FakeControllerBase.calls, [payload])
        self.assertIs(_FakeControllerBase.calls[0], payload)
        self.assertEqual(result['result']['receipt'], 'r')

    def test_controller_refusal_propagates_without_field_retry_or_rewrite(self):
        table = self.registry()
        action = table['application/vnd.archie.action+json']
        _FakeControllerBase.result = {'ok': False, 'error': 'controller-refused', 'receipt': 'no'}
        payload = {'action': 'future_controller_capability', 'opaque': {'a': 1}}
        result = action({'stream': 'parity-b', 'payload': payload}, b'')
        self.assertFalse(result['ok'])
        self.assertEqual(result['result']['error'], 'controller-refused')
        self.assertEqual(_FakeControllerBase.calls, [payload])

    def test_mcp_controller_action_preserves_entire_action_object(self):
        seen = []
        payload = {
            'action': 'workspace_patch',
            'patch': '*** Begin Patch\n*** Add File: x\n+y\n*** End Patch',
            'manifest': {'x': {'before_sha256': None}},
            'intent': 'opaque full parity',
            'future_extension': {'mode': 'unseen', 'args': [1, 2, 3]},
        }

        response = field_mcp.handle(
            {
                'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                'params': {'name': 'controller_action', 'arguments': {'action': payload}},
            },
            controller_get=lambda path, timeout=1.0: {'ok': True},
            controller_action=lambda value: seen.append(value) or {'ok': True, 'receipt': 'mcp-r'},
            field_snapshot=lambda after=0: {'serial': 0, 'events': []},
            urgency=lambda: True,
        )
        self.assertEqual(seen, [payload])
        self.assertIs(seen[0], payload)
        structured = response['result']['structuredContent']
        self.assertEqual(structured['schema'], 'archie-action-result/v1')
        self.assertEqual(structured['result']['receipt'], 'mcp-r')

    def test_listening_presence_is_a_controller_visual_and_field_event(self):
        old_action = base.controller_action
        old_wire = field.WIRE
        old_shown = field._PRESENCE_SHOWN
        calls = []
        try:
            base.controller_action = lambda payload: calls.append(dict(payload)) or {'ok': True, 'receipt': 'presence-r'}
            field.WIRE = FieldTransport(limit=32)
            field._PRESENCE_SHOWN = False
            field._show_presence_once()
            field._show_presence_once()

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['action'], 'undertow')
            self.assertEqual(calls[0]['text'], '∴')
            self.assertGreater(calls[0]['ttl_ms'], 0)
            events = field.WIRE.replay()['events']
            presence = [e for e in events if e['channel'] == 'surface.presence']
            self.assertEqual(len(presence), 1)
            self.assertEqual(presence[0]['payload']['state'], 'listening')
            self.assertEqual(presence[0]['meta']['authority'], 'controller')
        finally:
            base.controller_action = old_action
            field.WIRE = old_wire
            field._PRESENCE_SHOWN = old_shown


if __name__ == '__main__':
    unittest.main(verbosity=2)
