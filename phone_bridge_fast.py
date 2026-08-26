from __future__ import annotations

import json
import threading
import time
from collections import deque
from urllib.parse import urlparse

import phone_bridge as base
from acoustic_field import AcousticField


FAST_LOCK = threading.RLock()
FAST_COND = threading.Condition(FAST_LOCK)
FIELD = AcousticField(min_chars=4, max_phrase_chars=96, resume_guard_ms=64)
GESTURES = deque(maxlen=256)
VOICE_ACTIVE = False
LATEST = {
    'serial': 0,
    'reply': {},
    'seat': {},
    'acoustic': FIELD.snapshot(),
    'at': 0.0,
}
_LAST_RESPONSE_ID = None


def _text_active() -> bool:
    with base.LOCK:
        return bool(base.BUS.get('text_active'))


def _response_id(reply: dict):
    if not reply:
        return None
    return (reply.get('input_id'), reply.get('seq'), reply.get('stream_id'))


def _revision(reply: dict) -> int:
    for key in ('revision', 'seq'):
        try:
            if reply.get(key) is not None:
                return max(0, int(reply[key]))
        except Exception:
            pass
    return 0


def _append_gestures_locked(items):
    changed = False
    for item in items or ():
        GESTURES.append({
            'seq': item.seq,
            'generation': item.generation,
            'revision': item.revision,
            'kind': item.kind,
            'start': item.start,
            'end': item.end,
            'pace': item.pace,
            'pressure': item.pressure,
            'contour': item.contour,
            'continuity': item.continuity,
            'reason': item.reason,
        })
        changed = True
    return changed


def set_voice_active(active: bool):
    global VOICE_ACTIVE
    active = bool(active)
    text_active = _text_active()
    with FAST_COND:
        changed = active != VOICE_ACTIVE
        VOICE_ACTIVE = active
        if changed:
            gestures = FIELD.set_user_active(active or text_active)
            if _append_gestures_locked(gestures):
                LATEST['serial'] = int(LATEST.get('serial') or 0) + 1
            LATEST['acoustic'] = FIELD.snapshot()
            LATEST['at'] = time.time()
            FAST_COND.notify_all()
        return _snapshot_locked(text_active=text_active)


def _snapshot_locked(*, text_active: bool | None = None):
    if text_active is None:
        text_active = _text_active()
    return {
        'serial': int(LATEST.get('serial') or 0),
        'reply': dict(LATEST.get('reply') or {}),
        'seat': dict(LATEST.get('seat') or {}),
        'acoustic': dict(FIELD.snapshot()),
        'gestures': list(GESTURES)[-24:],
        'voice_active': bool(VOICE_ACTIVE),
        'text_active': bool(text_active),
        'at': float(LATEST.get('at') or 0.0),
    }


def snapshot():
    text_active = _text_active()
    with FAST_LOCK:
        return _snapshot_locked(text_active=text_active)


def observe_once(seat: dict | None = None):
    """One deterministic observer step, split out so the contract is unit-testable."""
    global _LAST_RESPONSE_ID
    seat = dict(seat or base.safe_seat())
    reply = dict(seat.get('reply') or {})
    response_id = _response_id(reply)
    text_active = _text_active()

    with FAST_COND:
        user_active = bool(VOICE_ACTIVE or text_active)
        gestures = []
        if user_active != FIELD.user_active:
            gestures.extend(FIELD.set_user_active(user_active))

        if response_id is not None and _LAST_RESPONSE_ID is not None and response_id != _LAST_RESPONSE_ID:
            gestures.extend(FIELD.supersede())
        if response_id is not None:
            _LAST_RESPONSE_ID = response_id

        text = str(reply.get('text') or '')
        if response_id is not None:
            gestures.extend(FIELD.observe(text, _revision(reply), done=bool(reply.get('done'))))
        if not user_active:
            gestures.extend(FIELD.advance())

        old_reply = LATEST.get('reply') or {}
        changed = (
            old_reply.get('text') != reply.get('text')
            or old_reply.get('done') != reply.get('done')
            or old_reply.get('revision') != reply.get('revision')
            or old_reply.get('seq') != reply.get('seq')
            or bool(gestures)
        )
        _append_gestures_locked(gestures)
        LATEST['reply'] = reply
        LATEST['seat'] = {
            'active_occupant': seat.get('active_occupant'),
            'input_seq': seat.get('input_seq'),
            'output_seq': seat.get('output_seq'),
            'time': seat.get('time') or {},
        }
        LATEST['acoustic'] = FIELD.snapshot()
        LATEST['at'] = time.time()
        if changed:
            LATEST['serial'] = int(LATEST.get('serial') or 0) + 1
            FAST_COND.notify_all()
        return _snapshot_locked(text_active=text_active)


def observer_worker():
    """Move high-rate observation to localhost so the phone receives one push stream."""
    while not base.STOP.is_set():
        try:
            snap = observe_once()
            active = not bool((snap.get('reply') or {}).get('done', True))
            wait = 0.012 if active else 0.055
        except Exception:
            wait = 0.12
        base.STOP.wait(wait)


class FastHandler(base.Handler):
    server_version = 'ArchiePhoneFast/1'

    def _ndjson(self, value: dict):
        self.wfile.write(base._json_bytes(value) + b'\n')
        self.wfile.flush()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/reply.ndjson':
            if not self._token():
                self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
                return
            self._headers(200, 'application/x-ndjson; charset=utf-8', None)
            self.send_header('Connection', 'close')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            serial = -1
            last_heartbeat = 0.0
            try:
                while not base.STOP.is_set():
                    with FAST_COND:
                        current = int(LATEST.get('serial') or 0)
                        now = time.time()
                        if current == serial and now - last_heartbeat < 8.0:
                            FAST_COND.wait(timeout=0.8)
                            continue
                    snap = snapshot()
                    current = int(snap.get('serial') or 0)
                    now = time.time()
                    if current != serial:
                        self._ndjson({'type': 'reply', **snap})
                        serial = current
                    elif now - last_heartbeat >= 8.0:
                        self._ndjson({'type': 'heartbeat', 'serial': serial, 'at': now})
                    last_heartbeat = now
            except Exception:
                pass
            return
        if path == '/api/acoustic':
            if not self._token():
                self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
                return
            self.sendb(200, base._json_bytes({'ok': True, **snapshot()}))
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/voice-state':
            if not self._token():
                self.sendb(401, b'{"ok":false,"error":"unauthorized"}')
                return
            n = min(32_000, int(self.headers.get('Content-Length') or 0))
            raw = self.rfile.read(n) if n else b''
            try:
                value = json.loads(raw.decode('utf-8', 'replace') or '{}')
            except Exception:
                value = {}
            snap = set_voice_active(bool(value.get('active')))
            self.sendb(200, base._json_bytes({'ok': True, **snap}))
            return
        return super().do_POST()


base.Handler = FastHandler


def main():
    threading.Thread(target=observer_worker, name='archie-phone-reply', daemon=True).start()
    base.main()


if __name__ == '__main__':
    main()
