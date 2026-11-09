import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mock_control import ControlRejected
from mock_relay import MockRelayClient, MockRelayServer, MockRelayState
import mock_mcp


class MockRelayStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.state = MockRelayState(lambda: self.now)
        self.state.authorize()
        self.session = self.state.status()['session_id']

    def send(self, **updates):
        arguments = {'session_id': self.session, 'level': 1, 'key': 'one'}
        arguments.update(updates)
        return self.state.execute('set_intensity', arguments, 'first-client')

    def test_idle_grant_waits_at_zero_then_claim_has_heartbeat_deadline(self):
        self.now = 20
        self.assertTrue(self.state.status()['authorization_active'])
        self.state.execute('heartbeat', {'session_id': self.session}, 'first-client')
        self.now = 22
        self.state.tick()
        self.assertFalse(self.state.status()['authorization_active'])
        self.assertEqual(self.state.status()['reason'], 'heartbeat_timeout')

    def test_grant_always_expires_after_thirty_seconds(self):
        self.now = 30
        self.assertFalse(self.state.status()['authorization_active'])
        with self.assertRaises(ControlRejected):
            self.send()

    def test_single_controller_and_emergency_takeover(self):
        self.send()
        with self.assertRaises(ControlRejected):
            self.state.execute('heartbeat', {'session_id': self.session}, 'second-client')
        self.state.execute('emergency_stop', {}, 'second-client')
        self.assertEqual(self.state.transport.level, 0)
        self.assertFalse(self.state.status()['authorization_active'])

    def test_ttl_and_duplicate_do_not_extend_authorization(self):
        self.send(ttl=0.25)
        self.now = 0.1
        self.send(ttl=0.25)
        self.assertEqual(self.state.transport.writes.count(1), 1)
        self.now = 0.25
        self.state.tick()
        self.assertEqual(self.state.transport.level, 0)
        self.assertFalse(self.state.status()['authorization_active'])

    def test_stop_and_release_revoke(self):
        for action in ('stop', 'release_control'):
            with self.subTest(action=action):
                self.send()
                self.state.execute(action, {'session_id': self.session}, 'first-client')
                self.assertFalse(self.state.status()['authorization_active'])
                self.state.authorize()
                self.session = self.state.status()['session_id']

    def test_only_controlling_client_close_revokes(self):
        self.send()
        self.state.execute('client_closed', {'session_id': self.session}, 'second-client')
        self.assertEqual(self.state.transport.level, 1)
        self.state.execute('client_closed', {'session_id': self.session}, 'first-client')
        self.assertEqual(self.state.transport.level, 0)
        self.assertEqual(self.state.status()['reason'], 'network_disconnected')

    def test_unknown_never_replayed_and_new_grant_uses_new_id(self):
        self.state.transport.fail_next_nonzero = True
        self.assertEqual(self.send()['outcome'], 'unknown')
        with self.assertRaises(ControlRejected):
            self.send()
        self.assertEqual(self.state.transport.writes.count(1), 1)
        self.state.authorize()
        self.assertNotEqual(self.state.status()['session_id'], self.session)
        with self.assertRaises(ControlRejected):
            self.send()

    def test_disconnected_mock_link_reports_unknown_until_reconnect(self):
        self.send()
        self.state.owner_event('link-drop')
        status = self.state.status()
        self.assertEqual(status['outcome'], 'unknown')
        self.assertEqual(status['mock_level'], 1)
        self.assertEqual(status['desired_level'], 0)
        self.state.owner_event('reconnect')
        self.assertEqual(self.state.status()['mock_level'], 0)
        self.assertFalse(self.state.status()['authorization_active'])

    def test_invalid_inputs_do_not_claim_controller(self):
        for updates in ({'level': True}, {'level': -1}, {'raw': 'forbidden'},
                        {'ttl': float('nan')}, {'session_id': 'old'}):
            with self.subTest(updates=updates), self.assertRaises(ControlRejected):
                self.send(**updates)
        self.assertFalse(self.state.status()['controller_claimed'])
        with self.assertRaises(ControlRejected):
            self.state.execute('authorize', {}, 'model')

    def test_shutdown_blocks_late_nonzero(self):
        self.send()
        self.state.close()
        with self.assertRaises(ControlRejected):
            self.send(key='late')
        self.assertEqual(self.state.transport.level, 0)


class MockRelayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name) / 'runtime'
        cls.relay = MockRelayServer(directory=cls.directory, port=0)
        cls.relay.start()

    @classmethod
    def tearDownClass(cls):
        cls.relay.close()
        cls.temp.cleanup()

    def setUp(self):
        self.relay.state.owner_event('reconnect')
        self.client = MockRelayClient(self.directory)

    def tearDown(self):
        self.client.close()

    def rpc(self, name, arguments=None):
        return mock_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': name, 'arguments': arguments or {}}},
                                 self.client)

    def test_http_and_mcp_share_owner_authorization(self):
        self.assertFalse(self.client.request('status')['authorization_active'])
        self.relay.state.authorize()
        status = self.rpc('mock_status')['result']
        self.assertFalse(status['isError'])
        session = json.loads(status['content'][0]['text'])['session_id']
        result = self.rpc('mock_set_intensity', {'session_id': session, 'level': 100,
                                                 'key': 'integration', 'ttl': 0.5})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(self.relay.state.transport.level, 1)
        self.client.close()
        self.assertEqual(self.relay.state.transport.level, 0)

    def test_autonomous_timer_zeros_without_more_requests(self):
        self.relay.state.authorize()
        session = self.client.request('status')['session_id']
        self.client.request('set_intensity', {'session_id': session, 'level': 1,
                                              'key': 'timer', 'ttl': 0.1})
        deadline = time.monotonic() + 2
        while self.relay.state.transport.level != 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.relay.state.transport.level, 0)
        self.assertEqual(self.relay.state.controller.reason, 'command_ttl')

    def test_slow_http_body_does_not_block_watchdog(self):
        self.relay.state.authorize()
        session = self.client.request('status')['session_id']
        self.client.request('set_intensity', {'session_id': session, 'level': 1,
                                              'key': 'slow-client', 'ttl': 0.1})
        with socket.create_connection(self.relay.server.server_address, timeout=2) as slow:
            request = (f'POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{self.relay.server.server_port}\r\n'
                       f'Authorization: Bearer {self.relay.api_token}\r\n'
                       'Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{')
            slow.sendall(request.encode())
            deadline = time.monotonic() + 2
            while self.relay.state.transport.level != 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.relay.state.transport.level, 0)
            self.assertEqual(self.relay.state.controller.reason, 'command_ttl')

    def test_bad_credential_origin_and_host_are_denied(self):
        for headers in ({'Authorization': 'Bearer invalid'},
                        {'Origin': 'https://example.invalid'},
                        {'Host': 'example.invalid'}):
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection(*self.relay.server.server_address, timeout=2)
                complete = {'Authorization': 'Bearer ' + self.relay.api_token,
                            'Content-Type': 'application/json', **headers}
                connection.request('POST', '/rpc', '{}', complete)
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertNotIn(self.relay.api_token, response.read().decode())
                connection.close()

    def test_invalid_payload_and_network_authorization_are_rejected(self):
        for action, arguments in (('authorize', {}), ('status', {'raw': 'hidden'}),
                                  ('set_intensity', {})):
            with self.subTest(action=action), self.assertRaises(ControlRejected):
                self.client.request(action, arguments)

    def test_second_instance_cannot_replace_runtime_credentials(self):
        before = (self.directory / '.env').read_bytes()
        with self.assertRaises(RuntimeError):
            MockRelayServer(directory=self.directory, port=0)
        self.assertEqual((self.directory / '.env').read_bytes(), before)

    def test_mcp_schema_and_absent_authorization_tool(self):
        reply = mock_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, self.client)
        tools = reply['result']['tools']
        self.assertEqual(len(tools), 6)
        self.assertTrue(all(t['name'].startswith('mock_') for t in tools))
        self.assertEqual(self.rpc('authorize')['error']['code'], -32602)
        self.assertEqual(self.rpc('mock_status', {'invalid': True})['result']['isError'], True)

    def test_offline_client_does_not_start_a_server(self):
        client = MockRelayClient(self.directory / 'missing')
        response = mock_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': 'mock_emergency_stop'}}, client)
        self.assertTrue(response['result']['isError'])
        self.assertFalse((self.directory / 'missing').exists())


class MockRelayLifecycleTests(unittest.TestCase):
    def test_shutdown_removes_credentials_and_releases_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'runtime'
            relay = MockRelayServer(directory=directory, port=0)
            relay.start()
            port = relay.server.server_port
            relay.close()
            self.assertFalse((directory / '.env').exists())
            self.assertFalse((directory / 'endpoint.json').exists())
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
            replacement = MockRelayServer(directory=directory, port=port)
            replacement.close()

    def test_occupied_port_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            directory = Path(temporary) / 'runtime'
            with self.assertRaises(OSError):
                MockRelayServer(directory=directory, port=port)
            self.assertFalse((directory / 'endpoint.json').exists())
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                pass

    def test_stdio_is_json_only_and_has_no_authorization_tool(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        result = subprocess.run([sys.executable, str(ROOT / 'mock_mcp.py')],
                                input='\n'.join(json.dumps(r) for r in requests) + '\n',
                                capture_output=True, encoding='utf-8', timeout=10, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'fun001-semantic-mock')
        self.assertEqual(len(replies[1]['result']['tools']), 6)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
