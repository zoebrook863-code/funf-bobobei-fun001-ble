"""Separate stdio MCP client for the loopback semantic mock relay."""
import http.client
import json
import sys

from mock_relay import MockRelayClient, SCOPE
import readonly

SESSION = {'type': 'string', 'minLength': 1, 'maxLength': 128,
           'description': 'Current simulation authorization handle from mock_status'}
DEFINITIONS = (
    ('status', 'Read simulation state and current owner-created authorization handle', {}, []),
    ('set_intensity', 'Set the single simulated intensity under an owner-created grant',
     {'session_id': SESSION, 'level': {'type': 'integer', 'minimum': 0, 'maximum': 100},
      'key': {'type': 'string', 'minLength': 1, 'maxLength': 128},
      'ttl': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}},
     ['session_id', 'level', 'key']),
    ('heartbeat', 'Renew the claimed simulation heartbeat, subject to grant and lock expiry',
     {'session_id': SESSION}, ['session_id']),
    ('stop', 'Zero the simulation and revoke its current authorization',
     {'session_id': SESSION}, ['session_id']),
    ('emergency_stop', 'Owner emergency zero and revocation of the simulation', {}, []),
    ('release_control', 'Release simulation control and zero its state',
     {'session_id': SESSION}, ['session_id']),
)
TOOLS = [
    {'name': 'mock_' + action, 'description': 'SIMULATION ONLY. ' + description,
     'inputSchema': {'type': 'object', 'properties': properties,
                     'required': required, 'additionalProperties': False},
     'annotations': {'readOnlyHint': action == 'status', 'openWorldHint': False}}
    for action, description, properties, required in DEFINITIONS
]


def dispatch(request, client):
    def call_tool(name, arguments):
        try:
            result = client.request(name.removeprefix('mock_'), arguments)
            failed = False
        except (OSError, ValueError, http.client.HTTPException):
            # A transport timeout never triggers a replay of this action.
            result = {'scope': SCOPE, 'outcome': 'rejected_or_unknown',
                      'real_write_enabled': False, 'retry_nonzero': False}
            failed = True
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                'isError': failed}

    return readonly.dispatch(request, tools=TOOLS, call_tool=call_tool,
                             server_info={'name': 'fun001-semantic-mock', 'version': '0.1.0'})


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    client = MockRelayClient()
    try:
        readonly.serve(lambda request: dispatch(request, client))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == '__main__':
    main()
