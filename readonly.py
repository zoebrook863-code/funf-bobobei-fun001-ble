"""Offline audit entry points. No BLE or network transport is present."""
import argparse
import json
from pathlib import Path
import sys
from device_safety import (NONZERO_BLOCK_REASON, live_nonzero_allowed,
                           nonzero_bench_allowed)

from fun001_protocol import MODE_NAMES

ROOT = Path(__file__).resolve().parent
DOCUMENTS = {'audit': 'AUDIT.md', 'guide': 'READONLY_BLE_GUIDE.md',
             'plan': 'IMPLEMENTATION_PLAN.md'}
VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18')
TOOLS = {'fun001_status': 'Offline project readiness, with unknown hardware state',
         'fun001_audit': 'Read the reference and environment audit',
         'fun001_collection_guide': 'Read the read-only BLE collection guide'}


def status():
    return {'stage': 'experimental_local_ble', 'protocol': 'fun001-apk307-offline-v2',
            'gatt_evidence': 'owner_device_capture_and_app_analysis',
            'mode_catalog': [{'mode': code, 'name': name}
                             for code, name in MODE_NAMES.items()],
            'pause_wire_observed': True, 'physical_stop_verified': False,
            'apk_version': '3.0.7', 'nonzero_physical_verified': True,
            'physical_verification_scope': 'single_device_mode1_lowest_suction_vibration_and_combined',
            'lowest_vibration_bench_verified': True,
            'all_channels_physical_verified': False,
            'firmware_disconnect_stop_verified': False,
            'windows_session_close_stop_test': 'failed_observation_window',
            'physical_radio_disconnect_verified': False,
            'manual_nonzero_bench_available': nonzero_bench_allowed(),
            'live_nonzero_enabled': live_nonzero_allowed(),
            'nonzero_block_reason': (None if live_nonzero_allowed()
                                     else NONZERO_BLOCK_REASON),
            'hardware_state': 'unknown', 'ble_transport': 'windows_ble',
            'manual_zero_bench_available': True,
            'real_write_enabled': live_nonzero_allowed(),
            'session_authorization': 'absent',
            'relay_deployed': False, 'mcp_scope': 'local_stdio_and_optional_authenticated_http',
            'mock_relay_available': True, 'mock_mcp_entrypoint': 'mock_mcp.py',
            'local_control_entrypoint': 'fun001_service.py',
            'local_mcp_entrypoint': 'fun001_mcp.py',
            'local_control_scope': 'real_local_ble',
            'local_control_real_device_verified': True,
            'phase1_diagnostics_entrypoint': 'diagnostics_phase1.py',
            'remote_scope': 'optional_user_configured_http',
            'mock_scope': 'semantic_mock_only'}


def document(name):
    return (ROOT / DOCUMENTS[name]).read_text(encoding='utf-8')


def dispatch(request, *, tools=None, call_tool=None, server_info=None):
    """Return JSON-RPC responses without reflecting user-supplied data."""
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
        return error(None, -32600, 'Invalid request')
    request_id = request.get('id')
    if request_id is not None and type(request_id) not in (str, int):
        return error(None, -32600, 'Invalid request id')
    method = request.get('method')
    if not isinstance(method, str):
        return error(request_id, -32600, 'Invalid method')
    if 'id' not in request:
        return None
    params = request.get('params', {})
    if not isinstance(params, dict):
        return error(request_id, -32602, 'Invalid params')
    if method == 'initialize':
        version = params.get('protocolVersion')
        result = {'protocolVersion': version if version in VERSIONS else VERSIONS[-1],
                  'capabilities': {'tools': {}},
                  'serverInfo': server_info or {'name': 'fun001-offline-audit', 'version': '0.1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': tools if tools is not None else [
            {'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': {},
                             'additionalProperties': False},
             'annotations': {'readOnlyHint': True, 'openWorldHint': False}}
            for name, description in TOOLS.items()]}
    elif method == 'tools/call':
        name = params.get('name')
        allowed_names = TOOLS if tools is None else {tool['name'] for tool in tools}
        if not isinstance(name, str) or name not in allowed_names:
            return error(request_id, -32602, 'Unknown tool')
        arguments = params.get('arguments', {})
        if not isinstance(arguments, dict):
            return error(request_id, -32602, 'Invalid tool arguments')
        if call_tool is not None:
            result = call_tool(name, arguments)
            return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        if arguments != {}:
            return error(request_id, -32602, 'This tool accepts no arguments')
        if name == 'fun001_status':
            content = json.dumps(status(), ensure_ascii=False)
        else:
            content = document('audit' if name == 'fun001_audit' else 'guide')
        result = {'content': [{'type': 'text', 'text': content}], 'isError': False}
    else:
        return error(request_id, -32601, 'Method not found')
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id,
            'error': {'code': code, 'message': message}}


def serve(dispatcher=dispatch):
    for line in sys.stdin:
        # Some PowerShell hosts prepend a UTF-8 BOM to the first pipeline line.
        line = line.lstrip('\ufeff').strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = dispatcher(request)
        except (ValueError, RecursionError):
            response = error(None, -32700, 'Parse error')
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', *DOCUMENTS, 'mcp'])
    action = parser.parse_args().action
    if action == 'mcp':
        serve()
    elif action == 'status':
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        print(document(action))


if __name__ == '__main__':
    main()
