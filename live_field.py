from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict
from typing import Any

from field_protocol import FieldRecord, record

# Compatibility name for callers/tests; there is now one record type across
# canonical and transport field layers.
FieldEvent = FieldRecord


class LiveField:
    """Modality-neutral, transport-neutral event field.

    The core deliberately does not know about text, audio, images, clicks,
    speech, models, or UI widgets. Producers publish an opaque payload with a
    channel + shape. Consumers can subscribe to the resulting ordered field.
    Adapters outside this class decide whether a shape has machine semantics.
    """

    def __init__(self, limit: int = 768):
        self._limit = max(64, int(limit))
        self._events: deque[FieldRecord] = deque(maxlen=self._limit)
        self._serial = 0
        self._latest: dict[str, FieldRecord] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

    @property
    def serial(self) -> int:
        with self._lock:
            return self._serial

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
    ) -> FieldRecord:
        with self._cond:
            self._serial += 1
            event = record(
                self._serial,
                channel,
                shape_value=shape,
                payload=payload,
                stream_value=stream,
                revision_value=revision,
                final=final,
                meta=meta,
            )
            self._events.append(event)
            self._latest[event.channel] = event
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
