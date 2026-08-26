from __future__ import annotations

import argparse
import json
import mimetypes
import os
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import field_entry_server as entry
import phone_bridge as base
import phone_bridge_field as field


ROOT = Path(__file__).resolve().parent
STATIC = {
    '/': ('native_index.html', 'text/html; charset=utf-8'),
    '/index.html': ('native_index.html', 'text/html; charset=utf-8'),
    '/field.js': ('native_field_client.js', 'text/javascript; charset=utf-8'),
}


class NativeFieldHandler(entry.EntryFieldHandler):
    server_version = 'ArchieNativeField/1'

    def _static(self, path: str) -> bool:
        item = STATIC.get(path)
        if not item:
            return False
        name, ctype = item
        p = ROOT / name
        try:
            raw = p.read_bytes()
        except Exception:
            self.sendb(404, b'not found', 'text/plain; charset=utf-8')
            return True
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), geolocation=(), microphone=(self)')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(raw)
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if self._static(path):
            return
        if path == '/favicon.ico':
            self.sendb(204, b'', 'image/x-icon')
            return
        return super().do_GET()


# All runtime API traffic uses the direct handler. No cloudflared/ntfy worker is
# started by this module.
entry.EntryFieldHandler = NativeFieldHandler
field.FieldHandler = NativeFieldHandler
base.Handler = NativeFieldHandler


def load_local_scene() -> None:
    path = ROOT / 'phone_scene.json'
    last = None
    while not base.STOP.is_set():
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            if stamp != last:
                value = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    with base.LOCK:
                        base.SCENE.clear(); base.SCENE.update(value)
                        base.SCENE['revision'] = int(base.SCENE.get('revision') or 0) + 1
                        base.BUS['event'] = 'scene:local'
                    field.project_aperture(force=True)
                    last = stamp
        except Exception:
            pass
        base.STOP.wait(.35)


def _announce(public_url: str, token: str) -> None:
    if not public_url:
        return
    clean = public_url.rstrip('/')
    with base.LOCK:
        base.PUBLIC_URL = clean
        base.TUNNEL_KIND = 'native-direct'
        base.BUS['event'] = 'native-direct:' + clean
        base.BUS['transport_revision'] = int(base.BUS.get('transport_revision') or 0) + 1
    field._append(
        'surface.endpoint',
        shape='application/vnd.archie.endpoint+json',
        payload={
            'url': clean,
            'surface_url': clean + '/#t=' + token,
            'mcp_url': clean + '/mcp?t=' + token,
            'transport': 'native-direct-https',
            'hosted_relay': False,
            'rendezvous': False,
        },
        final=True,
        meta={'direction': 'egress', 'authority': 'machine'},
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True)
    ap.add_argument('--cert', required=True)
    ap.add_argument('--key', required=True)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8844)
    ap.add_argument('--public-url', default='')
    args = ap.parse_args()

    base.TOKEN = args.token.strip()
    base.TOPIC = ''
    base.PORT = int(args.port)
    if len(base.TOKEN) < 24:
        raise SystemExit('token too short')
    cert = Path(args.cert).resolve(); key = Path(args.key).resolve()
    if not cert.is_file() or not key.is_file():
        raise SystemExit('TLS material missing')

    base.STOP.clear()
    server = base.ThreadingHTTPServer((args.host, int(args.port)), NativeFieldHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .04}, name='archie-native-field-http', daemon=True)
    serving.start()

    test = base.run_selftest()
    print('SELFTEST', json.dumps(test, separators=(',', ':')), flush=True)
    if not test.get('ok'):
        server.shutdown(); server.server_close()
        raise SystemExit(3)

    try:
        value = json.loads((ROOT / 'phone_scene.json').read_text(encoding='utf-8'))
        if isinstance(value, dict):
            with base.LOCK:
                base.SCENE.clear(); base.SCENE.update(value)
    except Exception:
        pass

    field.sample_controller(force=True)
    field.project_aperture(force=True)
    _announce(args.public_url, base.TOKEN)
    threading.Thread(target=load_local_scene, name='archie-native-scene', daemon=True).start()
    threading.Thread(target=field.controller_observer, name='archie-native-controller-field', daemon=True).start()
    print(f'ARCHIE NATIVE FIELD https://{args.host}:{args.port}', flush=True)
    try:
        while serving.is_alive() and not base.STOP.wait(.5):
            pass
    finally:
        base.STOP.set(); server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
