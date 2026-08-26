from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, asdict


@dataclass(frozen=True, slots=True)
class Activity:
    seq: int
    kind: str
    label: str
    detail: str = ''
    status: str = ''
    source: str = 'controller'


class ActivityField:
    """Small render-neutral ledger for what the controller is doing.

    This is intentionally passive. It mirrors controller/PC activity so the
    field can show coding and actions without becoming another control surface.
    Streaming reply revisions are deliberately coalesced; the primary field
    owns token-by-token immediacy.
    """

    def __init__(self, limit: int = 160):
        self._seq = 0
        self._items = deque(maxlen=max(24, int(limit)))
        self._last = {'occupant': None, 'input_seq': None, 'reply_key': None, 'focus': None, 'bus_event': None}

    @property
    def serial(self) -> int:
        return self._seq

    def push(self, kind: str, label: str, detail: str = '', *, status: str = '', source: str = 'controller') -> Activity:
        kind = str(kind or 'event')[:28]
        label = self._clean(label, 180)
        detail = self._clean(detail, 420)
        status = self._clean(status, 40)
        source = self._clean(source, 40) or 'controller'
        if self._items:
            last = self._items[-1]
            if (last.kind, last.label, last.detail, last.status, last.source) == (kind, label, detail, status, source):
                return last
        self._seq += 1
        item = Activity(self._seq, kind, label, detail, status, source)
        self._items.append(item)
        return item

    def observe(self, seat: dict | None = None, *, focus: str = '', bus_event: str = '') -> list[Activity]:
        seat = seat or {}
        out: list[Activity] = []
        occupant = str(seat.get('active_occupant') or '')
        if occupant and occupant != self._last['occupant']:
            self._last['occupant'] = occupant
            out.append(self.push('occupant', occupant, source='seat'))

        latest = seat.get('latest_input') if isinstance(seat.get('latest_input'), dict) else {}
        input_seq = latest.get('seq', seat.get('input_seq'))
        if input_seq is not None and input_seq != self._last['input_seq']:
            self._last['input_seq'] = input_seq
            src = str(latest.get('source') or 'input')
            kind = str(latest.get('kind') or 'ingress')
            chars = latest.get('chars')
            detail = f'{src} · {chars}ch' if chars is not None else src
            out.append(self.push('input', f'{kind} #{input_seq}', detail, source='seat'))

        reply = seat.get('reply') if isinstance(seat.get('reply'), dict) else {}
        seq = reply.get('seq', seat.get('output_seq'))
        status = 'fault' if reply.get('fault') else 'aborted' if reply.get('aborted') else 'done' if reply.get('done') else 'stream'
        reply_key = (seq, status, str(reply.get('fault') or '')[:120])
        if seq is not None and reply_key != self._last['reply_key']:
            self._last['reply_key'] = reply_key
            detail = str(reply.get('fault') or '')[:180]
            out.append(self.push('output', f'reply #{seq}', detail, status=status, source='seat'))

        if focus and focus != self._last['focus']:
            self._last['focus'] = focus
            out.append(self.push('focus', focus, source='windows'))

        if bus_event and bus_event != self._last['bus_event']:
            self._last['bus_event'] = bus_event
            out.append(self.push('bridge', bus_event, source='bridge'))
        return out

    def record_action(self, payload: dict, result: dict | None = None) -> Activity:
        action = str((payload or {}).get('action') or 'action')[:80]
        intent = self._clean((payload or {}).get('intent') or '', 220)
        detail = intent
        if action == 'workspace_patch':
            patch = str((payload or {}).get('patch') or (payload or {}).get('text') or '')
            paths = []
            for m in re.finditer(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', patch, re.M):
                p = self._clean(m.group(1), 110)
                if p and p not in paths:
                    paths.append(p)
            if paths:
                detail = ' · '.join(paths[:5])
        status = ''
        if isinstance(result, dict):
            status = 'ok' if bool(result.get('ok', True)) and not result.get('error') else 'refused'
            if not detail and result.get('error'):
                detail = self._clean(result.get('error'), 220)
        return self.push('code' if action == 'workspace_patch' else 'action', action, detail, status=status, source='controller')

    def snapshot(self, limit: int = 18) -> dict:
        n = max(1, min(80, int(limit)))
        newest_first = list(reversed(list(self._items)[-n:]))
        return {'serial': self._seq, 'items': [asdict(x) for x in newest_first]}

    @staticmethod
    def _clean(value, limit: int) -> str:
        text = ' '.join(str(value or '').replace('\x00', '').split())
        return text[: max(1, int(limit))]
