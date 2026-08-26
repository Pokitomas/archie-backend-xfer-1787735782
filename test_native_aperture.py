from __future__ import annotations

import ipaddress
import struct
import unittest

import native_aperture as n


class NativeApertureContract(unittest.TestCase):
    def test_natpmp_external_parse(self):
        packet = struct.pack('!BBHI', 0, 128, 0, 77) + bytes([203, 0, 113, 9])
        ip, epoch = n.parse_natpmp_external(packet)
        self.assertEqual(ip, '203.0.113.9')
        self.assertEqual(epoch, 77)

    def test_natpmp_tcp_mapping_round_trip_shape(self):
        req = n.natpmp_map_request('tcp', 8844, 443, 7200)
        self.assertEqual(len(req), 12)
        self.assertEqual(req[:2], b'\x00\x02')
        response = struct.pack('!BBHIHHI', 0, 130, 0, 88, 8844, 443, 7000)
        outside, life, epoch = n.parse_natpmp_map(response, internal_port=8844, protocol='tcp')
        self.assertEqual((outside, life, epoch), (443, 7000, 88))

    def test_pcp_map_packet_and_response_are_bound_by_nonce(self):
        nonce = b'abcdefghijkl'
        req, got = n.pcp_map_request('192.168.1.22', internal_port=8844, external_port=443,
                                     protocol='tcp', lifetime=7200, nonce=nonce)
        self.assertEqual(got, nonce)
        self.assertEqual(len(req), 60)
        self.assertEqual(req[0], 2)
        self.assertEqual(req[1], 1)
        self.assertEqual(req[8:24], b'\x00' * 10 + b'\xff\xff' + bytes([192, 168, 1, 22]))
        external = ipaddress.IPv6Address('::ffff:203.0.113.10').packed
        header = struct.pack('!BBBBII', 2, 0x81, 0, 0, 7100, 100) + b'\x00' * 12
        body = nonce + struct.pack('!B3xHH', 6, 8844, 443) + external
        ip, outside, life, epoch = n.parse_pcp_map(header + body, nonce=nonce, internal_port=8844, protocol='tcp')
        self.assertEqual(ip, '203.0.113.10')
        self.assertEqual((outside, life, epoch), (443, 7100, 100))

    def test_pcp_wrong_nonce_is_rejected(self):
        external = ipaddress.IPv6Address('::ffff:203.0.113.10').packed
        header = struct.pack('!BBBBII', 2, 0x81, 0, 0, 100, 1) + b'\x00' * 12
        body = b'xxxxxxxxxxxx' + struct.pack('!B3xHH', 6, 8844, 443) + external
        with self.assertRaises(ValueError):
            n.parse_pcp_map(header + body, nonce=b'abcdefghijkl', internal_port=8844, protocol='tcp')

    def test_ssdp_location_is_case_insensitive(self):
        packet = b'HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1:5000/root.xml\r\nST: x\r\n\r\n'
        self.assertEqual(n._ssdp_location(packet), 'http://192.168.1.1:5000/root.xml')

    def test_mapping_url_handles_ipv4_ipv6_and_standard_port(self):
        m = n.Mapping('pcp', '203.0.113.4', 443, 8844, 7200)
        self.assertEqual(m.public_url(), 'https://203.0.113.4')
        v6 = n.Mapping('direct-v6', '2001:db8::1', 8844, 8844, 0)
        self.assertEqual(v6.public_url(), 'https://[2001:db8::1]:8844')

    def test_no_external_ip_lookup_is_encoded(self):
        # Native discovery is local-interface/router only. This test prevents a
        # future convenience edit from quietly reintroducing an IP echo service.
        import inspect
        src = inspect.getsource(n)
        for forbidden in ('api.ipify', 'ifconfig.me', 'icanhazip', 'checkip.amazonaws', 'cloudflare.com/cdn-cgi/trace'):
            self.assertNotIn(forbidden, src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
