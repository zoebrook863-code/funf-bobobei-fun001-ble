"""Manual acceptance helper for the real local MCP path.

Uses the same fun001_mcp.dispatch path as the stdio server against a
running fun001_service.py. It never creates an authorization: the owner
must already have typed grant in the service console. Without --stop it
only reads status; with --stop it ends the current session with the
verified stop frame. No nonzero command is ever built.
"""
import argparse
import http.client
import json
import sys

import fun001_mcp
from fun001_service import ControlClient


def call(client, name, arguments=None):
    reply = fun001_mcp.dispatch(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': name, 'arguments': arguments or {}}}, client)
    if 'error' in reply:
        raise ValueError('rpc rejected')
    result = reply['result']
    if result.get('isError'):
        raise ValueError('tool rejected or unknown')
    return json.loads(result['content'][0]['text'])


def public(status):
    return {key: value for key, value in status.items() if key != 'session_id'}


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true',
                        help='stop the active session after reading status')
    arguments = parser.parse_args()
    client = ControlClient()
    try:
        status = call(client, 'status')
        print('status', json.dumps(public(status), ensure_ascii=False))
        if arguments.stop:
            if not status.get('authorization_active') or not status.get('session_id'):
                print('no_active_authorization')
                return 1
            result = call(client, 'stop', {'session_id': status['session_id']})
            print('stop', json.dumps(public(result), ensure_ascii=False))
        return 0
    except (OSError, http.client.HTTPException):
        print(json.dumps({'status': 'control_service_unavailable'}))
        return 1
    except ValueError:
        print(json.dumps({'status': 'rejected_or_unknown',
                          'retry_nonzero': False}))
        return 1
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
