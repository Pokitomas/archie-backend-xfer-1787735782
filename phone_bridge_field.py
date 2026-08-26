from __future__ import annotations

import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import phone_bridge as base
import phone_bridge_fast as fast
from live_field import LiveField


LIVE = LiveField(limit=1024)
_ADAPTERS = {}
_OBSERVER_STOP = threading.Event()


def register_adapter(shape: str):
    key = LiveField._shape(shape)
    def deco(fn):
        _ADAPTERS[key] = fn
        return fn
    return deco


def _publish(channel: str, *, shape='opaque', payload=None, stream='', revision=0, final=False, meta=None):
    return LIVE.publish(
        channel,
        shape=shape,
        payload=payload,
        stream=stream,
        revision=revision,
        final=final,
        meta=meta or {},
    )


def _field_observer():
    """Lift existing controller surfaces into one ordered field.

    The loop observes adapters, not modalities. New producers can publish into
    LIVE directly without changing this protocol or the phone transport.
    """
    last_reply_serial = -1
    last_activity_serial = -1
    last_scene_revision = -1
    last_pc = None
    while not base.STOP.is_set() and not _OBSERVER_STOP.is_set():
        changed = False
        try:
            snap = fast.snapshot(diagnostics=False)
            rs = int(snap.get('serial') or 0)
            if rs != last_reply_serial:
                last_reply_serial = rs
                reply = dict(snap.get('reply') or {})
                _publish(
                    'controller.primary',
                    shape='application/vnd.archie.controller-state+json',
                    payload={
                        'reply': reply,
                        'seat': dict(snap.get('seat') or {}),
                        'acoustic': dict(snap.get('acoustic') or {}),
                    },
                    stream=str(reply.get('stream_id') or ''),
                    revision=reply.get('revision') or reply.get('seq') or 0,
                    final=bool(reply.get('done')),
                    meta={'direction': 'egress', 'source': 'controller'},
                )
                changed = True
        except Exception:
            pass
        try:
            act = fast.activity_snapshot(limit=24)
            aser = int(act.get('serial') or 0)
            if aser != last_activity_serial:
                last_activity_serial = aser
                _publish(
                    'controller.activity',
                    shape='application/vnd.archie.activity+json',
                    payload={'items': act.get('items') or []},
                    revision=aser,
                    final=False,
                    meta={'direction': 'egress', 'source': 'controller'},
                )
                changed = True
            pc = act.get('pc') or {}
            pc_key = json.dumps(pc, sort_keys=True, separators=(',', ':'), default=str)
            if pc_key != last_pc:
                last_pc = pc_key
                _publish(
                    'machine.state',
                    shape='application/vnd.archie.machine-state+json',
                    payload=pc,
                    meta={'direction': 'egress', 'source': 'machine'},
                )
                changed = True
        except Exception:
            pass
        try:
            with base.LOCK:
                scene = dict(base.SCENE)
            rev = int(scene.get('revision') or 0)
            if rev != last_scene_revision:
                last_scene_revision = rev
                _publish(
                    'surface.scene',
                    shape='application/vnd.archie.scene+json',
                    payload=scene,
                    revision=rev,
                    meta={'direction': 'egress', 'source': 'surface'},
                )
                changed = True
        except Exception:
            pass
        base.STOP.wait(0.018 if changed else 0.048)


def _result_ok(result) -> bool:
    return isinstance(result, dict) and bool(result.get('ok', True)) and not result.get('error')


@register_adapter('utf8')
def _adapt_utf8(event: dict, raw: bytes):
    payload = event.get('payload')
    if isinstance(payload, dict):
        value = payload.get('value', '')
    else:
        value = payload if payload is not None else raw.decode('utf-8', 'replace')
    text = str(value or '')
    stream = str(event.get('stream') or '')[:120]
    revision = max(0, int(event.get('revision') or 0))
    final = bool(event.get('final'))
    if not stream:
        return {'ok': False, 'error': 'stream'}
    if not fast._arm_text_stream(stream):
        return {'ok': False, 'error': 'entry_pressure'}
    commit = False
    with base.LOCK:
        if stream != base.BUS.get('stream_id') or revision >= int(base.BUS.get('revision') or 0):
            base.BUS.update({
                'stream_id': stream,
                'revision': revision,
                'text': text,
                'text_active': not final,
                'updated': time.time(),
                'event': 'field:live',
            })
            commit = final and bool(text) and revision > int(base.BUS.get('committed_revision') or 0)
    result = {'ok': True, 'accepted': True, 'live': True, 'revision': revision}
    if commit:
        accepted = base.jpost('/phone/text', {'text': text}, 2.5)
        with base.LOCK:
            base.BUS['committed_revision'] = revision
            base.BUS['event'] = 'field:commit'
            base.BUS['text_active'] = False
        result['controller'] = accepted
        result['ok'] = _result_ok(accepted)
    return result


@register_adapter('audio/pcm;codec=s16le')
@register_adapter('pcm_s16le')
def _adapt_pcm(event: dict, raw: bytes):
    if not fast._urgency() and not fast._arm_entry_pressure():
        return {'ok': False, 'error': 'entry_pressure'}
    meta = event.get('meta') if isinstance(event.get('meta'), dict) else {}
    try:
        rate = max(8000, min(48000, int(meta.get('rate') or 16000)))
    except Exception:
        rate = 16000
    if not raw:
        payload = event.get('payload')
        if isinstance(payload, dict) and payload.get('base64'):
            try:
                raw = base64.b64decode(str(payload['base64']), validate=False)
            except Exception:
                raw = b''
    accepted = base.jpost('/phone/audio', {
        'sample_rate': rate,
        'pcm16_base64': base64.b64encode(raw).decode('ascii'),
    }, 2.0)
    with base.LOCK:
        base.BUS['audio_seq'] = int(base.BUS.get('audio_seq') or 0) + 1
        base.BUS['event'] = 'field:samples'
    return {'ok': _result_ok(accepted), 'accepted': accepted}


@register_adapter('application/vnd.archie.contact+json')
def _adapt_contact(event: dict, raw: bytes):
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    active = bool(payload.get('active'))
    if active and not fast._arm_entry_pressure():
        return {'ok': False, 'error': 'entry_pressure'}
    snap = fast.set_voice_active(active)
    return {'ok': True, 'active': active, 'serial': snap.get('serial')}


@register_adapter('application/vnd.archie.action+json')
def _adapt_action(event: dict, raw: bytes):
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    if not payload.get('action'):
        return {'ok': False, 'error': 'action'}
    result = base.controller_action(payload)
    fast._record_action(payload, result)
    return {'ok': _result_ok(result), 'result': result}


@register_adapter('application/vnd.archie.scene+json')
def _adapt_scene(event: dict, raw: bytes):
    value = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    with base.LOCK:
        base.SCENE.clear()
        base.SCENE.update(value)
        base.SCENE['revision'] = int(base.SCENE.get('revision') or 0) + 1
        base.BUS['event'] = 'field:scene'
        out = dict(base.SCENE)
    return {'ok': True, 'scene': out}


class FieldHandler(fast.FastHandler):
    server_version = 'ArchieLiveField/1'

    def _field_stream(self):
        self._headers(200, 'application/x-ndjson; charset=utf-8', None)
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        cursor = 0
        last_heartbeat = 0.0
        try:
            initial = LIVE.snapshot(latest=True, limit=128)
            cursor = int(initial.get('serial') or 0)
            self._ndjson({'type': 'field', 'snapshot': True, **initial})
            last_heartbeat = time.time()
            while not base.STOP.is_set():
                current = LIVE.wait_after(cursor, timeout=.75)
                now = time.time()
                if current > cursor:
                    snap = LIVE.snapshot(after=cursor, limit=192)
                    self._ndjson({'type': 'field', 'snapshot': False, **snap})
                    cursor = int(snap.get('serial') or cursor)
                    last_heartbeat = now
                elif now - last_heartbeat >= 8.0:
                    self._ndjson({'type': 'heartbeat', 'serial': cursor, 'at': now})
                    last_heartbeat = now
        except Exception:
            return

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/field.ndjson':
            if not self._token():
                self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
                return
            self._field_stream()
            return
        if path == '/api/field':
            if not self._token():
                self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
                return
            q = parse_qs(urlparse(self.path).query)
            try:
                after = int((q.get('after') or ['0'])[0])
            except Exception:
                after = 0
            latest = str((q.get('latest') or ['0'])[0]).lower() in {'1', 'true', 'yes'}
            self.sendb(200, base._json_bytes({'ok': True, **LIVE.snapshot(after=after, latest=latest)}))
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/session':
            if self._token() and base.SELFTEST.get('ok'):
                _publish(
                    'surface.presence',
                    shape='application/vnd.archie.presence+json',
                    payload={'state': 'listening'},
                    final=False,
                    meta={'direction': 'egress', 'source': 'bridge'},
                )
            return super().do_POST()
        if path != '/api/field':
            return super().do_POST()
        if not self._token():
            self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
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
            binary = raw
        channel = str(event.get('channel') or 'user.primary')
        shape = LiveField._shape(event.get('shape') or 'opaque')
        stream = str(event.get('stream') or '')
        revision = event.get('revision') or 0
        final = bool(event.get('final'))
        meta = event.get('meta') if isinstance(event.get('meta'), dict) else {}
        ingress = _publish(
            channel,
            shape=shape,
            payload=binary if binary else event.get('payload'),
            stream=stream,
            revision=revision,
            final=final,
            meta={**meta, 'direction': 'ingress'},
        )
        adapter = _ADAPTERS.get(shape)
        adapted = False
        result = {'ok': True, 'accepted': True, 'adapted': False}
        if adapter is not None:
            adapted = True
            try:
                result = adapter(event, binary)
                if not isinstance(result, dict):
                    result = {'ok': True, 'value': result}
            except Exception as exc:
                result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        _publish(
            channel + '.receipt',
            shape='application/vnd.archie.receipt+json',
            payload={
                'for_serial': ingress.serial,
                'adapted': adapted,
                'ok': bool(result.get('ok', True)) and not result.get('error'),
                'error': result.get('error'),
            },
            stream=stream,
            revision=revision,
            final=True,
            meta={'direction': 'egress', 'source': 'bridge'},
        )
        code = 202 if bool(result.get('ok', True)) and not result.get('error') else 503
        self.sendb(code, base._json_bytes({
            'ok': code < 400,
            'field_serial': ingress.serial,
            'adapted': adapted,
            'result': result,
        }))


base.Handler = FieldHandler


def main():
    threading.Thread(target=_field_observer, name='archie-live-field', daemon=True).start()
    fast.main()


if __name__ == '__main__':
    main()
