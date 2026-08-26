from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldEvent:
    serial: int
    channel: str
    shape: str
    stream: str
    revision: int
    final: bool
    payload: Any
    meta: dict[str, Any]
    at: float


class LiveField:
    """Modality-neutral, transport-neutral event field.

    The core deliberately does not know about text, audio, images, clicks,
    speech, models, or UI widgets. Producers publish an opaque payload with a
    channel + shape. Consumers can subscribe to the resulting ordered field.
    Adapters outside this class decide whether a shape has machine semantics.
    """

    def __init__(self, limit: int = 768):
        self._limit = max(64, int(limit))
        self._events: deque[FieldEvent] = deque(maxlen=self._limit)
        self._serial = 0
        self._latest: dict[str, FieldEvent] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

    @property
    def serial(self) -> int:
        with self._lock:
            return self._serial

    @staticmethod
    def _channel(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or 'field'))
        return (s.strip('._/-') or 'field')[:120]

    @staticmethod
    def _shape(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/+;=-' else '_' for c in str(value or 'opaque'))
        return (s.strip('._/-') or 'opaque')[:120]

    @staticmethod
    def _stream(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or ''))
        return s[:160]

    @staticmethod
    def _json_safe(value: Any, *, max_text: int = 128_000) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:max_text]
        if isinstance(value, bytes):
            return {
                'binary': True,
                'bytes': len(value),
                'sha256': hashlib.sha256(value).hexdigest(),
            }
        if isinstance(value, (list, tuple)):
            return [LiveField._json_safe(x, max_text=max_text) for x in value[:512]]
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= 512:
                    break
                out[str(k)[:160]] = LiveField._json_safe(v, max_text=max_text)
            return out
        return str(value)[:max_text]

    def publish(
        self,
        channel: str,
        *,
        shape: str = 'opaque',
        payload: Any = None,
        stream: str = '',
        revision: int = 0,
        final: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> FieldEvent:
        channel = self._channel(channel)
        shape = self._shape(shape)
        stream = self._stream(stream)
        try:
            revision = max(0, int(revision))
        except Exception:
            revision = 0
        safe_payload = self._json_safe(payload)
        safe_meta = self._json_safe(meta or {})
        if not isinstance(safe_meta, dict):
            safe_meta = {'value': safe_meta}
        with self._cond:
            self._serial += 1
            event = FieldEvent(
                serial=self._serial,
                channel=channel,
                shape=shape,
                stream=stream,
                revision=revision,
                final=bool(final),
                payload=safe_payload,
                meta=safe_meta,
                at=time.time(),
            )
            self._events.append(event)
            self._latest[channel] = event
            self._cond.notify_all()
            return event

    def snapshot(self, *, after: int = 0, limit: int = 96, latest: bool = False) -> dict[str, Any]:
        try:
            after = max(0, int(after))
        except Exception:
            after = 0
        limit = max(1, min(512, int(limit)))
        with self._lock:
            if latest:
                events = sorted(self._latest.values(), key=lambda x: x.serial)
                if after:
                    events = [e for e in events if e.serial > after]
            else:
                events = [e for e in self._events if e.serial > after]
            events = events[-limit:]
            floor = self._events[0].serial if self._events else self._serial
            return {
                'serial': self._serial,
                'floor': floor,
                'events': [asdict(e) for e in events],
            }

    def wait_after(self, serial: int, timeout: float = 0.8) -> int:
        try:
            serial = max(0, int(serial))
        except Exception:
            serial = 0
        with self._cond:
            if self._serial <= serial:
                self._cond.wait(timeout=max(0.0, min(30.0, float(timeout))))
            return self._serial

    def to_json(self, *, after: int = 0, limit: int = 96, latest: bool = False) -> bytes:
        return json.dumps(
            self.snapshot(after=after, limit=limit, latest=latest),
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
