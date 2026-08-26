from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldPacket:
    serial: int
    channel: str
    shape: str
    stream: str
    revision: int
    final: bool
    payload: Any
    meta: dict[str, Any]
    at: float


class FieldTransport:
    """Transient replay window; never a second source of truth.

    ARCHIE_CONTROLLER.LiveField remains canonical. This object only orders
    opaque packets crossing a remote aperture and keeps a short reconnect
    window. It has no per-channel latest state, no model semantics, and no
    modality semantics. A reconnect is re-seeded from the controller itself.
    """

    def __init__(self, limit: int = 768):
        self._events: deque[FieldPacket] = deque(maxlen=max(64, int(limit)))
        self._serial = 0
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

    @property
    def serial(self) -> int:
        with self._lock:
            return self._serial

    @staticmethod
    def channel(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or 'field'))
        return (s.strip('._/-') or 'field')[:120]

    @staticmethod
    def shape(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/+;=-' else '_' for c in str(value or 'opaque'))
        return (s.strip('._/-') or 'opaque')[:120]

    @staticmethod
    def stream(value: Any) -> str:
        s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or ''))
        return s[:160]

    @classmethod
    def safe(cls, value: Any, *, max_text: int = 128_000) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:max_text]
        if isinstance(value, bytes):
            return {'binary': True, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
        if isinstance(value, (list, tuple)):
            return [cls.safe(x, max_text=max_text) for x in value[:512]]
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= 512:
                    break
                out[str(k)[:160]] = cls.safe(v, max_text=max_text)
            return out
        return str(value)[:max_text]

    def append(self, channel: str, *, shape: str = 'opaque', payload: Any = None,
               stream: str = '', revision: int = 0, final: bool = False,
               meta: dict[str, Any] | None = None) -> FieldPacket:
        try:
            revision = max(0, int(revision))
        except Exception:
            revision = 0
        safe_meta = self.safe(meta or {})
        if not isinstance(safe_meta, dict):
            safe_meta = {'value': safe_meta}
        with self._cond:
            self._serial += 1
            packet = FieldPacket(
                serial=self._serial,
                channel=self.channel(channel),
                shape=self.shape(shape),
                stream=self.stream(stream),
                revision=revision,
                final=bool(final),
                payload=self.safe(payload),
                meta=safe_meta,
                at=time.time(),
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
