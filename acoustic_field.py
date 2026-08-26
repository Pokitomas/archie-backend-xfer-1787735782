from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GestureKind = Literal['prime', 'phrase', 'cut', 'release']


@dataclass(frozen=True, slots=True)
class AcousticGesture:
    """A render-neutral instruction, not speech audio and not a TTS request."""

    seq: int
    kind: GestureKind
    start: int = 0
    end: int = 0
    pace: float = 1.0
    pressure: float = 0.0
    contour: float = 0.0
    reason: str = ''


class AcousticField:
    """
    Controller-side response-acoustics primitive.

    Text is canonical and may render immediately.  This object only derives a
    low-dimensional acoustic gesture stream that a future native resonator may
    choose to render.  It intentionally has no waveform, phoneme, voice-model,
    TTS, audio-device, or network dependency.

    Invariants:
      * user activity always wins: no phrase gesture is emitted over the user;
      * user onset cuts an already-emitting phrase immediately;
      * stale model revisions cannot resurrect old speech;
      * voice identity is not encoded here; renderers own timbre completely;
      * partial text can be prepared before the model finishes.
    """

    def __init__(self, *, min_chars: int = 5, soft_chars: int = 26) -> None:
        self.min_chars = max(1, int(min_chars))
        self.soft_chars = max(self.min_chars, int(soft_chars))
        self._revision = -1
        self._text = ''
        self._emitted = 0
        self._user_active = False
        self._speaking = False
        self._seq = 0
        self._done = False

    @property
    def emitted_chars(self) -> int:
        return self._emitted

    @property
    def user_active(self) -> bool:
        return self._user_active

    def _gesture(self, kind: GestureKind, **kw) -> AcousticGesture:
        self._seq += 1
        return AcousticGesture(seq=self._seq, kind=kind, **kw)

    def set_user_active(self, active: bool) -> list[AcousticGesture]:
        active = bool(active)
        if active == self._user_active:
            return []
        self._user_active = active
        if active and self._speaking:
            self._speaking = False
            return [self._gesture('cut', start=self._emitted, end=self._emitted, reason='user-onset')]
        if active:
            return [self._gesture('prime', start=self._emitted, end=len(self._text), reason='prepare-only')]
        return []

    def observe(self, text: str, revision: int, *, done: bool = False) -> list[AcousticGesture]:
        revision = int(revision)
        text = str(text or '')
        if revision < self._revision:
            return []
        if revision > self._revision:
            # A revision may rewrite a suffix. Preserve only text that is still a
            # literal prefix of the new response; never continue from stale prose.
            common = 0
            limit = min(len(self._text), len(text), self._emitted)
            while common < limit and self._text[common] == text[common]:
                common += 1
            if common < self._emitted:
                self._emitted = common
                self._speaking = False
            self._revision = revision
        self._text = text
        self._done = bool(done)

        if self._user_active:
            return [self._gesture('prime', start=self._emitted, end=len(text), reason='user-active')] if len(text) > self._emitted else []

        out: list[AcousticGesture] = []
        while True:
            end = self._next_boundary(done=done)
            if end <= self._emitted:
                break
            pace, pressure, contour = self._shape(self._text[self._emitted:end])
            out.append(self._gesture('phrase', start=self._emitted, end=end, pace=pace, pressure=pressure, contour=contour, reason='stable-boundary'))
            self._emitted = end
            self._speaking = True
        if done and self._emitted >= len(text) and self._speaking:
            out.append(self._gesture('release', start=self._emitted, end=self._emitted, reason='response-done'))
            self._speaking = False
        return out

    def _next_boundary(self, *, done: bool) -> int:
        start = self._emitted
        remaining = len(self._text) - start
        if remaining < self.min_chars and not done:
            return start
        if done:
            return len(self._text)

        hard = -1
        soft = -1
        scan_end = min(len(self._text), start + max(self.soft_chars * 2, 64))
        for i in range(start + self.min_chars, scan_end):
            c = self._text[i]
            if c in '.!?;:\n':
                hard = i + 1
                break
            if c in ',—- ' and i - start >= self.soft_chars:
                soft = i + 1
                break
        if hard > start:
            return hard
        if soft > start:
            return soft
        return start

    @staticmethod
    def _shape(span: str) -> tuple[float, float, float]:
        s = span.strip()
        if not s:
            return 1.0, 0.0, 0.0
        q = s.endswith('?')
        terminal = s.endswith(('.', '!', '?'))
        density = min(1.0, max(0.0, len(s) / 80.0))
        pace = 1.06 - density * 0.12
        pressure = 0.12 if s.endswith('!') else 0.04 if terminal else 0.0
        contour = 0.18 if q else -0.08 if terminal else 0.02
        return round(pace, 3), pressure, contour
