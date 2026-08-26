from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldRecord:
    serial: int
    channel: str
    shape: str
    stream: str
    revision: int
    final: bool
    payload: Any
    meta: dict[str, Any]
    at: float


def channel(value: Any) -> str:
    s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or 'field'))
    return (s.strip('._/-') or 'field')[:120]


def shape(value: Any) -> str:
    s = ''.join(c if c.isalnum() or c in '._/+;=-' else '_' for c in str(value or 'opaque'))
    return (s.strip('._/-') or 'opaque')[:120]


def stream(value: Any) -> str:
    s = ''.join(c if c.isalnum() or c in '._/-' else '_' for c in str(value or ''))
    return s[:160]


def revision(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def safe(value: Any, *, max_text: int = 128_000) -> Any:
    """One wire-safety rule for canonical state and transient apertures.

    Binary payloads are represented by identity/extent only unless a transport
    deliberately carries their raw bytes out of band. This function has no
    modality semantics and intentionally performs no interpretation.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_text]
    if isinstance(value, bytes):
        return {'binary': True, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
    if isinstance(value, (list, tuple)):
        return [safe(x, max_text=max_text) for x in value[:512]]
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 512:
                break
            out[str(k)[:160]] = safe(v, max_text=max_text)
        return out
    return str(value)[:max_text]


def record(
    serial: int,
    channel_value: Any,
    *,
    shape_value: Any = 'opaque',
    payload: Any = None,
    stream_value: Any = '',
    revision_value: Any = 0,
    final: bool = False,
    meta: dict[str, Any] | None = None,
    at: float | None = None,
) -> FieldRecord:
    safe_meta = safe(meta or {})
    if not isinstance(safe_meta, dict):
        safe_meta = {'value': safe_meta}
    return FieldRecord(
        serial=max(0, int(serial)),
        channel=channel(channel_value),
        shape=shape(shape_value),
        stream=stream(stream_value),
        revision=revision(revision_value),
        final=bool(final),
        payload=safe(payload),
        meta=safe_meta,
        at=time.time() if at is None else float(at),
    )
