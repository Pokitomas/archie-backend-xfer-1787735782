from __future__ import annotations

import unittest

from activity_field import ActivityField


class ActivityFieldContract(unittest.TestCase):
    def test_observe_mirrors_pc_and_seat_without_becoming_a_control_surface(self):
        field = ActivityField(limit=32)
        seat = {
            'active_occupant': 'gpt56sol-interface-live',
            'input_seq': 8,
            'output_seq': 9,
            'latest_input': {'seq': 8, 'source': 'phone-text', 'kind': 'text', 'chars': 11},
            'reply': {'seq': 9, 'revision': 2, 'text': 'hello', 'chars': 5, 'done': False},
        }
        emitted = field.observe(seat, focus='Code', bus_event='scene:hot-reload')
        kinds = {x.kind for x in emitted}
        self.assertTrue({'occupant', 'input', 'output', 'focus', 'bridge'} <= kinds)
        snap = field.snapshot()
        self.assertEqual(snap['serial'], len(snap['items']))
        self.assertFalse(any('deadline' in str(x).lower() for x in snap['items']))
        self.assertEqual(snap['items'][0]['kind'], 'bridge')

    def test_workspace_patch_is_rendered_as_paths_not_patch_body(self):
        field = ActivityField()
        patch = '''*** Begin Patch
*** Update File: ARCHIE_CONTROLLER.py
@@
-old
+new
*** Add File: tests/test_controller.py
+assert True
*** End Patch'''
        item = field.record_action(
            {'action': 'workspace_patch', 'patch': patch, 'intent': 'controller parity'},
            {'ok': True, 'receipt': 'abc'},
        )
        self.assertEqual(item.kind, 'code')
        self.assertIn('ARCHIE_CONTROLLER.py', item.detail)
        self.assertIn('tests/test_controller.py', item.detail)
        self.assertNotIn('+new', item.detail)
        self.assertEqual(item.status, 'ok')

    def test_identical_activity_is_coalesced(self):
        field = ActivityField()
        a = field.push('action', 'click', 'same')
        b = field.push('action', 'click', 'same')
        self.assertEqual(a.seq, b.seq)
        self.assertEqual(field.snapshot()['serial'], 1)

    def test_snapshot_is_newest_first_for_constant_time_surface_binding(self):
        field = ActivityField()
        field.push('action', 'first')
        field.push('action', 'second')
        field.push('action', 'third')
        self.assertEqual([x['label'] for x in field.snapshot()['items'][:3]], ['third', 'second', 'first'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
