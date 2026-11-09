import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from device_safety import NONZERO_BLOCK_REASON
from fun001_core import ControlRejected, LinkUnavailable, SCOPE, UnknownExecution
from fun001_protocol import build_control_frame, build_stop_frames
from fun001_service import (ControlClient, ControlServer, ControlState,
                             sustained_level)
import fun001_mcp

ZERO_BODY = build_stop_frames()[0][1:]
ONE_BODY = build_control_frame('vibration', 1, mode=1, max_intensity=1)[1:]


class FakeLink:
    def __init__(self):
        self.connected = False
        self.frames = []
        self.close_count = 0
        self.lost = False
        self.fail_writes = []

    def ensure_connected(self):
        self.connected = True

    def write(self, frame):
        if self.fail_writes:
            kind = self.fail_writes.pop(0)
            raise UnknownExecution('unknown') if kind == 'unknown' else LinkUnavailable('unavailable')
        if not self.connected:
            raise LinkUnavailable('link_not_open')
        self.frames.append(frame)

    def disconnect(self):
        self.connected = False

    def take_link_lost(self):
        lost, self.lost = self.lost, False
        return lost

    def close(self):
        self.connected = False
        self.close_count += 1

    def bodies(self):
        return [frame[1:] for frame in self.frames]


class ControlStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.link = FakeLink()
        self.state = ControlState(self.link, clock=lambda: self.now,
                                  nonzero_allowed=lambda: False)
        self.state.authorize()
        self.session = self.state.status()['session_id']

    def execute(self, action, arguments, client='first-client'):
        return self.state.execute(action, arguments, client)

    def test_grant_zeroes_connects_and_reports_layered_state(self):
        self.assertEqual(self.link.bodies(), [ZERO_BODY])
        status = self.state.status()
        self.assertEqual(status['scope'], SCOPE)
        self.assertTrue(status['authorization_active'])
        self.assertFalse(status['controller_claimed'])
        self.assertTrue(status['link_connected'])
        self.assertEqual(status['application_gatt_session'], 'open')
        self.assertFalse(status['physical_radio_disconnect_verified'])
        self.assertFalse(status['physical_stop_verified'])
        self.assertFalse(status['device_confirmed'])
        self.assertEqual(status['device_state'], 'unknown')
        self.assertFalse(status['real_write_enabled'])
        self.assertEqual(status['nonzero_block_reason'], NONZERO_BLOCK_REASON)
        self.assertEqual(status['verified_envelope'],
                         {'function': 'vibration', 'mode': 1, 'max_intensity': 1})
        self.assertEqual(status['outcome'], 'gatt_ack')

    def test_second_grant_is_rejected(self):
        with self.assertRaises(ControlRejected):
            self.state.authorize()

    def test_mcp_cannot_grant(self):
        with self.assertRaises(ControlRejected):
            self.execute('authorize', {})

    def test_blocked_nonzero_does_not_write_or_claim(self):
        result = self.execute('set_intensity',
                              {'session_id': self.session, 'level': 1, 'key': 'one'})
        self.assertEqual(result['outcome'], 'blocked_by_disconnect_safety')
        self.assertIsNone(result['level'])
        self.assertEqual(self.link.bodies(), [ZERO_BODY])
        self.assertFalse(self.state.status()['controller_claimed'])

    def test_second_client_is_locked_but_emergency_takes_over(self):
        self.state.controller._nonzero_allowed = lambda: True
        self.execute('set_intensity',
                     {'session_id': self.session, 'level': 1, 'key': 'one'})
        with self.assertRaises(ControlRejected):
            self.execute('heartbeat', {'session_id': self.session}, 'second-client')
        self.state.execute('emergency_stop', {}, 'second-client')
        self.assertFalse(self.link.connected)
        self.assertFalse(self.state.status()['authorization_active'])
        self.assertEqual(self.link.bodies(), [ZERO_BODY, ONE_BODY, ZERO_BODY])

    def test_stop_and_release_zero_disconnect_and_clear_session(self):
        for action in ('stop', 'release_control'):
            with self.subTest(action=action):
                if action == 'release_control':
                    self.state.authorize()
                session = self.state.status()['session_id']
                self.execute(action, {'session_id': session})
                self.assertFalse(self.link.connected)
                self.assertFalse(self.state.status()['authorization_active'])
                self.assertEqual(self.link.bodies()[-1], ZERO_BODY)

    def test_only_controlling_client_close_revokes(self):
        self.state.controller._nonzero_allowed = lambda: True
        self.execute('set_intensity',
                     {'session_id': self.session, 'level': 1, 'key': 'one'})
        self.execute('client_closed', {'session_id': self.session}, 'second-client')
        self.assertTrue(self.link.connected)
        self.execute('client_closed', {'session_id': self.session}, 'first-client')
        self.assertFalse(self.link.connected)
        self.assertEqual(self.state.controller.reason, 'client_disconnected')
        self.assertFalse(self.state.status()['authorization_active'])

    def test_session_and_argument_validation(self):
        for action, arguments in (('set_intensity', {}),
                                  ('status', {'raw': 'hidden'}),
                                  ('set_intensity', {'session_id': self.session, 'level': 1}),
                                  ('unknown', {})):
            with self.subTest(action=action, arguments=arguments), self.assertRaises(ControlRejected):
                self.execute(action, arguments)
        with self.assertRaises(ControlRejected):
            self.execute('heartbeat', {'session_id': 'old'})
        self.assertFalse(self.state.status()['controller_claimed'])

    def test_idle_grant_expires_after_twenty_minutes(self):
        self.now = 1300
        self.state.tick()
        self.assertFalse(self.state.status()['authorization_active'])
        self.assertFalse(self.link.connected)
        self.assertEqual(self.state.controller.reason, 'session_expired')

    def test_zero_burst_keeps_session_active_within_grant(self):
        self.state.execute('set_intensity', {'session_id': self.session,
                                            'level': 0, 'key': 'zero',
                                            'ttl': 1.0}, 'first-client')
        self.now = 110
        self.state.tick()
        status = self.state.status()
        self.assertTrue(status['authorization_active'])
        self.assertTrue(self.link.connected)
        self.assertEqual(status['desired_level'], 0)

    def test_close_zeroes_active_session(self):
        self.state.close()
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.close_count, 1)
        self.assertEqual(self.link.bodies(), [ZERO_BODY, ZERO_BODY])
        with self.assertRaises(ControlRejected):
            self.state.authorize()


class SustainedLevelTests(unittest.TestCase):
    class FakeState:
        def __init__(self):
            self.now = 0.0
            self.calls = []
            self.session = 'session'
            self.active = True
            self.outcome = 'gatt_ack'

        def status(self):
            return {'session_id': self.session if self.active else None,
                    'authorization_active': self.active}

        def execute(self, action, arguments, client):
            self.calls.append((action, dict(arguments)))
            if action == 'set_intensity':
                return {'outcome': self.outcome}
            if action == 'heartbeat':
                return {}
            if action == 'stop':
                self.active = False
                return {'outcome': 'gatt_ack', 'authorization_active': False}
            raise AssertionError(action)

    def test_repeats_until_deadline_then_stops(self):
        state = self.FakeState()

        def sleep(duration):
            state.now += duration

        result = sustained_level(state, 1, 1.0, clock=lambda: state.now,
                                 sleep=sleep)
        actions = [call[0] for call in state.calls]
        self.assertEqual(actions.count('set_intensity'), 4)
        self.assertEqual(actions.count('heartbeat'), 3)
        self.assertEqual(actions.count('stop'), 0)
        self.assertTrue(result['authorization_active'])
        self.assertEqual(state.calls[-1][1]['level'], 0)

    def test_rejects_out_of_range_and_non_numeric(self):
        for seconds in (0, -1, 300.5, float('nan'), True, '1'):
            with self.subTest(seconds=seconds), self.assertRaises(ControlRejected):
                sustained_level(self.FakeState(), 1, seconds)

    def test_blocked_result_stops_without_repeating(self):
        state = self.FakeState()
        state.outcome = 'blocked_by_disconnect_safety'
        sustained_level(state, 1, 1.0, clock=lambda: state.now,
                        sleep=lambda duration: None)
        actions = [call[0] for call in state.calls]
        self.assertEqual(actions.count('set_intensity'), 2)
        self.assertEqual(actions.count('stop'), 0)


class RunSecondsTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, action, arguments=None):
            self.calls.append((action, arguments or {}))
            return {'outcome': 'gatt_ack'}

    def test_repeats_heartbeats_and_stops(self):
        client = self.FakeClient()
        now = [0.0]
        def wait(seconds):
            now[0] += seconds
        result = fun001_mcp.run_seconds(client, {'session_id': 's', 'seconds': 1},
                                       clock=lambda: now[0], wait=wait)
        actions = [call[0] for call in client.calls]
        self.assertEqual(actions.count('set_intensity'), 3)
        self.assertEqual(actions.count('heartbeat'), 2)
        self.assertEqual(actions[-1], 'set_intensity')
        self.assertEqual(client.calls[-1][1]['level'], 0)
        self.assertEqual(result['outcome'], 'gatt_ack')

    def test_rejects_out_of_range(self):
        for seconds in (0, 301, '1', True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                fun001_mcp.run_seconds(self.FakeClient(),
                                       {'session_id': 's', 'seconds': seconds})


class ControlServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name) / 'runtime'
        cls.link = FakeLink()
        cls.state = ControlState(cls.link, nonzero_allowed=lambda: True)
        cls.server = ControlServer(state=cls.state, directory=cls.directory, port=0)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    def setUp(self):
        self.state.execute('emergency_stop', {}, 'test')
        self.state.authorize()
        self.client = ControlClient(self.directory)

    def tearDown(self):
        self.client.close()

    def rpc(self, name, arguments=None):
        return fun001_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                    'params': {'name': name,
                                               'arguments': arguments or {}}},
                                   self.client)

    def test_http_and_mcp_share_owner_grant_and_zero_on_close(self):
        status = json.loads(self.rpc('status')['result']['content'][0]['text'])
        self.assertTrue(status['authorization_active'])
        session = status['session_id']
        before = self.link.bodies().count(ONE_BODY)
        result = json.loads(self.rpc('set_intensity', {'session_id': session, 'level': 100,
                                                       'key': 'integration', 'ttl': 0.5})
                            ['result']['content'][0]['text'])
        self.assertEqual(result['outcome'], 'gatt_ack')
        self.assertEqual(result['level'], 1)
        self.assertEqual(self.link.bodies().count(ONE_BODY) - before, 1)
        self.client.close()
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.bodies()[-1], ZERO_BODY)

    def test_autonomous_ttl_zeroes_without_more_requests(self):
        session = self.client.request('status')['session_id']
        self.client.request('set_intensity', {'session_id': session, 'level': 1,
                                              'key': 'timer', 'ttl': 0.1})
        deadline = time.monotonic() + 2
        while self.state.controller.reason != 'command_ttl' and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.state.controller.reason, 'command_ttl')
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.bodies()[-1], ZERO_BODY)

    def test_bad_credential_origin_and_host_are_denied(self):
        for headers in ({'Authorization': 'Bearer invalid'},
                        {'Origin': 'https://example.invalid'},
                        {'Host': 'example.invalid'}):
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection(*self.server.server.server_address,
                                                        timeout=2)
                complete = {'Authorization': 'Bearer ' + self.server.api_token,
                            'Content-Type': 'application/json', **headers}
                connection.request('POST', '/rpc', '{"x":1}', complete)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertNotIn(self.server.api_token, response.read().decode())
                connection.close()

    def test_mcp_schema_and_absent_grant_tool(self):
        reply = fun001_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                    self.client)
        tools = reply['result']['tools']
        self.assertEqual([tool['name'] for tool in tools],
                         ['status', 'project_status', 'project_audit', 'set_intensity',
                          'run_seconds', 'set_state', 'run_state_seconds', 'heartbeat', 'stop', 'emergency_stop',
                          'release_control'])
        read_only = {tool['name'] for tool in tools
                     if tool['annotations']['readOnlyHint']}
        self.assertEqual(read_only, {'status', 'project_status', 'project_audit'})
        self.assertEqual(self.rpc('authorize')['error']['code'], -32602)
        self.assertEqual(self.rpc('status', {'invalid': True})['result']['isError'], True)
        project = self.rpc('project_status')['result']
        self.assertFalse(project['isError'])
        self.assertEqual(json.loads(project['content'][0]['text'])['hardware_state'],
                         'unknown')
        self.assertTrue(self.rpc('project_status', {'invalid': True})['result']['isError'])

    def test_project_audit_is_readable_offline(self):
        client = ControlClient(self.directory / 'missing')
        result = fun001_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': 'project_audit'}}, client)['result']
        self.assertFalse(result['isError'])
        self.assertIn('审计', result['content'][0]['text'])

    def test_stdio_emergency_preempts_long_run_over_loopback(self):
        session = self.state.status()['session_id']
        script = ('from pathlib import Path\nimport sys\nimport fun001_mcp\n'
                  'from fun001_service import ControlClient\n'
                  'client = ControlClient(Path(sys.argv[1]))\n'
                  'try:\n    fun001_mcp.serve(client)\n'
                  'finally:\n    client.close()\n')
        process = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.directory)],
                                   cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8')
        def send(identifier, name, arguments):
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identifier,
                                            'method': 'tools/call',
                                            'params': {'name': name, 'arguments': arguments}}) + '\n')
            process.stdin.flush()
        try:
            before = len(self.link.frames)
            send(1, 'run_seconds', {'session_id': session, 'seconds': 300})
            deadline = time.monotonic() + 5
            while len(self.link.frames) <= before and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(len(self.link.frames), before)
            self.assertEqual(self.link.bodies()[-1], ONE_BODY)
            send(2, 'emergency_stop', {})
            deadline = time.monotonic() + 3
            while self.link.connected and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.link.connected)
            self.assertEqual(self.link.bodies()[-1], ZERO_BODY)
        finally:
            output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(errors, '')
        replies = [json.loads(line) for line in output.splitlines()]
        self.assertIn(2, {reply['id'] for reply in replies})
        emergency = next(reply for reply in replies if reply['id'] == 2)
        self.assertFalse(emergency['result']['isError'])

    def test_offline_client_does_not_start_a_server(self):
        client = ControlClient(self.directory / 'missing')
        response = fun001_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                        'params': {'name': 'emergency_stop'}}, client)
        self.assertTrue(response['result']['isError'])
        self.assertFalse((self.directory / 'missing').exists())


class ControlLifecycleTests(unittest.TestCase):
    def test_shutdown_removes_credentials_and_releases_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'runtime'
            link = FakeLink()
            state = ControlState(link, nonzero_allowed=lambda: True)
            server = ControlServer(state=state, directory=directory, port=0)
            server.start()
            state.authorize()
            port = server.server.server_port
            server.close()
            self.assertFalse((directory / '.env').exists())
            self.assertFalse((directory / 'endpoint.json').exists())
            self.assertFalse(link.connected)
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
            replacement = ControlServer(state=ControlState(FakeLink()),
                                        directory=directory, port=port)
            replacement.close()

    def test_stdio_is_json_only_and_lists_real_tools(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        result = subprocess.run([sys.executable, str(ROOT / 'fun001_mcp.py')],
                                input='\n'.join(json.dumps(request) for request in requests) + '\n',
                                capture_output=True, encoding='utf-8', timeout=10, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'fun001-local-control')
        self.assertEqual(len(replies[1]['result']['tools']), 11)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
