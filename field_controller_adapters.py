from __future__ import annotations

import base64
import threading
import time
from typing import Callable

from field_transport import FieldTransport


_CALL_LOCK = threading.RLock()
_CALLS: dict[str, dict] = {}


def reset_state() -> None:
    with _CALL_LOCK:
        _CALLS.clear()


def install(register: Callable, *, base, arm_stream: Callable[[str], bool], project_aperture: Callable[..., None]) -> None:
    """Install optional machine interpretations onto a modality-blind field.

    Nothing in the field transport requires these adapters. Unknown shapes stay
    valid field events. This module is the replaceable boundary where a current
    controller happens to understand a few concrete shapes.
    """
    def ok(value) -> bool:
        return isinstance(value, dict) and bool(value.get('ok', True)) and not value.get('error')

    @register('utf8')
    def adapt_symbol(event: dict, raw: bytes):
        stream = FieldTransport.stream(event.get('stream'))
        if not arm_stream(stream):
            return {'ok': False, 'error': 'entry_pressure'}
        payload = event.get('payload')
        value = payload.get('value', '') if isinstance(payload, dict) else payload
        text = str(value or '')
        if not bool(event.get('final')):
            return {'ok': True, 'accepted': True, 'committed': False}
        if not text or len(text) > 4000:
            return {'ok': False, 'error': 'symbol_size'}
        accepted = base.jpost('/phone/text', {
            'text': text,
            'client_sent_ms': (event.get('meta') or {}).get('client_sent_ms') if isinstance(event.get('meta'), dict) else None,
            'bridge_received_ms': int(time.time() * 1000),
        }, 2.5)
        return {'ok': ok(accepted), 'accepted': accepted, 'committed': True}

    @register('application/vnd.archie.contact+json')
    def adapt_contact(event: dict, raw: bytes):
        stream = FieldTransport.stream(event.get('stream')) or 'contact'
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        active = bool(payload.get('active'))
        if active:
            if not arm_stream(stream):
                return {'ok': False, 'error': 'entry_pressure'}
            opened = base.jpost('/phone/audio/begin', {'client_started_ms': payload.get('client_started_ms')}, 2.0)
            if not ok(opened):
                return {'ok': False, 'error': 'contact_begin', 'controller': opened}
            with _CALL_LOCK:
                _CALLS[stream] = {'call_id': str(opened.get('call_id') or ''), 'opened': time.monotonic()}
            return {'ok': True, 'active': True, 'call_id': opened.get('call_id'), 'ack': opened.get('ack')}
        with _CALL_LOCK:
            current = dict(_CALLS.get(stream) or {})
        return {'ok': True, 'active': False, 'call_id': current.get('call_id')}

    @register('audio/pcm;codec=s16le')
    @register('pcm_s16le')
    def adapt_samples(event: dict, raw: bytes):
        stream = FieldTransport.stream(event.get('stream')) or 'contact'
        if not arm_stream(stream):
            return {'ok': False, 'error': 'entry_pressure'}
        if not raw:
            payload = event.get('payload')
            if isinstance(payload, dict) and payload.get('base64'):
                try:
                    raw = base64.b64decode(str(payload['base64']), validate=False)
                except Exception:
                    raw = b''
        if len(raw) < 6400 or len(raw) > 640000 or len(raw) % 2:
            return {'ok': False, 'error': 'sample_window', 'bytes': len(raw)}
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
        return {'ok': ok(accepted), 'accepted': accepted, 'preview': preview, 'call_id': call_id}

    @register('application/vnd.archie.action+json')
    def adapt_action(event: dict, raw: bytes):
        if not arm_stream(FieldTransport.stream(event.get('stream'))):
            return {'ok': False, 'error': 'entry_pressure'}
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        if not payload.get('action'):
            return {'ok': False, 'error': 'action'}
        result = base.controller_action(payload)
        return {'ok': ok(result), 'result': result}

    @register('application/vnd.archie.scene+json')
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
