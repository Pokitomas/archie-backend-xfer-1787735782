from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def protect(data: bytes, *, entropy: bytes = b'ARCHIE/native-field/v1') -> bytes:
    if os.name != 'nt':
        raise RuntimeError('DPAPI is Windows-only')
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    incoming, keep1 = _blob(bytes(data))
    extra, keep2 = _blob(bytes(entropy))
    outgoing = DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN; CurrentUser scope is the default.
    ok = crypt32.CryptProtectData(ctypes.byref(incoming), None, ctypes.byref(extra), None, None, 0x1, ctypes.byref(outgoing))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)
        _ = (keep1, keep2)


def unprotect(data: bytes, *, entropy: bytes = b'ARCHIE/native-field/v1') -> bytes:
    if os.name != 'nt':
        raise RuntimeError('DPAPI is Windows-only')
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    incoming, keep1 = _blob(bytes(data))
    extra, keep2 = _blob(bytes(entropy))
    outgoing = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(incoming), None, ctypes.byref(extra), None, None, 0x1, ctypes.byref(outgoing))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)
        _ = (keep1, keep2)


def save(path: str | Path, secret: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sealed = protect(str(secret).encode('utf-8'))
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_bytes(sealed)
    os.replace(tmp, p)


def load(path: str | Path) -> str:
    return unprotect(Path(path).read_bytes()).decode('utf-8')
