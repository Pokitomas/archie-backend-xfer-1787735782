from __future__ import annotations

import inspect
import unittest
from pathlib import Path

import native_aperture
import native_resident as r


class NativeResidentContract(unittest.TestCase):
    def test_public_ip_filter_rejects_local_and_cgnat(self):
        for value in ('127.0.0.1', '192.168.1.2', '10.0.0.2', '100.64.1.1', '::1', 'fd00::1'):
            self.assertFalse(r.is_public_ip(value), value)
        self.assertTrue(r.is_public_ip('8.8.8.8'))
        self.assertTrue(r.is_public_ip('2606:4700:4700::1111'))

    def test_ipv6_route_is_direct_and_standard_https(self):
        old_candidates = native_aperture.global_ipv6_candidates
        old_bind = r.can_bind
        try:
            native_aperture.global_ipv6_candidates = lambda: ['2606:4700:4700::1111']
            r.can_bind = lambda host, port: True
            route, errors = r.choose_ipv6()
        finally:
            native_aperture.global_ipv6_candidates = old_candidates
            r.can_bind = old_bind
        self.assertEqual(errors, [])
        self.assertEqual(route.kind, 'direct-ipv6')
        self.assertEqual(route.internal_https_port, 443)
        self.assertEqual(route.challenge_port, 80)
        self.assertEqual(route.public_url, 'https://[2606:4700:4700::1111]')

    def test_ipv4_route_requires_exact_public_80_and_443(self):
        old = native_aperture.map_port
        calls = []
        def mapped(**kw):
            calls.append(dict(kw))
            external = kw['external_port']
            return native_aperture.Mapping('pcp', '8.8.4.4', external, kw['internal_port'], kw['lifetime']), []
        try:
            native_aperture.map_port = mapped
            route, errors = r.choose_ipv4()
        finally:
            native_aperture.map_port = old
        self.assertEqual(errors, [])
        self.assertEqual(route.kind, 'mapped-ipv4')
        self.assertEqual(route.public_url, 'https://8.8.4.4')
        self.assertEqual([x['external_port'] for x in calls], [80, 443])

    def test_runtime_sources_do_not_reintroduce_hosted_relays(self):
        sources = '\n'.join([
            inspect.getsource(r),
            inspect.getsource(native_aperture),
            Path('native_field_server.py').read_text(encoding='utf-8'),
            Path('native_field_client.js').read_text(encoding='utf-8'),
            Path('native_index.html').read_text(encoding='utf-8'),
        ]).lower()
        for forbidden in ('vercel.app', 'trycloudflare.com', 'cloudflared', 'ntfy.sh', 'tail1bf489', 'raw.githubusercontent.com', 'raw.githack'):
            self.assertNotIn(forbidden, sources)

    def test_native_client_has_one_interaction_control_and_generic_field_api(self):
        html = Path('native_index.html').read_text(encoding='utf-8')
        js = Path('native_field_client.js').read_text(encoding='utf-8')
        self.assertEqual(html.lower().count('<button'), 1)
        self.assertIn('window.ARCHIE_FIELD', js)
        self.assertIn('/api/field', js)
        self.assertIn('/api/field.ndjson', js)
        self.assertNotIn('/api/pointer', js)
        self.assertNotIn('speechSynthesis', js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
