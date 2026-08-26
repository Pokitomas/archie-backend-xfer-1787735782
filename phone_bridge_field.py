from __future__ import annotations

import argparse
import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import phone_bridge as base
from field_transport import FieldTransport


WIRE = FieldTransport(limit=1024)
_ADAPTERS = {}
_PRESSURE_SECONDS = 900.0
_PRESSURE_LOCK = threading.RLock()
_PRESSURE_UNTIL = 0.0
_PRESSURE_STREAMS: set[str] = set()
_PRESSURE_ORDER: list[str] = []
_LAST_DEFAULT_CHAT_SEQ = -1
_DEFAULT_CHAT_SOURCES = {'default-chat', 'default_chat', 'chatgpt-default', 'chatgpt'}
_CONTROLLER_LOCK = threading.RLock()
_CONTROLLER_KEY = None
_SCENE_KEY = None
_PRESENCE_LOCK = threading.RLock()
_PRESENCE_SHOWN = False


def register_adapter(shape: str):
    key = FieldTransport.shape(shape)
    def deco(fn):
        _ADAPTERS[key] = fn
        return fn
    return deco


def _append(channel: str, *, shape='opaque', payload=None, stream='', revision=0, final=False, meta=None):
    return WIRE.append(channel, shape=shape, payload=payload, stream=stream,
                       revision=revision, final=final, meta=meta or {})


def _result_ok(value) -> bool:
    return isinstance(value, dict) and bool(value.get('ok', True)) and not value.get('error')


def _urgent() -> bool:
    with _PRESSURE_LOCK:
        return time.monotonic() < _PRESSURE_UNTIL


def _arm_pressure() -> bool:
    """Concrete duration exists only below the remote/model-facing field."""
    global _PRESSURE_UNTIL
    try:
        result = base.controller_action({
            'action': 'timebox',
            'seconds': _PRESSURE_SECONDS,
            'label': 'entry-pressure',
            'intent': 'automatic ingress pressure; expose urgency only',
        })
        ok = _result_ok(result)
    except Exception:
        ok = False
    if ok:
        with _PRESSURE_LOCK:
            _PRESSURE_UNTIL = max(_PRESSURE_UNTIL, time.monotonic() + _PRESSURE_SECONDS)
    return ok


def _arm_stream(stream: str) -> bool:
    sid = FieldTransport.stream(stream)
    if not sid:
        return _arm_pressure()
    with _PRESSURE_LOCK:
        if sid in _PRESSURE_STREAMS:
            return True
    if not _arm_pressure():
        return False
    with _PRESSURE_LOCK:
        if sid not in _PRESSURE_STREAMS:
            _PRESSURE_STREAMS.add(sid)
            _PRESSURE_ORDER.append(sid)
            while len(_PRESSURE_ORDER) > 256:
                _PRESSURE_STREAMS.discard(_PRESSURE_ORDER.pop(0))
    return True


def _maybe_arm_default_chat(controller: dict) -> None:
    global _LAST_DEFAULT_CHAT_SEQ
    sensors = controller.get('sensors') if isinstance(controller.get('sensors'), dict) else {}
    seat = sensors.get('seat') if isinstance(sensors.get('seat'), dict) else {}
    latest = seat.get('latest_input') if isinstance(seat.get('latest_input'), dict) else {}
    try:
        seq = int(latest.get('seq'))
    except Exception:
        return
    source = str(latest.get('source') or '').strip().lower()
    if source not in _DEFAULT_CHAT_SOURCES or seq == _LAST_DEFAULT_CHAT_SEQ:
        return
    if _arm_pressure():
        _LAST_DEFAULT_CHAT_SEQ = seq


def _redact_temporal_secret(value, path=()):
    """Keep normal event timestamps while removing hidden pressure clocks."""
    if isinstance(value, list):
        return [_redact_temporal_secret(v, path + ('[]',)) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        k = str(key)
        lower = k.lower()
        if lower.startswith('timebox') or lower.startswith('remaining') or lower in {'deadline', 'deadline_ns'}:
            continue
        if lower == 'time' and ('seat' in path or 'body' in path or 'orientation' in path):
            continue
        out[k] = _redact_temporal_secret(item, path + (lower,))
    return out


def _controller_key(snap: dict):
    sensors = snap.get('sensors') if isinstance(snap.get('sensors'), dict) else {}
    seat = sensors.get('seat') if isinstance(sensors.get('seat'), dict) else {}
    reply = seat.get('reply') if isinstance(seat.get('reply'), dict) else {}
    field = snap.get('field') if isinstance(snap.get('field'), dict) else {}
    front = field.get('field') if isinstance(field.get('field'), dict) else field.get('front') if isinstance(field.get('front'), dict) else {}
    attention = snap.get('attention') if isinstance(snap.get('attention'), dict) else {}
    events = snap.get('events') if isinstance(snap.get('events'), list) else []
    last = events[-1] if events and isinstance(events[-1], dict) else {}
    voice = seat.get('voice') if isinstance(seat.get('voice'), dict) else {}
    return (
        snap.get('run'), snap.get('activity'), snap.get('mutations'), snap.get('phase'), snap.get('receipt'),
        field.get('basis'), field.get('cursor'), front.get('hwnd'), front.get('title'),
        attention.get('seq'), seat.get('active_occupant'), seat.get('input_seq'), seat.get('output_seq'),
        reply.get('seq'), reply.get('revision'), reply.get('done'), reply.get('aborted'), reply.get('fault'),
        reply.get('sha256'), voice.get('seq'), seat.get('ack_seq'), last.get('seq'), last.get('phase'),
    )


def sample_controller(*, force: bool = False) -> dict:
    """Project the one canonical controller LiveField; never recreate it here."""
    global _CONTROLLER_KEY
    snap = base.jget('/controller', 1.6)
    if not isinstance(snap, dict) or not snap.get('ok'):
        raise RuntimeError('controller field unavailable')
    _maybe_arm_default_chat(snap)
    key = _controller_key(snap)
    with _CONTROLLER_LOCK:
        changed = force or key != _CONTROLLER_KEY
        if changed:
            _CONTROLLER_KEY = key
    if changed:
        public = _redact_temporal_secret(snap)
        public['urgency'] = _urgent()
        _append(
            'controller.state',
            shape='application/vnd.archie.controller-livefield+json',
            payload=public,
            revision=int(snap.get('activity') or 0),
            meta={'direction': 'egress', 'authority': 'controller'},
        )
    return snap


def project_aperture(*, force: bool = False) -> None:
    """Expose aperture resources as field values, not hard-coded client paths."""
    global _SCENE_KEY
    with base.LOCK:
        scene = dict(base.SCENE)
    scene_key = json.dumps(scene, sort_keys=True, separators=(',', ':'), default=str)
    if force or scene_key != _SCENE_KEY:
        _SCENE_KEY = scene_key
        _append('surface.scene', shape='application/vnd.archie.scene+json', payload=scene,
                revision=int(scene.get('revision') or 0), meta={'direction': 'egress', 'authority': 'surface'})
    if force:
        _append('machine.screen', shape='application/vnd.archie.stream-ref+json',
                payload={'path': '/api/screen.mjpg', 'transport': 'mjpeg', 'passive': True},
                meta={'direction': 'egress', 'authority': 'machine'})


def controller_observer():
    while not base.STOP.is_set():
        wait = .06
        try:
            snap = sample_controller()
            project_aperture()
            sensors = snap.get('sensors') if isinstance(snap.get('sensors'), dict) else {}
            seat = sensors.get('seat') if isinstance(sensors.get('seat'), dict) else {}
            reply = seat.get('reply') if isinstance(seat.get('reply'), dict) else {}
            wait = .012 if reply and not bool(reply.get('done')) else .045
        except Exception:
            wait = .14
        base.STOP.wait(wait)


def _show_presence_once() -> None:
    global _PRESENCE_SHOWN
    with _PRESENCE_LOCK:
        if _PRESENCE_SHOWN:
            return
        _PRESENCE_SHOWN = True
    try:
        base.controller_action({
            'action': 'undertow',
            'text': '∴',
            'ttl_ms': 1200,
            'intent': 'remote field listening presence',
        })
    except Exception:
        pass
    _append('surface.presence', shape='application/vnd.archie.presence+json',
            payload={'state': 'listening'}, meta={'direction': 'egress', 'authority': 'controller'})


def _binary_meta(headers) -> dict:
    """Decode generic opaque metadata without interpreting its vocabulary."""
    out = {}
    packed = str(headers.get('X-Field-Meta') or '')[:12_000]
    if packed:
        try:
            raw = base64.b64decode(packed + '=' * ((4 - len(packed) % 4) % 4), validate=False)
            value = json.loads(raw.decode('utf-8', 'replace'))
            if isinstance(value, dict):
                out.update(value)
        except Exception:
            pass
    # Legacy shims remain only at the transport boundary for old clients.
    if headers.get('X-Field-Rate'):
        out['rate'] = headers.get('X-Field-Rate')
    if headers.get('X-Field-Preview'):
        out['preview'] = str(headers.get('X-Field-Preview')).lower() in {'1', 'true', 'yes'}
    return out


# Concrete machine interpretations are optional and replaceable. Keeping them
# in a separate module makes this aperture itself ignorant of modality names.
from field_controller_adapters import install as _install_controller_adapters
_install_controller_adapters(register_adapter, base=base, arm_stream=_arm_stream, project_aperture=project_aperture)


class FieldHandler(base.Handler):
    server_version = 'ArchieField/4'

    def _headers(self, code=200, ctype='application/json', length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', self._origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Field-Channel, X-Field-Shape, X-Field-Stream, X-Field-Revision, X-Field-Final, X-Field-Meta, X-Field-Rate, X-Field-Preview')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')

    def _ndjson(self, value: dict):
        self.wfile.write(base._json_bytes(value) + b'\n')
        self.wfile.flush()

    def _stream_field(self):
        self._headers(200, 'application/x-ndjson; charset=utf-8', None)
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            sample_controller(force=True)
            project_aperture(force=True)
        except Exception:
            pass
        cursor = 0
        last_heartbeat = 0.0
        try:
            initial = WIRE.replay(after=0, limit=192)
            cursor = int(initial.get('serial') or 0)
            self._ndjson({'type': 'field', 'seeded_from': 'controller', **initial})
            last_heartbeat = time.time()
            while not base.STOP.is_set():
                current = WIRE.wait_after(cursor, timeout=.75)
                now = time.time()
                if current > cursor:
                    snap = WIRE.replay(after=cursor, limit=192)
                    self._ndjson({'type': 'field', **snap})
                    cursor = int(snap.get('serial') or cursor)
                    last_heartbeat = now
                elif now - last_heartbeat >= 8.0:
                    self._ndjson({'type': 'heartbeat', 'serial': cursor, 'at': now})
                    last_heartbeat = now
        except Exception:
            return

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {'/api/health', '/api/selftest', '/api/screen', '/api/screen.mjpg'}:
            return base.Handler.do_GET(self)
        if path not in {'/api/field.ndjson', '/api/field'}:
            self.sendb(404, b'{"ok":false,"error":"not_found"}')
            return
        if not self._token():
            self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
            return
        if path == '/api/field.ndjson':
            self._stream_field()
            return
        q = parse_qs(urlparse(self.path).query)
        try:
            after = int((q.get('after') or ['0'])[0])
        except Exception:
            after = 0
        try:
            sample_controller(force=(after == 0))
            if after == 0:
                project_aperture(force=True)
        except Exception:
            pass
        self.sendb(200, base._json_bytes({'ok': True, **WIRE.replay(after=after, limit=192)}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in {'/api/session', '/api/field'}:
            self.sendb(404, b'{"ok":false,"error":"not_found"}')
            return
        if not self._token():
            self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
            return
        if path == '/api/session':
            if not base.SELFTEST.get('ok'):
                self.sendb(503, base._json_bytes(base.SELFTEST))
                return
            self.sendb(200, b'{"ok":true}', extra={'Set-Cookie': f'archie_phone={base.TOKEN}; Path=/; HttpOnly; Secure; SameSite=None'})
            _show_presence_once()
            return

        n = min(8_000_000, int(self.headers.get('Content-Length') or 0))
        raw = self.rfile.read(n) if n else b''
        content_type = str(self.headers.get('Content-Type') or '').lower()
        if 'application/json' in content_type:
            try:
                event = json.loads(raw.decode('utf-8', 'replace') or '{}')
            except Exception:
                event = {}
            if not isinstance(event, dict):
                event = {}
            binary = b''
        else:
            event = {
                'channel': self.headers.get('X-Field-Channel') or 'user.primary',
                'shape': self.headers.get('X-Field-Shape') or content_type or 'application/octet-stream',
                'stream': self.headers.get('X-Field-Stream') or '',
                'revision': self.headers.get('X-Field-Revision') or 0,
                'final': str(self.headers.get('X-Field-Final') or '').lower() in {'1', 'true', 'yes'},
                'payload': None,
                'meta': _binary_meta(self.headers),
            }
            binary = raw
        channel = FieldTransport.channel(event.get('channel') or 'user.primary')
        shape = FieldTransport.shape(event.get('shape') or 'opaque')
        stream = FieldTransport.stream(event.get('stream') or '')
        try:
            revision = max(0, int(event.get('revision') or 0))
        except Exception:
            revision = 0
        final = bool(event.get('final'))
        meta = event.get('meta') if isinstance(event.get('meta'), dict) else {}
        if channel.startswith('user.') and not _arm_stream(stream):
            self.sendb(503, b'{"ok":false,"error":"entry_pressure"}')
            return
        ingress = _append(channel, shape=shape, payload=binary if binary else event.get('payload'),
                          stream=stream, revision=revision, final=final, meta={**meta, 'direction': 'ingress'})
        adapter = _ADAPTERS.get(shape)
        adapted = adapter is not None
        result = {'ok': True, 'accepted': True, 'adapted': False}
        if adapter is not None:
            try:
                result = adapter(event, binary)
                if not isinstance(result, dict):
                    result = {'ok': True, 'value': result}
            except Exception as exc:
                result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        ok = bool(result.get('ok', True)) and not result.get('error')
        _append(channel + '.receipt', shape='application/vnd.archie.receipt+json',
                payload={'for_serial': ingress.serial, 'adapted': adapted, 'ok': ok, 'error': result.get('error')},
                stream=stream, revision=revision, final=True, meta={'direction': 'egress', 'authority': 'bridge'})
        self.sendb(202 if ok else 503, base._json_bytes({'ok': ok, 'field_serial': ingress.serial, 'adapted': adapted, 'result': result}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True)
    ap.add_argument('--topic', default='')
    args = ap.parse_args()
    base.TOKEN = args.token.strip()
    base.TOPIC = args.topic.strip()
    if len(base.TOKEN) < 24:
        raise SystemExit('token too short')
    server = base.ThreadingHTTPServer(('127.0.0.1', base.PORT), FieldHandler)
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, name='archie-field-http', daemon=True)
    serving.start()
    test = base.run_selftest()
    print('SELFTEST', json.dumps(test, separators=(',', ':')), flush=True)
    if not test.get('ok'):
        base.signal('field withheld', json.dumps(test.get('checks', {}), separators=(',', ':')))
        base.STOP.wait(2.0)
        server.shutdown(); server.server_close()
        raise SystemExit(3)
    base.signal('field selftest passed', json.dumps(test.get('checks', {}), separators=(',', ':')))
    threading.Thread(target=base.scene_worker, name='archie-field-scene', daemon=True).start()
    threading.Thread(target=controller_observer, name='archie-controller-field-projection', daemon=True).start()
    threading.Thread(target=base.tunnel_worker, name='archie-field-tunnel', daemon=True).start()
    print(f'ARCHIE FIELD local http://127.0.0.1:{base.PORT}', flush=True)
    try:
        while serving.is_alive() and not base.STOP.wait(.5):
            pass
    finally:
        base.STOP.set()
        server.shutdown(); server.server_close()


base.Handler = FieldHandler

if __name__ == '__main__':
    main()
