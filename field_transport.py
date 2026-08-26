from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict
from typing import Any

import field_protocol as protocol
from field_protocol import FieldRecord

# Compatibility name. Transport and canonical field now share one structural
# vocabulary; only their ownership/replay semantics differ.
FieldPacket = FieldRecord


class FieldTransport:
    """Transient replay window; never a second source of truth.

    ARCHIE_CONTROLLER.LiveField remains canonical. This object only orders
    opaque packets crossing a remote aperture and keeps a short reconnect
    window. It has no per-channel latest state, no model semantics, and no
    modality semantics. A reconnect is re-seeded from the controller itself.
    """

    channel = staticmethod(protocol.channel)
    shape = staticmethod(protocol.shape)
    stream = staticmethod(protocol.stream)
    safe = staticmethod(protocol.safe)

    def __init__(self, limit: int = 768):
        self._events: deque[FieldRecord] = deque(maxlen=max(64, int(limit)))
        self._serial = 0
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

    @property
    def serial(self) -> int:
        with self._lock:
            return self._serial

    def append(
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
            packet = protocol.record(
                self._serial,
                channel,
                shape_value=shape,
                payload=payload,
                stream_value=stream,
                revision_value=revision,
                final=final,
                meta=meta,
            )
            self._events.append(packet)
            self._cond.notify_all()
            return packet

    def replay(self, *, after: int = 0, limit: int = 192) -> dict[str, Any]:
        try:
            after = max(0, int(after))
        except Exception:
            after = 0
        limit = max(1, min(512, int(limit)))
        with self._lock:
            events = [e for e in self._events if e.serial > after][-limit:]
            floor = self._events[0].serial if self._events else self._serial
            return {'serial': self._serial, 'floor': floor, 'events': [asdict(e) for e in events]}

    def wait_after(self, serial: int, timeout: float = .8) -> int:
        try:
            serial = max(0, int(serial))
        except Exception:
            serial = 0
        with self._cond:
            if self._serial <= serial:
                self._cond.wait(timeout=max(0.0, min(30.0, float(timeout))))
            return self._serial
