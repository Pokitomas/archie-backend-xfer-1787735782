from __future__ import annotations

import json
from typing import Any, Callable

PROTOCOL = '2025-06-18'


def _jsonrpc(req_id, *, result=None, error=None):
    out = {'jsonrpc': '2.0'}
    if req_id is not None:
        out['id'] = req_id
    if error is not None:
        out['error'] = error
    elif req_id is not None:
        out['result'] = result if result is not None else {}
    return out


def _tool_result(value: dict[str, Any], *, error: bool = False):
    return {
        'content': [{'type': 'text', 'text': 'attached' if not error else 'controller unavailable'}],
        'structuredContent': value,
        'isError': bool(error),
    }


def tool_list():
    return [
        {
            'name': 'enter_controller',
            'title': 'Enter live controller',
            'description': (
                'Use this immediately whenever the user asks to enter, continue, use, inspect, '
                'control, develop through, or otherwise inhabit their PC/controller/live field, '
                'including shorthand or an opaque cue. No prior chat context or setup explanation '
                'is required. Call this before asking the user to restate anything.'
            ),
            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False, 'idempotentHint': True},
        },
        {
            'name': 'controller_action',
            'title': 'Act through controller membrane',
            'description': (
                'Use this after enter_controller for a concrete PC or workspace mutation. '
                'Pass one controller action object unchanged; the local controller remains the '
                'authority for occupancy, safety, evidence, receipts, and refusal.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {'action': {'type': 'object', 'additionalProperties': True}},
                'required': ['action'],
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'openWorldHint': False, 'idempotentHint': False},
        },
        {
            'name': 'field_snapshot',
            'title': 'Read live field',
            'description': 'Use this to refresh the current controller-owned live field without relying on chat history.',
            'inputSchema': {
                'type': 'object',
                'properties': {'after': {'type': 'integer', 'minimum': 0}},
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False, 'idempotentHint': True},
        },
    ]


def fallback_contract(*, urgency: bool) -> dict[str, Any]:
    # Machine-readable invariants rather than a prose persona prompt. A future
    # controller-native /controller/entry endpoint can replace this wholesale.
    return {
        'schema': 'archie-entry/v1',
        'authority': 'controller',
        'continuity': 'backend-owned',
        'occupancy': 'provider-replaceable',
        'mutation_membrane': 'controller',
        'surface': 'live-field',
        'provider_failure_completes_task': False,
        'plaintext_persistence': False,
        'ack_required': False,
        'urgency': bool(urgency),
        'temporal_detail_exposed': False,
    }


def entry_capsule(*, controller_get: Callable[[str, float], dict], field_snapshot: Callable[[int], dict], urgency: Callable[[], bool]):
    native = controller_get('/controller/entry', 1.6)
    if isinstance(native, dict) and native.get('ok') and isinstance(native.get('contract'), dict):
        contract = dict(native['contract'])
        contract['urgency'] = bool(urgency())
        contract.pop('deadline', None)
        contract.pop('remaining_ms', None)
        contract.pop('timebox', None)
    else:
        contract = fallback_contract(urgency=urgency())
    controller = controller_get('/controller', 1.8)
    if not isinstance(controller, dict):
        controller = {'ok': False, 'error': 'controller'}
    # The bridge's controller projection is responsible for stripping hidden
    # pressure clocks before this function is wired in production.
    return {
        'schema': 'archie-entry-capsule/v1',
        'contract': contract,
        'controller': controller,
        'field': field_snapshot(0),
    }


def handle(message: dict[str, Any], *, controller_get, controller_action, field_snapshot, urgency):
    req_id = message.get('id')
    method = str(message.get('method') or '')
    params = message.get('params') if isinstance(message.get('params'), dict) else {}

    if method == 'initialize':
        requested = str(params.get('protocolVersion') or PROTOCOL)
        version = requested if requested in {PROTOCOL, '2025-03-26'} else PROTOCOL
        return _jsonrpc(req_id, result={
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'archie-live-field', 'version': '1.0.0'},
            'instructions': 'Use enter_controller for PC/controller/live-field intent before asking for prior context.',
        })
    if method == 'notifications/initialized':
        return None
    if method == 'ping':
        return _jsonrpc(req_id, result={})
    if method == 'tools/list':
        return _jsonrpc(req_id, result={'tools': tool_list()})
    if method != 'tools/call':
        return _jsonrpc(req_id, error={'code': -32601, 'message': 'method not found'})

    name = str(params.get('name') or '')
    args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
    if name == 'enter_controller':
        try:
            capsule = entry_capsule(
                controller_get=controller_get,
                field_snapshot=field_snapshot,
                urgency=urgency,
            )
            failed = not bool((capsule.get('controller') or {}).get('ok', True))
            return _jsonrpc(req_id, result=_tool_result(capsule, error=failed))
        except Exception as exc:
            return _jsonrpc(req_id, result=_tool_result({'schema': 'archie-entry-capsule/v1', 'error': f'{type(exc).__name__}: {exc}'}, error=True))
    if name == 'field_snapshot':
        try:
            after = max(0, int(args.get('after') or 0))
        except Exception:
            after = 0
        return _jsonrpc(req_id, result=_tool_result({'schema': 'archie-field-snapshot/v1', **field_snapshot(after)}))
    if name == 'controller_action':
        action = args.get('action') if isinstance(args.get('action'), dict) else {}
        if not action.get('action'):
            return _jsonrpc(req_id, result=_tool_result({'ok': False, 'error': 'action'}, error=True))
        result = controller_action(dict(action))
        failed = not (isinstance(result, dict) and bool(result.get('ok', True)) and not result.get('error'))
        return _jsonrpc(req_id, result=_tool_result({'schema': 'archie-action-result/v1', 'result': result}, error=failed))
    return _jsonrpc(req_id, error={'code': -32602, 'message': 'unknown tool'})


def dumps(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
