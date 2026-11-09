"""Loopback relay backed exclusively by the semantic MockTransport."""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import sys
import threading
import time

from local_runtime import (LoopbackClient, LoopbackServer, RuntimeLock,
                           RequestRejected, secure_directory, write_private)
from mock_control import ControlRejected, MockSafetyController, MockTransport

RUNTIME = Path(__file__).resolve().parents[1] / 'private' / 'mock-relay'
DEFAULT_PORT = 18761
SCOPE = 'semantic_mock_only'
ACTIONS = {
    'status': set(),
    'set_intensity': {'session_id', 'level', 'key', 'ttl'},
    'heartbeat': {'session_id'},
    'stop': {'session_id'},
    'release_control': {'session_id'},
    'emergency_stop': set(),
    'client_closed': {'session_id'},
}


class MockRelayState:
    def __init__(self, clock=time.monotonic):
        self.transport = MockTransport()
        self.controller = MockSafetyController(self.transport, clock)
        self.lock = threading.RLock()
        self.session_id = None
        self.client_id = None
        self.closed = False

    def tick(self):
        with self.lock:
            self.controller.tick()
            if self.controller.token is None:
                self.session_id = None
                self.client_id = None

    def status(self):
        with self.lock:
            self.tick()
            return {'scope': SCOPE, 'real_write_enabled': False,
                    'mock_level': self.transport.level,
                    'desired_level': self.controller.desired_level,
                    'outcome': self.controller.result, 'reason': self.controller.reason,
                    'authorization_active': self.session_id is not None,
                    'session_id': self.session_id, 'controller_claimed': self.client_id is not None,
                    'mock_link_connected': self.transport.connected}

    def authorize(self):
        """Owner-console action, deliberately absent from the HTTP/MCP APIs."""
        with self.lock:
            if self.closed:
                raise ControlRejected('Mock service closed')
            self.tick()
            self.controller.authorize(duration=30, cap=1)
            # The owner grant can wait at zero until claimed. Once a controller
            # acts, the ordinary 2-second heartbeat and 3-second lock apply.
            self.controller.heartbeat_deadline = self.controller.session_deadline
            self.controller.lock_deadline = self.controller.session_deadline
            self.session_id = secrets.token_hex(16)
            self.client_id = None

    def _check_session(self, arguments):
        self.tick()
        session = arguments.get('session_id')
        if (not isinstance(session, str) or self.session_id is None
                or not secrets.compare_digest(session, self.session_id)):
            raise ControlRejected('Current mock session required')

    def execute(self, action, arguments, client_id):
        with self.lock:
            if self.closed:
                raise ControlRejected('Mock service closed')
            if not isinstance(action, str) or action not in ACTIONS:
                raise ControlRejected('Unknown mock action')
            if not isinstance(arguments, dict) or arguments.keys() - ACTIONS[action]:
                raise ControlRejected('Invalid mock arguments')
            if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
                raise ControlRejected('Invalid mock controller')
            if action == 'status':
                return self.status()
            if action == 'emergency_stop':
                self.controller.emergency_stop()
                return self.status()
            self._check_session(arguments)
            if action == 'client_closed':
                if self.client_id == client_id:
                    self.controller.network_disconnected()
                return self.status()
            if self.client_id is not None and self.client_id != client_id:
                raise ControlRejected('Mock controller lock held')
            token = self.controller.token
            if action == 'set_intensity':
                if not {'level', 'key'} <= arguments.keys():
                    raise ControlRejected('Mock level and request key required')
                result = self.controller.set_level(
                    token, level=arguments['level'], key=arguments['key'],
                    ttl=arguments.get('ttl', 1))
                if self.client_id is None and self.controller.token is not None:
                    self.controller.heartbeat(token)
                self.client_id = client_id
                return {**result, 'scope': SCOPE, 'real_write_enabled': False}
            if action == 'heartbeat':
                self.controller.heartbeat(token)
                if self.client_id is None:
                    self.controller.lock_deadline = self.controller.clock() + 3
                self.client_id = client_id
            else:
                self.controller.stop(token)
            return self.status()

    def owner_event(self, action):
        with self.lock:
            events = {'stop': self.controller.emergency_stop,
                      'network-drop': self.controller.network_disconnected,
                      'link-drop': self.controller.ble_disconnected,
                      'reconnect': self.controller.reconnect}
            events[action]()
            return self.status()

    def close(self):
        with self.lock:
            self.closed = True
            self.controller.emergency_stop()
            self.tick()


class MockRelayServer(LoopbackServer):
    def __init__(self, *, directory=RUNTIME, port=DEFAULT_PORT, state=None):
        super().__init__(state=state or MockRelayState(), directory=directory, port=port,
                         scope=SCOPE, credential='MOCK_API_TOKEN',
                         lock_message='Mock instance already running')


class MockRelayClient(LoopbackClient):
    def __init__(self, directory=RUNTIME):
        super().__init__(directory=directory, scope=SCOPE, credential='MOCK_API_TOKEN',
                         error_type=ControlRejected)
        self.session_id = None

    def request(self, action, arguments=None):
        result = super().request(action, arguments)
        if action in ('set_intensity', 'heartbeat'):
            self.session_id = (arguments or {}).get('session_id')
        return result

    def close(self):
        if self.session_id is not None:
            try:
                self.request('client_closed', {'session_id': self.session_id})
            except (OSError, ValueError, http.client.HTTPException):
                pass
            self.session_id = None


def owner_console():
    relay = MockRelayServer()
    relay.start()
    print('模拟服务已启动，仅绑定 127.0.0.1，设备可保持关机。')
    print('命令 grant 开启模拟授权，level 1 模拟最低值，status 查看状态。')
    print('命令 stop、network-drop、link-drop、reconnect、quit。授权最多 30 秒。')
    try:
        while True:
            try:
                command = input('mock> ').strip()
                if command == 'quit':
                    break
                if command == 'grant':
                    relay.state.authorize()
                elif command == 'level 1':
                    session = relay.state.status()['session_id']
                    relay.state.execute('set_intensity', {'session_id': session, 'level': 1,
                                                        'key': secrets.token_hex(8), 'ttl': 0.25},
                                        'owner-console')
                elif command in ('stop', 'network-drop', 'link-drop', 'reconnect'):
                    relay.state.owner_event(command)
                elif command != 'status':
                    print('未知模拟命令。')
                    continue
                status = relay.state.status()
                status.pop('session_id')
                print(json.dumps(status, ensure_ascii=False))
            except ControlRejected:
                print('模拟动作被拒绝，请检查授权、控制锁和模拟连接。')
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        relay.close()
        print('模拟服务已关闭，运行时凭据已清理。')


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('serve', 'status'))
    action = parser.parse_args().action
    try:
        if action == 'serve':
            owner_console()
        else:
            result = MockRelayClient().request('status')
            result.pop('session_id')
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, RequestRejected,
            http.client.HTTPException):
        print(json.dumps({'scope': SCOPE, 'status': 'mock_service_unavailable'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
