from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any


PCP_PORT = 5351
SSDP_ADDR = ('239.255.255.250', 1900)


@dataclass(frozen=True, slots=True)
class Mapping:
    kind: str
    external_ip: str
    external_port: int
    internal_port: int
    lifetime: int
    gateway: str = ''
    control_url: str = ''

    def public_url(self, *, scheme: str = 'https') -> str:
        host = self.external_ip
        if ':' in host:
            host = f'[{host}]'
        port = '' if (scheme == 'https' and self.external_port == 443) or (scheme == 'http' and self.external_port == 80) else f':{self.external_port}'
        return f'{scheme}://{host}{port}'


def _run_ps(script: str, timeout: float = 4.0) -> str:
    if os.name != 'nt':
        return ''
    try:
        p = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding='utf-8', errors='replace', timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000),
        )
        return p.stdout.strip() if p.returncode == 0 else ''
    except Exception:
        return ''


def default_gateway() -> str:
    """Return the active IPv4 default gateway without an Internet lookup."""
    text = _run_ps(
        "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' "
        "| Where-Object {$_.State -ne 'Unreachable'} | Sort-Object RouteMetric,InterfaceMetric "
        "| Select-Object -First 1 -ExpandProperty NextHop"
    )
    try:
        ip = ipaddress.ip_address(text.strip())
        if ip.version == 4 and not ip.is_unspecified:
            return str(ip)
    except Exception:
        pass
    if os.name == 'nt':
        try:
            p = subprocess.run(['route.exe', 'print', '-4'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding='utf-8', errors='replace', timeout=3,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
            for line in p.stdout.splitlines():
                m = re.match(r'^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+', line)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ''


def global_ipv6_candidates() -> list[str]:
    """Enumerate directly routable local IPv6 addresses; no STUN/HTTP service."""
    values: list[str] = []
    if os.name == 'nt':
        text = _run_ps(
            "Get-NetIPAddress -AddressFamily IPv6 | Where-Object {$_.AddressState -eq 'Preferred'} "
            "| Select-Object -ExpandProperty IPAddress",
            timeout=5,
        )
        raw = re.split(r'[\r\n\s]+', text)
    else:
        raw = []
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
                raw.append(item[4][0])
        except Exception:
            pass
    for value in raw:
        value = value.split('%', 1)[0].strip()
        if not value:
            continue
        try:
            ip = ipaddress.ip_address(value)
        except Exception:
            continue
        if ip.version != 6 or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            continue
        # ULA is intentionally not accepted as public reachability.
        if ip in ipaddress.ip_network('fc00::/7'):
            continue
        s = str(ip)
        if s not in values:
            values.append(s)
    return values


def natpmp_external_request() -> bytes:
    return b'\x00\x00'


def parse_natpmp_external(data: bytes) -> tuple[str, int]:
    if len(data) < 12:
        raise ValueError('short nat-pmp external response')
    version, opcode, result, epoch = struct.unpack('!BBHI', data[:8])
    if version != 0 or opcode != 128 or result != 0:
        raise ValueError(f'nat-pmp external rejected v={version} op={opcode} result={result}')
    return socket.inet_ntoa(data[8:12]), int(epoch)


def natpmp_map_request(protocol: str, internal_port: int, external_port: int, lifetime: int) -> bytes:
    opcode = 1 if protocol.lower() == 'udp' else 2 if protocol.lower() == 'tcp' else None
    if opcode is None:
        raise ValueError('protocol')
    return struct.pack('!BBHHHI', 0, opcode, 0, int(internal_port), int(external_port), int(lifetime))


def parse_natpmp_map(data: bytes, *, internal_port: int, protocol: str) -> tuple[int, int, int]:
    if len(data) < 16:
        raise ValueError('short nat-pmp map response')
    version, opcode, result, epoch, inside, outside, lifetime = struct.unpack('!BBHIHHI', data[:16])
    expected = 129 if protocol.lower() == 'udp' else 130
    if version != 0 or opcode != expected or result != 0 or inside != int(internal_port):
        raise ValueError(f'nat-pmp map rejected v={version} op={opcode} result={result} inside={inside}')
    return int(outside), int(lifetime), int(epoch)


def natpmp_map(gateway: str, *, internal_port: int, external_port: int, protocol: str = 'tcp', lifetime: int = 7200, timeout: float = .7) -> Mapping:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(natpmp_external_request(), (gateway, PCP_PORT))
        external_ip, _ = parse_natpmp_external(s.recvfrom(64)[0])
        req = natpmp_map_request(protocol, internal_port, external_port, lifetime)
        s.sendto(req, (gateway, PCP_PORT))
        outside, granted, _ = parse_natpmp_map(s.recvfrom(64)[0], internal_port=internal_port, protocol=protocol)
    return Mapping('nat-pmp', external_ip, outside, internal_port, granted, gateway=gateway)


def _v4_mapped_16(ip: str) -> bytes:
    addr = ipaddress.ip_address(ip)
    if addr.version == 6:
        return addr.packed
    return b'\x00' * 10 + b'\xff\xff' + addr.packed


def pcp_map_request(client_ip: str, *, internal_port: int, external_port: int, protocol: str = 'tcp', lifetime: int = 7200, nonce: bytes | None = None) -> tuple[bytes, bytes]:
    proto = 6 if protocol.lower() == 'tcp' else 17 if protocol.lower() == 'udp' else None
    if proto is None:
        raise ValueError('protocol')
    nonce = nonce or uuid.uuid4().bytes[:12]
    if len(nonce) != 12:
        raise ValueError('nonce')
    header = struct.pack('!BBHI', 2, 1, 0, int(lifetime)) + _v4_mapped_16(client_ip)
    body = nonce + struct.pack('!B3xHH', proto, int(internal_port), int(external_port)) + (b'\x00' * 16)
    return header + body, nonce


def parse_pcp_map(data: bytes, *, nonce: bytes, internal_port: int, protocol: str) -> tuple[str, int, int, int]:
    if len(data) < 60:
        raise ValueError('short pcp map response')
    version, opcode, reserved, result, lifetime, epoch = struct.unpack('!BBBBII', data[:12])
    if version != 2 or opcode != 0x81 or result != 0:
        raise ValueError(f'pcp map rejected v={version} op={opcode} result={result}')
    body = data[24:]
    got_nonce = body[:12]
    proto, inside, outside = struct.unpack('!B3xHH', body[12:20])
    expected = 6 if protocol.lower() == 'tcp' else 17
    if got_nonce != nonce or proto != expected or inside != int(internal_port):
        raise ValueError('pcp map response does not match request')
    raw_ip = body[20:36]
    ip6 = ipaddress.IPv6Address(raw_ip)
    if ip6.ipv4_mapped:
        external_ip = str(ip6.ipv4_mapped)
    else:
        external_ip = str(ip6)
    return external_ip, int(outside), int(lifetime), int(epoch)


def local_ipv4_for_gateway(gateway: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((gateway, 9))
            return str(s.getsockname()[0])
    except Exception:
        return '0.0.0.0'


def pcp_map(gateway: str, *, internal_port: int, external_port: int, protocol: str = 'tcp', lifetime: int = 7200, timeout: float = .8) -> Mapping:
    client_ip = local_ipv4_for_gateway(gateway)
    request, nonce = pcp_map_request(client_ip, internal_port=internal_port, external_port=external_port,
                                     protocol=protocol, lifetime=lifetime)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(request, (gateway, PCP_PORT))
        data = s.recvfrom(256)[0]
    external_ip, outside, granted, _ = parse_pcp_map(data, nonce=nonce, internal_port=internal_port, protocol=protocol)
    return Mapping('pcp', external_ip, outside, internal_port, granted, gateway=gateway)


def _ssdp_location(packet: bytes) -> str:
    text = packet.decode('iso-8859-1', 'replace')
    for line in text.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            if key.strip().lower() == 'location':
                return value.strip()
    return ''


def upnp_discover(timeout: float = .7) -> list[str]:
    msg = ('M-SEARCH * HTTP/1.1\r\n'
           'HOST: 239.255.255.250:1900\r\n'
           'MAN: "ssdp:discover"\r\n'
           'MX: 1\r\n'
           'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n').encode('ascii')
    locations: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.settimeout(timeout)
        try:
            s.sendto(msg, SSDP_ADDR)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, _ = s.recvfrom(65535)
                except socket.timeout:
                    break
                loc = _ssdp_location(data)
                if loc and loc not in locations:
                    locations.append(loc)
        except OSError:
            pass
    return locations


def _xml_local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def upnp_control(location: str, timeout: float = 1.6) -> tuple[str, str]:
    with urllib.request.urlopen(urllib.request.Request(location, headers={'User-Agent': 'ARCHIE-native/1'}), timeout=timeout) as r:
        raw = r.read(1_000_000)
    root = ET.fromstring(raw)
    base_url = location
    for node in root.iter():
        if _xml_local(node.tag) == 'URLBase' and (node.text or '').strip():
            base_url = node.text.strip()
            break
    for service in root.iter():
        if _xml_local(service.tag) != 'service':
            continue
        fields = {_xml_local(x.tag): (x.text or '').strip() for x in list(service)}
        st = fields.get('serviceType', '')
        if 'WANIPConnection' in st or 'WANPPPConnection' in st:
            control = urllib.parse.urljoin(base_url, fields.get('controlURL', ''))
            if control:
                return control, st
    raise RuntimeError('UPnP WAN connection service not found')


def _soap(control: str, service: str, action: str, fields: dict[str, Any], timeout: float = 1.8) -> bytes:
    args = ''.join(f'<New{k}>{v}</New{k}>' for k, v in fields.items())
    body = (f'<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}">{args}</u:{action}></s:Body></s:Envelope>').encode('utf-8')
    req = urllib.request.Request(control, data=body, method='POST', headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': f'"{service}#{action}"',
        'Connection': 'close',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1_000_000)


def upnp_external_ip(control: str, service: str) -> str:
    raw = _soap(control, service, 'GetExternalIPAddress', {})
    root = ET.fromstring(raw)
    for node in root.iter():
        if _xml_local(node.tag) == 'NewExternalIPAddress':
            return (node.text or '').strip()
    return ''


def upnp_map(control: str, service: str, *, local_ip: str, internal_port: int, external_port: int,
             protocol: str = 'TCP', lifetime: int = 7200, description: str = 'ARCHIE live field') -> Mapping:
    proto = protocol.upper()
    if proto not in {'TCP', 'UDP'}:
        raise ValueError('protocol')
    _soap(control, service, 'AddPortMapping', {
        'RemoteHost': '',
        'ExternalPort': int(external_port),
        'Protocol': proto,
        'InternalPort': int(internal_port),
        'InternalClient': local_ip,
        'Enabled': 1,
        'PortMappingDescription': description,
        'LeaseDuration': max(0, int(lifetime)),
    })
    external_ip = upnp_external_ip(control, service)
    return Mapping('upnp-igd', external_ip, int(external_port), int(internal_port), int(lifetime), control_url=control)


def map_port(*, internal_port: int, external_port: int, protocol: str = 'tcp', lifetime: int = 7200) -> tuple[Mapping | None, list[str]]:
    """Try native router protocols only. Returns proof trail for diagnostics."""
    errors: list[str] = []
    gateway = default_gateway()
    if gateway:
        for name, fn in [('pcp', pcp_map), ('nat-pmp', natpmp_map)]:
            try:
                mapping = fn(gateway, internal_port=internal_port, external_port=external_port,
                             protocol=protocol, lifetime=lifetime)
                if mapping.external_ip and mapping.external_port:
                    return mapping, errors
            except Exception as exc:
                errors.append(f'{name}:{type(exc).__name__}:{exc}')
    else:
        errors.append('gateway:unavailable')
    try:
        locations = upnp_discover()
    except Exception as exc:
        locations = []
        errors.append(f'upnp-discovery:{type(exc).__name__}:{exc}')
    local_ip = local_ipv4_for_gateway(gateway) if gateway else ''
    for loc in locations[:4]:
        try:
            control, service = upnp_control(loc)
            mapping = upnp_map(control, service, local_ip=local_ip, internal_port=internal_port,
                               external_port=external_port, protocol=protocol, lifetime=lifetime)
            if mapping.external_ip:
                return mapping, errors
        except Exception as exc:
            errors.append(f'upnp:{type(exc).__name__}:{exc}')
    return None, errors


def direct_candidates(*, https_port: int = 8844) -> dict[str, Any]:
    """Probe native paths without opening anything yet."""
    ipv6 = global_ipv6_candidates()
    gateway = default_gateway()
    return {
        'ipv6': [{'ip': ip, 'url': f'https://[{ip}]:{int(https_port)}'} for ip in ipv6],
        'gateway': gateway,
        'router_protocols': ['pcp', 'nat-pmp', 'upnp-igd'],
        'requires_public_tls': True,
    }
