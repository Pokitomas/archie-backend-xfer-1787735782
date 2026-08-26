from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import native_aperture as net
import native_secret


HERE = Path(__file__).resolve().parent


@dataclass(slots=True)
class Route:
    kind: str
    identifier: str
    bind_host: str
    internal_https_port: int
    public_https_port: int
    challenge_port: int
    public_url: str
    mapping: dict[str, Any] | None = None


def is_public_ip(value: str) -> bool:
    try:
        return bool(ipaddress.ip_address(value).is_global)
    except Exception:
        return False


def _creationflags() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if os.name == 'nt' else 0


def firewall_allow(ports: list[int]) -> list[str]:
    """Best-effort Windows firewall aperture. Lack of elevation is reported, not hidden."""
    errors: list[str] = []
    if os.name != 'nt':
        return errors
    for port in sorted(set(int(x) for x in ports if int(x) > 0)):
        name = f'ARCHIE native field {port}'
        try:
            p = subprocess.run(
                ['netsh.exe', 'advfirewall', 'firewall', 'add', 'rule', f'name={name}', 'dir=in', 'action=allow',
                 'protocol=TCP', f'localport={port}', 'profile=private,public'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=5,
                creationflags=_creationflags(),
            )
            if p.returncode != 0:
                errors.append(f'firewall:{port}:{(p.stderr or "refused").strip()[:160]}')
        except Exception as exc:
            errors.append(f'firewall:{port}:{type(exc).__name__}:{exc}')
    return errors


def can_bind(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            try:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except Exception:
                pass
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def choose_ipv6() -> tuple[Route | None, list[str]]:
    errors: list[str] = []
    values = net.global_ipv6_candidates()
    if not values:
        return None, ['ipv6:none']
    if not can_bind('::', 80):
        return None, ['ipv6:port80-unavailable']
    if not can_bind('::', 443):
        return None, ['ipv6:port443-unavailable']
    for ip in values:
        if is_public_ip(ip):
            return Route('direct-ipv6', ip, '::', 443, 443, 80, f'https://[{ip}]'), errors
        errors.append(f'ipv6:not-global:{ip}')
    return None, errors


def choose_ipv4(*, lifetime: int = 7200) -> tuple[Route | None, list[str]]:
    errors: list[str] = []
    challenge, e = net.map_port(internal_port=8080, external_port=80, protocol='tcp', lifetime=min(900, lifetime))
    errors.extend(e)
    if not challenge:
        return None, errors + ['ipv4:http01-map-unavailable']
    if challenge.external_port != 80:
        return None, errors + [f'ipv4:http01-not-80:{challenge.external_port}']
    if not is_public_ip(challenge.external_ip):
        return None, errors + [f'ipv4:not-global:{challenge.external_ip}']
    https, e2 = net.map_port(internal_port=8844, external_port=443, protocol='tcp', lifetime=lifetime)
    errors.extend(e2)
    if not https:
        return None, errors + ['ipv4:https-map-unavailable']
    if https.external_port != 443:
        return None, errors + [f'ipv4:https-not-443:{https.external_port}']
    if https.external_ip != challenge.external_ip:
        return None, errors + [f'ipv4:public-ip-changed:{challenge.external_ip}->{https.external_ip}']
    return Route(
        'mapped-ipv4', https.external_ip, '0.0.0.0', 8844, 443, 8080,
        f'https://{https.external_ip}', mapping=asdict(https),
    ), errors


def choose_route(*, prefer_ipv6: bool = True, lifetime: int = 7200) -> tuple[Route | None, list[str]]:
    errors: list[str] = []
    order = (choose_ipv6, lambda: choose_ipv4(lifetime=lifetime)) if prefer_ipv6 else (lambda: choose_ipv4(lifetime=lifetime), choose_ipv6)
    for fn in order:
        route, detail = fn()
        errors.extend(detail)
        if route:
            return route, errors
    return None, errors


def cert_info(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {'ok': False, 'reason': 'missing'}
    try:
        data = ssl._ssl._test_decode_cert(str(path))  # stdlib test decoder; no crypto package
        expiry = ssl.cert_time_to_seconds(data['notAfter'])
        sans = [v for k, v in data.get('subjectAltName', ()) if k == 'IP Address']
        return {'ok': True, 'expires': expiry, 'sans': sans, 'seconds_left': expiry - time.time()}
    except Exception as exc:
        return {'ok': False, 'reason': f'{type(exc).__name__}:{exc}'}


def certificate_ready(cert: Path, identifier: str, *, minimum_left: float = 48 * 3600) -> bool:
    info = cert_info(cert)
    return bool(info.get('ok') and identifier in info.get('sans', []) and float(info.get('seconds_left') or 0) > minimum_left)


def issue_certificate(route: Route, *, cert: Path, key: Path, account: Path) -> dict[str, Any]:
    script = HERE / 'field_acme.ps1'
    if not script.is_file():
        return {'ok': False, 'error': 'field_acme.ps1 missing'}
    cmd = [
        'powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
        '-Identifier', route.identifier, '-CertPath', str(cert), '-KeyPath', str(key),
        '-AccountKeyPath', str(account), '-ChallengePort', str(route.challenge_port),
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                           timeout=300, creationflags=_creationflags())
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}:{exc}'}
    if p.returncode != 0:
        return {'ok': False, 'error': (p.stderr or p.stdout or f'exit {p.returncode}')[-1200:]}
    line = (p.stdout or '').strip().splitlines()[-1:] or ['{}']
    try:
        value = json.loads(line[0])
    except Exception:
        value = {'ok': True, 'stdout': (p.stdout or '')[-800:]}
    value['ok'] = bool(value.get('ok', True))
    return value


def start_server(route: Route, *, token: str, cert: Path, key: Path, logs: Path) -> subprocess.Popen:
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / 'native-field.out.log', 'ab', buffering=0)
    err = open(logs / 'native-field.err.log', 'ab', buffering=0)
    cmd = [
        sys.executable, str(HERE / 'native_field_server.py'), '--token', token, '--cert', str(cert), '--key', str(key),
        '--host', route.bind_host, '--port', str(route.internal_https_port), '--public-url', route.public_url,
    ]
    return subprocess.Popen(cmd, cwd=str(HERE), stdout=out, stderr=err, creationflags=_creationflags())


def local_health(route: Route, token: str, *, timeout: float = 7.0) -> bool:
    deadline = time.monotonic() + timeout
    ctx = ssl._create_unverified_context()
    host = '[::1]' if ':' in route.bind_host else '127.0.0.1'
    url = f'https://{host}:{route.internal_https_port}/api/health?t={token}'
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=.8) as r:
                value = json.loads(r.read(100_000))
            if value.get('ok'):
                return True
        except Exception:
            time.sleep(.12)
    return False


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, separators=(',', ':'), default=str), encoding='utf-8')
    os.replace(tmp, path)


def refresh_ipv4(route: Route, *, lifetime: int) -> tuple[Route | None, list[str]]:
    if route.kind != 'mapped-ipv4':
        return route, []
    mapping, errors = net.map_port(internal_port=route.internal_https_port, external_port=443, protocol='tcp', lifetime=lifetime)
    if not mapping or mapping.external_port != 443 or not is_public_ip(mapping.external_ip):
        return None, errors + ['refresh:https-map-failed']
    if mapping.external_ip != route.identifier:
        return None, errors + [f'refresh:identifier-changed:{route.identifier}->{mapping.external_ip}']
    route.mapping = asdict(mapping)
    return route, errors


def run(*, initial_token: str = '', token_file: Path, prefer_ipv6: bool = True, once: bool = False) -> int:
    root = token_file.parent
    cert = root / 'tls' / 'field-cert.pem'
    key = root / 'tls' / 'field-key.pem'
    account = root / 'tls' / 'acme-account-key.pem'
    state_file = root / 'native-state.json'
    logs = root / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    if initial_token:
        if len(initial_token) < 24:
            raise ValueError('token too short')
        native_secret.save(token_file, initial_token)
    token = native_secret.load(token_file)
    if len(token) < 24:
        raise RuntimeError('stored token invalid')

    route, errors = choose_route(prefer_ipv6=prefer_ipv6)
    if not route:
        write_state(state_file, {'status': 'native-unavailable', 'errors': errors, 'at': time.time()})
        return 4
    errors.extend(firewall_allow([route.internal_https_port, route.challenge_port]))

    if not certificate_ready(cert, route.identifier):
        issued = issue_certificate(route, cert=cert, key=key, account=account)
        if not issued.get('ok') or not certificate_ready(cert, route.identifier, minimum_left=3600):
            write_state(state_file, {'status': 'certificate-refused', 'route': asdict(route), 'issue': issued, 'errors': errors, 'at': time.time()})
            return 5

    child = start_server(route, token=token, cert=cert, key=key, logs=logs)
    if not local_health(route, token):
        child.terminate()
        write_state(state_file, {'status': 'server-refused', 'route': asdict(route), 'errors': errors, 'at': time.time()})
        return 6

    write_state(state_file, {
        'status': 'native-live', 'route': asdict(route), 'pid': child.pid,
        'surface_url': route.public_url + '/#t=' + token,
        'mcp_url': route.public_url + '/mcp?t=' + token,
        'certificate': cert_info(cert), 'errors': errors, 'at': time.time(),
    })
    if once:
        return 0

    while child.poll() is None:
        time.sleep(60)
        if route.kind == 'mapped-ipv4':
            refreshed, e = refresh_ipv4(route, lifetime=7200)
            errors.extend(e[-4:])
            if refreshed is None:
                write_state(state_file, {'status': 'route-lost', 'route': asdict(route), 'errors': errors[-16:], 'at': time.time()})
                child.terminate(); return 7
        if not certificate_ready(cert, route.identifier):
            # Reopen HTTP-01 mapping on mapped IPv4 before issuance.
            if route.kind == 'mapped-ipv4':
                challenge, e = net.map_port(internal_port=route.challenge_port, external_port=80, protocol='tcp', lifetime=900)
                errors.extend(e[-4:])
                if not challenge or challenge.external_port != 80 or challenge.external_ip != route.identifier:
                    continue
            issued = issue_certificate(route, cert=cert, key=key, account=account)
            if issued.get('ok') and certificate_ready(cert, route.identifier, minimum_left=3600):
                child.terminate()
                try:
                    child.wait(timeout=4)
                except Exception:
                    child.kill()
                child = start_server(route, token=token, cert=cert, key=key, logs=logs)
                if not local_health(route, token):
                    return 8
        write_state(state_file, {
            'status': 'native-live', 'route': asdict(route), 'pid': child.pid,
            'surface_url': route.public_url + '/#t=' + token,
            'mcp_url': route.public_url + '/mcp?t=' + token,
            'certificate': cert_info(cert), 'errors': errors[-16:], 'at': time.time(),
        })
    return int(child.returncode or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', default='')
    ap.add_argument('--token-file', default=str(Path(os.getenv('LOCALAPPDATA', str(HERE))) / 'ARCHIE' / 'field' / 'native' / 'token.dpapi'))
    ap.add_argument('--prefer-ipv4', action='store_true')
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args()
    raise SystemExit(run(initial_token=args.token, token_file=Path(args.token_file), prefer_ipv6=not args.prefer_ipv4, once=args.once))


if __name__ == '__main__':
    main()
