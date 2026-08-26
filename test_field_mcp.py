from __future__ import annotations

import json
import unittest

import field_mcp


class FieldMcpContract(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.controller = {
            'ok': True,
            'run': 'r',
            'activity': 9,
            'sensors': {'seat': {'active_occupant': 'provider', 'input_seq': 7, 'output_seq': 8}},
        }

    def get(self, path, timeout=1.0):
        if path == '/controller/entry':
            return {'ok': False, 'status': 404}
        if path == '/controller':
            return dict(self.controller)
        return {'ok': False}

    def act(self, payload):
        self.actions.append(dict(payload))
        return {'ok': True, 'receipt': 'abc'}

    def field(self, after=0):
        return {'serial': 3, 'events': [{'serial': 3, 'channel': 'controller.state'}] if after < 3 else []}

    def call(self, message):
        return field_mcp.handle(
            message,
            controller_get=self.get,
            controller_action=self.act,
            field_snapshot=self.field,
            urgency=lambda: True,
        )

    def test_initialize_tells_client_entry_requires_no_history(self):
        r = self.call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(r['result']['protocolVersion'], '2025-06-18')
        self.assertIn('enter_controller', r['result']['instructions'])
        self.assertIn('tools', r['result']['capabilities'])

    def test_tool_description_is_zero_history_trigger(self):
        r = self.call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        tools = {t['name']: t for t in r['result']['tools']}
        enter = tools['enter_controller']
        self.assertEqual(enter['inputSchema']['required'] if 'required' in enter['inputSchema'] else [], [])
        d = enter['description'].lower()
        self.assertIn('immediately', d)
        self.assertIn('no prior chat context', d)
        self.assertIn('before asking', d)

    def test_entry_capsule_is_structured_backend_state_not_chat_memory(self):
        r = self.call({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'enter_controller', 'arguments': {}}})
        result = r['result']
        self.assertFalse(result['isError'])
        cap = result['structuredContent']
        self.assertEqual(cap['schema'], 'archie-entry-capsule/v1')
        self.assertEqual(cap['contract']['authority'], 'controller')
        self.assertEqual(cap['contract']['surface'], 'live-field')
        self.assertTrue(cap['contract']['urgency'])
        self.assertFalse(cap['contract']['temporal_detail_exposed'])
        self.assertEqual(cap['controller']['run'], 'r')
        self.assertEqual(cap['field']['serial'], 3)
        self.assertNotIn('900', json.dumps(cap['contract']))

    def test_controller_can_replace_fallback_contract(self):
        def native(path, timeout=1.0):
            if path == '/controller/entry':
                return {'ok': True, 'contract': {'schema': 'native', 'authority': 'controller', 'remaining_ms': 9, 'timebox': 900}}
            return dict(self.controller)
        cap = field_mcp.entry_capsule(controller_get=native, field_snapshot=self.field, urgency=lambda: True)
        self.assertEqual(cap['contract']['schema'], 'native')
        self.assertTrue(cap['contract']['urgency'])
        self.assertNotIn('remaining_ms', cap['contract'])
        self.assertNotIn('timebox', cap['contract'])

    def test_action_stays_inside_controller_membrane(self):
        payload = {'action': 'key', 'key': 'escape'}
        r = self.call({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'controller_action', 'arguments': {'action': payload}}})
        self.assertEqual(self.actions, [payload])
        self.assertEqual(r['result']['structuredContent']['result']['receipt'], 'abc')

    def test_snapshot_does_not_need_reentry(self):
        r = self.call({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'field_snapshot', 'arguments': {'after': 2}}})
        self.assertEqual(r['result']['structuredContent']['serial'], 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
