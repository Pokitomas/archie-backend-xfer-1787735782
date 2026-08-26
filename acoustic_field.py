from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal


GestureKind = Literal['prime', 'phrase', 'cut', 'release']


@dataclass(frozen=True, slots=True)
class AcousticGesture:
    """
    A render-neutral controller instruction.

    It deliberately carries no voice id, phoneme ids, samples, codec frames, or
    model-specific token ids. A renderer may be replaced without changing the
    response/turn semantics encoded here.
    """

    seq: int
    generation: int
    revision: int
    kind: GestureKind
    start: int = 0
    end: int = 0
    pace: float = 1.0
    pressure: float = 0.0
    contour: float = 0.0
    continuity: float = 0.0
    reason: str = ''


class AcousticField:
    """
    Text-first response acoustics for the controller.

    This is intentionally *not* TTS. Text is canonical and can be displayed as
    soon as it exists. AcousticField only turns changing text plus turn-taking
    state into a tiny stream of acoustic gestures:

      prime   - silently prepare a text span; never audible by itself
      phrase  - a stable span may be rendered if a renderer exists
      cut     - invalidate audible work immediately
      release - the current response has no more audible work

    `generation` is the cancellation membrane. Any future renderer must discard
    queued work from older generations. User onset, response supersession, and
    model rewrites advance generation before new audible work is admitted.
    """

    def __init__(
        self,
        *,
        min_chars: int = 4,
        max_phrase_chars: int = 96,
        resume_guard_ms: float = 64.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_phrase_chars = max(self.min_chars + 1, int(max_phrase_chars))
        self.resume_guard_ns = max(0, int(float(resume_guard_ms) * 1_000_000))
        self._clock_ns = clock_ns
        self._revision = -1
        self._text = ''
        self._emitted = 0
        self._primed = 0
        self._user_active = False
        self._speaking = False
        self._done = False
        self._seq = 0
        self._generation = 0
        self._resume_after_ns = 0

    @property
    def emitted_chars(self) -> int:
        return self._emitted

    @property
    def primed_chars(self) -> int:
        return self._primed

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def user_active(self) -> bool:
        return self._user_active

    @property
    def revision(self) -> int:
        return self._revision

    def snapshot(self) -> dict:
        return {
            'generation': self._generation,
            'revision': self._revision,
            'chars': len(self._text),
            'primed_chars': self._primed,
            'emitted_chars': self._emitted,
            'user_active': self._user_active,
            'speaking': self._speaking,
            'done': self._done,
        }

    def _gesture(self, kind: GestureKind, **kw) -> AcousticGesture:
        self._seq += 1
        return AcousticGesture(
            seq=self._seq,
            generation=self._generation,
            revision=self._revision,
            kind=kind,
            **kw,
        )

    def _invalidate(self) -> None:
        self._generation += 1
        self._speaking = False

    def supersede(self, *, now_ns: int | None = None) -> list[AcousticGesture]:
        """Cancel the old response and clear its text without resetting generation."""
        now = self._clock_ns() if now_ns is None else int(now_ns)
        was_speaking = self._speaking
        old_end = self._emitted
        self._invalidate()
        out = [self._gesture('cut', start=old_end, end=old_end, reason='response-superseded')] if was_speaking else []
        self._revision = -1
        self._text = ''
        self._emitted = 0
        self._primed = 0
        self._done = False
        self._resume_after_ns = 2**63 - 1 if self._user_active else now
        return out

    def set_user_active(self, active: bool, *, now_ns: int | None = None) -> list[AcousticGesture]:
        """Apply turn-taking state. User onset is an unconditional hard cut."""
        active = bool(active)
        now = self._clock_ns() if now_ns is None else int(now_ns)
        if active == self._user_active:
            return []

        self._user_active = active
        if active:
            was_speaking = self._speaking
            self._invalidate()
            self._resume_after_ns = 2**63 - 1
            out: list[AcousticGesture] = []
            if was_speaking:
                out.append(self._gesture('cut', start=self._emitted, end=self._emitted, reason='user-onset'))
            out.extend(self._prime_new(reason='user-onset-prepare'))
            return out

        # A tiny quiet guard prevents ping-pong on breath/noise edges. It never
        # delays text; only optional acoustic emission is held.
        self._resume_after_ns = now + self.resume_guard_ns
        return []

    def observe(
        self,
        text: str,
        revision: int,
        *,
        done: bool = False,
        now_ns: int | None = None,
    ) -> list[AcousticGesture]:
        """Observe the latest canonical text snapshot and return new gestures."""
        revision = int(revision)
        text = str(text or '')
        now = self._clock_ns() if now_ns is None else int(now_ns)
        if revision < self._revision:
            return []

        out: list[AcousticGesture] = []
        common = self._common_prefix(self._text, text)
        rewrite_touches_committed = common < self._emitted
        rewrite_touches_prepared = common < self._primed
        is_rewrite = bool(self._text) and common < len(self._text)

        if is_rewrite and (rewrite_touches_committed or rewrite_touches_prepared or self._speaking):
            was_speaking = self._speaking
            self._invalidate()
            if was_speaking:
                out.append(self._gesture('cut', start=common, end=common, reason='model-rewrite'))
            self._emitted = min(self._emitted, common)
            self._primed = min(self._primed, common)

        self._revision = revision
        self._text = text
        self._done = bool(done)
        out.extend(self._prime_new(reason='text-delta'))
        out.extend(self._admit(now))
        return out

    def advance(self, *, now_ns: int | None = None) -> list[AcousticGesture]:
        """Advance pending optional audio after the user-silence guard expires."""
        now = self._clock_ns() if now_ns is None else int(now_ns)
        return self._admit(now)

    def _prime_new(self, *, reason: str) -> list[AcousticGesture]:
        if len(self._text) <= self._primed:
            return []
        start = self._primed
        self._primed = len(self._text)
        return [self._gesture('prime', start=start, end=self._primed, reason=reason)]

    def _admit(self, now_ns: int) -> list[AcousticGesture]:
        if self._user_active or now_ns < self._resume_after_ns:
            return []

        out: list[AcousticGesture] = []
        while True:
            end = self._next_boundary(done=self._done)
            if end <= self._emitted:
                break
            span = self._text[self._emitted:end]
            pace, pressure, contour, continuity = self._shape(span)
            out.append(self._gesture(
                'phrase',
                start=self._emitted,
                end=end,
                pace=pace,
                pressure=pressure,
                contour=contour,
                continuity=continuity,
                reason='stable-boundary' if not self._done else 'response-tail',
            ))
            self._emitted = end
            self._speaking = True

        if self._done and self._emitted >= len(self._text) and self._speaking:
            out.append(self._gesture('release', start=self._emitted, end=self._emitted, reason='response-done'))
            self._speaking = False
        return out

    def _next_boundary(self, *, done: bool) -> int:
        start = self._emitted
        remaining = len(self._text) - start
        if remaining <= 0:
            return start
        if done:
            return len(self._text)
        if remaining < self.min_chars:
            return start

        end_limit = min(len(self._text), start + self.max_phrase_chars)
        medium = -1
        last_space = -1
        for i in range(start + self.min_chars - 1, end_limit):
            c = self._text[i]
            if c in '.!?\n':
                return self._include_following_space(i + 1)
            if c in ';:,' or c in '—–':
                medium = self._include_following_space(i + 1)
                if i - start >= 12:
                    return medium
            if c.isspace():
                last_space = i + 1

        # Do not manufacture tiny robotic chunks just because a token stream is
        # growing. Only force a word boundary when a phrase becomes genuinely long.
        if remaining >= self.max_phrase_chars and last_space > start:
            return last_space
        if medium > start:
            return medium
        return start

    def _include_following_space(self, end: int) -> int:
        while end < len(self._text) and self._text[end].isspace():
            end += 1
        return end

    @staticmethod
    def _common_prefix(a: str, b: str) -> int:
        i = 0
        limit = min(len(a), len(b))
        while i < limit and a[i] == b[i]:
            i += 1
        return i

    @staticmethod
    def _shape(span: str) -> tuple[float, float, float, float]:
        """Cheap renderer-neutral dynamics; no voice or pronunciation model."""
        s = span.strip()
        if not s:
            return 1.0, 0.0, 0.0, 1.0
        terminal = s[-1]
        density = min(1.0, len(s) / 96.0)
        pace = 1.045 - density * 0.085
        if terminal == '!':
            pressure, contour, continuity = 0.13, -0.02, 0.15
        elif terminal == '?':
            pressure, contour, continuity = 0.04, 0.16, 0.18
        elif terminal in '.\n':
            pressure, contour, continuity = 0.035, -0.085, 0.10
        elif terminal in ',;:—–':
            pressure, contour, continuity = 0.0, 0.015, 0.82
        else:
            pressure, contour, continuity = 0.0, 0.0, 0.92
        return round(pace, 3), pressure, contour, continuity
