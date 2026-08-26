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
_CALL_LOCK = threading.RLock()
_CALLS: dict[str, dict] = {}
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
        # Use the controller's existing transient aperture. No second window,
        # renderer, process, or notification system is created.
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


@register_adapter('utf8')
def adapt_utf8(event: dict, raw: bytes):
    stream = FieldTransport.stream(event.get('stream'))
    if not _arm_stream(stream):
        return {'ok': False, 'error': 'entry_pressure'}
    payload = event.get('payload')
    value = payload.get('value', '') if isinstance(payload, dict) else payload
    text = str(value or '')
    if not bool(event.get('final')):
        return {'ok': True, 'accepted': True, 'committed': False}
    if not text or len(text) > 4000:
        return {'ok': False, 'error': 'text_size'}
    accepted = base.jpost('/phone/text', {
        'text': text,
        'client_sent_ms': (event.get('meta') or {}).get('client_sent_ms') if isinstance(event.get('meta'), dict) else None,
        'bridge_received_ms': int(time.time() * 1000),
    }, 2.5)
    return {'ok': _result_ok(accepted), 'accepted': accepted, 'committed': True}


@register_adapter('application/vnd.archie.contact+json')
def adapt_contact(event: dict, raw: bytes):
    stream = FieldTransport.stream(event.get('stream')) or 'contact'
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    active = bool(payload.get('active'))
    if active:
        if not _arm_stream(stream):
            return {'ok': False, 'error': 'entry_pressure'}
        opened = base.jpost('/phone/audio/begin', {'client_started_ms': payload.get('client_started_ms')}, 2.0)
        if not _result_ok(opened):
            return {'ok': False, 'error': 'contact_begin', 'controller': opened}
        with _CALL_LOCK:
            _CALLS[stream] = {'call_id': str(opened.get('call_id') or ''), 'opened': time.monotonic()}
        return {'ok': True, 'active': True, 'call_id': opened.get('call_id'), 'ack': opened.get('ack')}
    with _CALL_LOCK:
        current = dict(_CALLS.get(stream) or {})
    return {'ok': True, 'active': False, 'call_id': current.get('call_id')}


@register_adapter('audio/pcm;codec=s16le')
@register_adapter('pcm_s16le')
def adapt_pcm(event: dict, raw: bytes):
    stream = FieldTransport.stream(event.get('stream')) or 'contact'
    if not _arm_stream(stream):
        return {'ok': False, 'error': 'entry_pressure'}
    if not raw:
        payload = event.get('payload')
        if isinstance(payload, dict) and payload.get('base64'):
            try:
                raw = base64.b64decode(str(payload['base64']), validate=False)
            except Exception:
                raw = b''
    if len(raw) < 6400 or len(raw) > 640000 or len(raw) % 2:
        return {'ok': False, 'error': 'pcm_window', 'bytes': len(raw)}
    meta = event.get('meta') if isinstance(event.get('meta'), dict) else {}
    try:
        rate = int(meta.get('rate') or 16000)
    except Exception:
        rate = 16000
    with _CALL_LOCK:
        call = dict(_CALLS.get(stream) or {})
    call_id = str(meta.get('call_id') or call.get('call_id') or '')
    preview = bool(meta.get('preview')) and not bool(event.get('final'))
    path = '/phone/audio/preview' if preview else '/phone/audio'
    body = {'sample_rate': rate, 'pcm16_base64': base64.b64encode(raw).decode('ascii'), 'call_id': call_id}
    if isinstance(meta.get('speech_evidence'), bool):
        body['speech_evidence'] = meta['speech_evidence']
    accepted = base.jpost(path, body, 3.0)
    if not preview and call_id:
        with _CALL_LOCK:
            _CALLS.pop(stream, None)
    return {'ok': _result_ok(accepted), 'accepted': accepted, 'preview': preview, 'call_id': call_id}


@register_adapter('application/vnd.archie.action+json')
def adapt_action(event: dict, raw: bytes):
    if not _arm_stream(FieldTransport.stream(event.get('stream'))):
        return {'ok': False, 'error': 'entry_pressure'}
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    if not payload.get('action'):
        return {'ok': False, 'error': 'action'}
    result = base.controller_action(payload)
    return {'ok': _result_ok(result), 'result': result}


@register_adapter('application/vnd.archie.scene+json')
def adapt_scene(event: dict, raw: bytes):
    value = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    with base.LOCK:
        base.SCENE.clear()
        base.SCENE.update(value)
        base.SCENE['revision'] = int(base.SCENE.get('revision') or 0) + 1
        base.BUS['event'] = 'field:scene'
        out = dict(base.SCENE)
    project_aperture(force=True)
    return {'ok': True, 'scene': out}


class FieldHandler(base.Handler):
    server_version = 'ArchieField/3'

    def _headers(self, code=200, ctype='application/json', length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', self._origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Field-Channel, X-Field-Shape, X-Field-Stream, X-Field-Revision, X-Field-Final, X-Field-Rate, X-Field-Preview')
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
                'meta': {},
            }
            if self.headers.get('X-Field-Rate'):
                event['meta']['rate'] = self.headers.get('X-Field-Rate')
            if self.headers.get('X-Field-Preview'):
                event['meta']['preview'] = str(self.headers.get('X-Field-Preview')).lower() in {'1', 'true', 'yes'}
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
