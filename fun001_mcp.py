"""Local stdio MCP client for the resident FUN-001 control service.

This entry point is a thin client. It cannot create an authorization: the
owner enables a standing permission or creates a timed grant locally.
Live writes follow the owner's policy in device_safety.py and the core's
session envelope. Long runs leave the reader free for stop and cancellation.
"""
import copy
import http.client
import json
import secrets
import sys
import threading
import time

import readonly
from fun001_core import SCOPE
from fun001_service import ControlClient
from fun001_protocol import FUNCTIONS, MODE_NAMES

SESSION = {'type': 'string', 'minLength': 1, 'maxLength': 128,
           'description': 'Current authorization handle returned by status'}
FUNCTION_SCHEMA = {'type': 'string', 'enum': list(FUNCTIONS),
                   'description': 'Must match the function in the owner grant; omitted uses that function'}
MODE_SCHEMA = {'type': 'integer', 'enum': [1, 2, 3, 4],
               'description': ('Waveform presets: ' + '; '.join(f'{code}={name}' for code, name in MODE_NAMES.items())
                               + '. Must be owner-authorized; omitted uses the first authorized mode')}
STATE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': 'Full three-channel target; omitted channels are zero',
    'properties': {name: {
        'type': 'object', 'required': ['level'], 'additionalProperties': False,
        'properties': {'level': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                       'mode': MODE_SCHEMA}}
        for name in FUNCTIONS}}
RUN_TOOLS = {'run_seconds', 'run_state_seconds'}

DEFINITIONS = (
    ('status', 'Read real local link, outcome and authorization state', {}, []),
    ('project_status', 'OFFLINE. Project readiness summary; not live hardware state', {}, []),
    ('project_audit', 'OFFLINE. Read the reference and environment audit', {}, []),
    ('set_intensity', 'Set the owner-authorized function and mode, clamped to the owner cap',
     {'session_id': SESSION,
      'level': {'type': 'integer', 'minimum': 0, 'maximum': 100},
      'key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
              'description': 'Idempotency key; never reuse with different input'},
      'ttl': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1},
      'function': FUNCTION_SCHEMA, 'mode': MODE_SCHEMA},
     ['session_id', 'level', 'key']),
    ('run_seconds', 'Sustain an owner-authorized level and mode for N seconds, then zero',
     {'session_id': SESSION,
      'seconds': {'type': 'number', 'minimum': 1, 'maximum': 300},
      'level': {'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 1},
      'function': FUNCTION_SCHEMA, 'mode': MODE_SCHEMA},
     ['session_id', 'seconds']),
    ('set_state', 'Replace the three-channel state within owner-authorized per-channel limits',
     {'session_id': SESSION, 'state': STATE_SCHEMA,
      'key': {'type': 'string', 'minLength': 1, 'maxLength': 128},
      'ttl': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}},
     ['session_id', 'state', 'key']),
    ('run_state_seconds', 'Sustain a full three-channel state, then zero all channels',
     {'session_id': SESSION, 'state': STATE_SCHEMA,
      'seconds': {'type': 'number', 'minimum': 1, 'maximum': 300}},
     ['session_id', 'state', 'seconds']),
    ('heartbeat', 'Renew the claimed session heartbeat, subject to grant and lock expiry',
     {'session_id': SESSION}, ['session_id']),
    ('stop', 'Zero and release the current session; standing permission permits later reconnection',
     {'session_id': SESSION}, ['session_id']),
    ('emergency_stop', 'Zero and revoke standing permission and the session; available without a session', {}, []),
    ('release_control', 'Zero and revoke standing permission and the current session',
     {'session_id': SESSION}, ['session_id']),
)
TOOLS = [
    {'name': action,
     'description': 'REAL LOCAL DEVICE. ' + description,
     'inputSchema': {'type': 'object', 'properties': properties,
                     'required': required, 'additionalProperties': False},
     'annotations': {'readOnlyHint': action in ('status', 'project_status',
                                                'project_audit'),
                     'openWorldHint': False}}
    for action, description, properties, required in DEFINITIONS
]
OFFLINE = {'project_status', 'project_audit'}


def run_seconds(client, arguments, *, cancel=None, clock=time.monotonic, wait=None):
    session = arguments.get('session_id')
    seconds = arguments.get('seconds')
    level = arguments.get('level', 1)
    if (set(arguments) - {'session_id', 'seconds', 'level', 'function', 'mode'}
            or not isinstance(session, str) or not 1 <= len(session) <= 128
            or type(seconds) not in (int, float)
            or not 1 <= seconds <= 300):
        raise ValueError('Invalid run_seconds arguments')
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError('Invalid intensity')
    if 'function' in arguments and (not isinstance(arguments['function'], str)
                                    or arguments['function'] not in FUNCTIONS):
        raise ValueError('Invalid function')
    if 'mode' in arguments and (type(arguments['mode']) is not int
                                or arguments['mode'] not in range(1, 5)):
        raise ValueError('Invalid mode')
    selection = {key: arguments[key] for key in ('function', 'mode') if key in arguments}
    result = _run_timed(client, session, seconds, 'set_intensity',
                        {'level': level, **selection}, cancel=cancel, clock=clock, wait=wait)
    return {**result, 'requested_level': level}


def run_state_seconds(client, arguments, *, cancel=None, clock=time.monotonic, wait=None):
    if set(arguments) != {'session_id', 'seconds', 'state'}:
        raise ValueError('Invalid run_state_seconds arguments')
    state = arguments['state']
    if not isinstance(state, dict) or state.keys() - FUNCTIONS.keys():
        raise ValueError('Invalid state channels')
    for value in state.values():
        if (not isinstance(value, dict) or 'level' not in value
                or value.keys() - {'level', 'mode'}
                or type(value['level']) is not int or not 0 <= value['level'] <= 100):
            raise ValueError('Invalid channel state')
        if 'mode' in value and (type(value['mode']) is not int or value['mode'] not in range(1, 5)):
            raise ValueError('Invalid mode')
    state = copy.deepcopy(state)
    result = _run_timed(client, arguments['session_id'], arguments['seconds'],
                        'set_state', {'state': state}, cancel=cancel, clock=clock, wait=wait)
    return {**result, 'requested_state': state}


def _run_timed(client, session, seconds, action, arguments, *, cancel, clock, wait):
    if (not isinstance(session, str) or not 1 <= len(session) <= 128
            or type(seconds) not in (int, float) or not 1 <= seconds <= 300):
        raise ValueError('Invalid timed command arguments')
    cancel = cancel if cancel is not None else threading.Event()
    wait = wait if wait is not None else cancel.wait
    deadline = clock() + seconds
    result = {'outcome': 'cancelled', 'level': None}
    try:
        while not cancel.is_set():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            result = client.request(action, {
                'session_id': session, **arguments,
                'key': secrets.token_hex(8), 'ttl': min(1.0, remaining)})
            if result.get('outcome') != 'gatt_ack':
                break
            if cancel.is_set() or clock() >= deadline:
                break
            client.request('heartbeat', {'session_id': session})
            remaining = deadline - clock()
            if remaining > 0:
                wait(min(0.5, remaining))
    except (OSError, ValueError, http.client.HTTPException):
        result = {'outcome': 'rejected_or_unknown', 'level': None,
                  'retry_nonzero': False}
    finally:
        # Keep execution and cleanup outcomes separate, including a revoked
        # session after a concurrent stop. Never replay a failed nonzero write.
        try:
            zero_arguments = {'state': {}} if action == 'set_state' else {'level': 0}
            zero = client.request(action, {
                'session_id': session, **zero_arguments,
                'key': secrets.token_hex(8), 'ttl': 1.0})
        except (OSError, ValueError, http.client.HTTPException):
            zero = {'outcome': 'rejected_or_unknown', 'level': None}
            # At the deadline the resident watchdog may already have zeroed
            # and revoked this grant before the explicit cleanup arrives.
            try:
                status = client.request('status')
                if (status.get('reason') == 'command_ttl'
                        and status.get('outcome') == 'gatt_ack'
                        and status.get('authorization_active') is False
                        and status.get('unknown_latched') is False
                        and status.get('desired_level') == 0):
                    zero = {'outcome': 'gatt_ack', 'level': 0,
                            'state': status.get('desired_state'),
                            'source': 'resident_command_ttl_stop'}
            except (OSError, ValueError, http.client.HTTPException):
                pass
    execution = result.copy()
    if cancel.is_set() and result.get('outcome') == 'gatt_ack':
        result = {'outcome': 'cancelled', 'level': None}
    if result.get('outcome') == 'gatt_ack':
        result = zero if zero.get('outcome') == 'gatt_ack' else {
            'outcome': 'unknown', 'level': None}
    result = {**result, 'zero_outcome': zero.get('outcome', 'unknown'),
              'applied_level': execution.get('level'),
              'applied_mode': execution.get('mode'),
              'applied_state': execution.get('state'),
              'execution_outcome': execution.get('outcome')}
    return result


def dispatch(request, client, *, cancel=None):
    def call_tool(name, arguments):
        if name in OFFLINE:
            if arguments:
                return {'content': [{'type': 'text',
                                     'text': 'This tool accepts no arguments'}],
                        'isError': True}
            if name == 'project_status':
                text = json.dumps(readonly.status(), ensure_ascii=False)
            else:
                text = readonly.document('audit')
            return {'content': [{'type': 'text', 'text': text}], 'isError': False}
        try:
            if isinstance(name, str) and name in RUN_TOOLS:
                runner = run_seconds if name == 'run_seconds' else run_state_seconds
                result = runner(client, arguments, cancel=cancel)
            else:
                result = client.request(name, arguments)
            failed = (name != 'status' and result.get('outcome') != 'gatt_ack')
        except (OSError, ValueError, http.client.HTTPException):
            # A transport timeout never triggers a replay of this action.
            result = {'scope': SCOPE, 'outcome': 'rejected_or_unknown',
                      'real_write_enabled': False, 'retry_nonzero': False}
            failed = True
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                'isError': failed}

    return readonly.dispatch(request, tools=TOOLS, call_tool=call_tool,
                             server_info={'name': 'fun001-local-control',
                                          'version': '0.1.0'})


def serve(client, source=None, output=None):
    source = sys.stdin if source is None else source
    output = sys.stdout if output is None else output
    output_lock = threading.Lock()
    worker = None
    cancel = None
    active_id = None
    active_session = None
    suppress_response = None

    def emit(response):
        if response is not None:
            with output_lock:
                print(json.dumps(response, ensure_ascii=False), file=output, flush=True)

    def run(request, event, suppress):
        response = dispatch(request, client, cancel=event)
        if not suppress.is_set():
            emit(response)

    try:
        for line in source:
            line = line.lstrip('\ufeff').strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                emit(readonly.error(None, -32700, 'Parse error'))
                continue
            valid_request = (isinstance(request, dict) and request.get('jsonrpc') == '2.0'
                             and isinstance(request.get('method'), str)
                             and isinstance(request.get('params', {}), dict)
                             and (request.get('id') is None
                                  or type(request['id']) in (str, int)))
            if not valid_request:
                emit(dispatch(request, client))
                continue
            params = request.get('params', {}) if isinstance(request, dict) else {}
            params = params if isinstance(params, dict) else {}
            method = request.get('method') if isinstance(request, dict) else None
            name = params.get('name') if method == 'tools/call' else None
            arguments = params.get('arguments', {})
            arguments = arguments if isinstance(arguments, dict) else {}
            busy = worker is not None and worker.is_alive()
            if (busy and method == 'notifications/cancelled'
                    and 'id' not in request
                    and type(params.get('requestId')) is type(active_id)
                    and params.get('requestId') == active_id):
                suppress_response.set()
                cancel.set()
            if busy and method == 'tools/call' and 'id' in request:
                valid_stop = (name == 'emergency_stop' and not arguments) or (
                    name in ('stop', 'release_control')
                    and set(arguments) == {'session_id'}
                    and arguments['session_id'] == active_session)
                if valid_stop and isinstance(params.get('arguments', {}), dict):
                    cancel.set()
                if name in ('run_seconds', 'run_state_seconds', 'set_intensity', 'set_state'):
                    emit(readonly.error(request.get('id'), -32602, 'Run already active'))
                    continue
            if (isinstance(name, str) and name in RUN_TOOLS and isinstance(request, dict)
                    and request.get('jsonrpc') == '2.0' and 'id' in request
                    and (request['id'] is None or type(request['id']) in (str, int))):
                cancel = threading.Event()
                suppress_response = threading.Event()
                active_id = request['id']
                active_session = arguments.get('session_id')
                worker = threading.Thread(target=run,
                                          args=(request, cancel, suppress_response), daemon=True)
                worker.start()
            else:
                emit(dispatch(request, client))
    finally:
        if worker is not None:
            suppress_response.set()
            cancel.set()
            worker.join(timeout=10)


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    client = ControlClient()
    try:
        serve(client)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == '__main__':
    main()
