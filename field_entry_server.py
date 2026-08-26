from __future__ import annotations

import json
from urllib.parse import urlparse

import field_mcp
import phone_bridge as base
import phone_bridge_field as field


class EntryFieldHandler(field.FieldHandler):
    server_version = 'ArchieEntryField/1'

    def _controller_get_public(self, path: str, timeout: float = 1.8):
        value = base.jget(path, timeout)
        if not isinstance(value, dict):
            return {'ok': False, 'error': 'controller'}
        if path == '/controller':
            value = field._redact_temporal_secret(value)
            value['urgency'] = field._urgent()
        return value

    @staticmethod
    def _field_snapshot(after: int = 0):
        try:
            field.sample_controller(force=(int(after or 0) == 0))
            if int(after or 0) == 0:
                field.project_aperture(force=True)
        except Exception:
            pass
        return field.WIRE.replay(after=max(0, int(after or 0)), limit=192)

    def do_GET(self):
        if urlparse(self.path).path == '/mcp':
            if not self._token():
                self.sendb(401, b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized"}}')
                return
            self.sendb(405, b'', 'application/json', extra={'Allow': 'POST'})
            return
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != '/mcp':
            return super().do_POST()
        if not self._token():
            self.sendb(401, b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized"}}')
            return
        n = min(2_000_000, int(self.headers.get('Content-Length') or 0))
        raw = self.rfile.read(n) if n else b''
        try:
            message = json.loads(raw.decode('utf-8', 'replace') or '{}')
        except Exception:
            message = {}
        if not isinstance(message, dict):
            message = {}
        method = str(message.get('method') or '')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        if method == 'tools/call' and str(params.get('name') or '') == 'enter_controller':
            # Entry pressure is infrastructure, not model context. A failed arm
            # prevents a false attachment rather than leaking timing detail.
            if not field._arm_pressure():
                result = field_mcp._jsonrpc(
                    message.get('id'),
                    result=field_mcp._tool_result(
                        {'schema': 'archie-entry-capsule/v1', 'error': 'entry_pressure'},
                        error=True,
                    ),
                )
                self.sendb(200, field_mcp.dumps(result), 'application/json')
                return
        response = field_mcp.handle(
            message,
            controller_get=self._controller_get_public,
            controller_action=base.controller_action,
            field_snapshot=self._field_snapshot,
            urgency=field._urgent,
        )
        if response is None:
            self.sendb(202, b'', 'application/json')
            return
        self.sendb(200, field_mcp.dumps(response), 'application/json')


# field.main resolves this global when it constructs the HTTP server, so this
# preserves the already-tested field runtime and changes only the aperture.
field.FieldHandler = EntryFieldHandler
base.Handler = EntryFieldHandler


def main():
    field.main()


if __name__ == '__main__':
    main()
