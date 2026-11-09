import asyncio
from contextlib import contextmanager
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starlette.testclient import TestClient
import uvicorn
from fun001_http import create_app, GatewayGuard, GatewayBackend, REQUEST_CANCEL
from fun001_service import ControlState, ControlServer, ControlClient
from test_fun001_core import FakeLink

TOKEN = 'synthetic-test-token-for-loopback-tests-only'
HEADERS = {'Authorization': 'Bearer '+TOKEN, 'Accept': 'application/json, text/event-stream'}
TARGET = {'suction': {'level': 1, 'mode': 1}, 'vibration': {'level': 1, 'mode': 1}}


def rpc(identifier, method, params=None):
    return {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}}


class HttpMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
        cls.server = ControlServer(state=cls.state, directory=Path(cls.temp.name)/'runtime', port=0)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    def setUp(self):
        self.state.execute('emergency_stop', {}, 'test')
        self.client = ControlClient(self.server.directory)
        self.http = TestClient(create_app(TOKEN, client=self.client),
                               base_url='http://127.0.0.1:8769')
        self.http.__enter__()
        response = self.http.post('/mcp', headers=HEADERS, json=rpc(1, 'initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name':'test','version':'1'}}))
        self.headers = {**HEADERS, 'Mcp-Session-Id': response.headers['mcp-session-id']}

    def tearDown(self):
        self.http.delete('/mcp', headers=self.headers)
        self.http.__exit__(None, None, None)

    @staticmethod
    def payload(response):
        if 'event-stream' in response.headers.get('content-type', ''):
            return next(json.loads(line[5:].strip()) for line in response.text.splitlines()
                        if line.startswith('data:'))
        return response.json()

    def call(self, name, arguments=None):
        response = self.http.post('/mcp', headers=self.headers,
            json=rpc(2, 'tools/call', {'name': name, 'arguments': arguments or {}}))
        self.assertEqual(response.status_code, 200)
        return self.payload(response)['result']

    def grant(self):
        self.state.authorize(channels={name: {'cap': 1, 'modes': (1,)}
                                       for name in ('suction', 'vibration')}, allow_unverified=True)
        return self.state.session_id

    def test_missing_and_incorrect_bearer_rejected(self):
        for headers in ({}, {'Authorization': 'Bearer invalid'}):
            result = self.http.post('/mcp', headers=headers, json=rpc(1, 'tools/list'))
            self.assertEqual(result.status_code, 401)
            self.assertNotIn(TOKEN, result.text)

    def test_host_origin_and_wrong_path_rejected(self):
        for headers, path in (({**HEADERS, 'Host': 'untrusted.invalid'}, '/mcp'),
                              ({**HEADERS, 'Origin': 'https://untrusted.invalid'}, '/mcp'),
                              (HEADERS, '/other')):
            response = self.http.post(path, headers=headers, json=rpc(1, 'tools/list'))
            self.assertEqual(response.status_code, 401)

    def test_oversized_body_rejected(self):
        response = self.http.post('/mcp', headers=HEADERS, content=b'x'*16385)
        self.assertEqual(response.status_code, 400)

    def test_initialization_and_eleven_tools(self):
        response = self.http.post('/mcp', headers=HEADERS, json=rpc(1, 'initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'test', 'version': '1'}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload(response)['result']['serverInfo']['name'], 'fun001-http-control')
        response = self.http.post('/mcp', headers=self.headers, json=rpc(2, 'tools/list'))
        names = {tool['name'] for tool in self.payload(response)['result']['tools']}
        self.assertEqual(len(names), 11)
        self.assertIn('run_state_seconds', names)
        self.assertNotIn('authorize', names)

    def test_offline_status_works_without_owner_grant(self):
        self.assertFalse(self.call('project_status')['isError'])
        self.assertFalse(self.state.status()['authorization_active'])

    def test_secret_alone_cannot_authorize_nonzero(self):
        before = len(self.state.link.frames)
        result = self.call('set_state', {'session_id': 'inactive', 'state': TARGET, 'key': 'x'})
        self.assertTrue(result['isError'])
        self.assertEqual(len(self.state.link.frames), before)

    def test_http_tool_updates_fake_core_and_stop(self):
        session = self.grant()
        result = self.call('set_state', {'session_id': session, 'state': TARGET, 'key': 'x'})
        self.assertFalse(result['isError'])
        self.assertEqual(self.state.controller.desired_state['suction']['level'], 1)
        result = self.call('emergency_stop')
        self.assertFalse(result['isError'])
        self.assertFalse(self.state.link.connected)

    def test_schema_rejects_bad_input(self):
        session = self.grant()
        before = len(self.state.link.frames)
        result = self.call('set_state', {'session_id': session,
                                         'state': {'suction': {'level': -1}}, 'key': 'bad'})
        self.assertTrue(result['isError'])
        self.assertEqual(len(self.state.link.frames), before)

    def test_gateway_serializes_runs_but_allows_emergency(self):
        session = self.grant()
        backend = GatewayBackend(self.client)
        cancel = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(backend.call(
            'run_state_seconds', {'session_id': session, 'seconds': 300, 'state': TARGET}, cancel)))
        worker.start()
        try:
            deadline = time.monotonic()+3
            while self.state.controller.desired_level == 0 and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertGreater(self.state.controller.desired_level, 0)
            denied = backend.call('set_state', {'session_id': session, 'state': TARGET,
                                                'key': 'interleave'}, threading.Event())
            self.assertTrue(denied['isError'])
            backend.call('emergency_stop', {}, threading.Event())
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(self.state.link.connected)
        finally:
            cancel.set()
            worker.join(3)
            backend.close()

    @contextmanager
    def real_gateway(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        app = create_app(TOKEN, client=ControlClient(self.server.directory),
                         hosts=(f'127.0.0.1:{port}',))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic()+5
            while not server.started and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(server.started)
            yield port
        finally:
            server.should_exit = True
            thread.join(5)
            self.assertFalse(thread.is_alive())
            sock.close()

    def start_raw_run(self, port, session):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        connection.request('POST', '/mcp', json.dumps(rpc(1, 'initialize', {
            'protocolVersion':'2025-06-18','capabilities':{},
            'clientInfo':{'name':'socket-test','version':'1'}})),
            {**HEADERS, 'Content-Type':'application/json'})
        initialize = connection.getresponse()
        session_header = initialize.getheader('mcp-session-id')
        initialize.read()
        self.assertIsNotNone(session_header)
        body = json.dumps(rpc(3, 'tools/call', {'name': 'run_state_seconds', 'arguments': {
            'session_id': session, 'seconds': 300, 'state': TARGET}}))
        connection.request('POST', '/mcp', body, {
            **HEADERS, 'Content-Type':'application/json', 'Mcp-Session-Id':session_header})
        deadline = time.monotonic()+3
        while self.state.controller.desired_level == 0 and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertGreater(self.state.controller.desired_level, 0)
        return connection

    def wait_for_revoke(self):
        deadline = time.monotonic()+4
        while self.state.controller.token is not None and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertIsNone(self.state.controller.token)
        self.assertFalse(self.state.link.connected)

    def test_socket_disconnect_cancels_long_run_and_releases_grant(self):
        session = self.grant()
        with self.real_gateway() as port:
            connection = self.start_raw_run(port, session)
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
                connection.close()
                self.wait_for_revoke()
            finally:
                connection.close()

    def test_no_ping_reply_revokes_even_with_socket_open(self):
        session = self.grant()
        with self.real_gateway() as port:
            connection = self.start_raw_run(port, session)
            try:
                self.wait_for_revoke()
            finally:
                connection.close()

    def test_native_mcp_client_answers_ping_during_run(self):
        session = self.grant()
        async def run(port):
            async with httpx.AsyncClient(headers=HEADERS, timeout=10, trust_env=False) as http:
                async with streamable_http_client(
                        f'http://127.0.0.1:{port}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool('run_state_seconds', {
                            'session_id':session, 'seconds':3, 'state':TARGET})
                        self.assertFalse(result.isError)
                        value = json.loads(result.content[0].text)
                        self.assertEqual(value['zero_outcome'], 'gatt_ack')
                        status = self.state.status()
                        if not status['authorization_active']:
                            self.assertEqual(status['reason'], 'command_ttl')
                            self.assertEqual(value['source'], 'resident_command_ttl_stop')
                            self.assertFalse(status['unknown_latched'])
                        self.assertEqual(self.state.controller.desired_level, 0)
        with self.real_gateway() as port:
            asyncio.run(run(port))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_sets_request_context_cancel(self):
        observed = []
        async def app(scope, receive, send):
            await receive()
            await receive()
            observed.append(REQUEST_CANCEL.get().is_set())
        messages = iter([{'type': 'http.request', 'body': b'{}', 'more_body': False},
                         {'type': 'http.disconnect'}])
        async def receive():
            return next(messages)
        async def send(message):
            pass
        await GatewayGuard(app, TOKEN, ('127.0.0.1:8769',))({
            'type': 'http', 'path': '/mcp', 'method': 'POST',
            'headers': [(b'host', b'127.0.0.1:8769'),
                        (b'authorization', ('Bearer '+TOKEN).encode()), (b'content-length', b'2')]},
            receive, send)
        self.assertEqual(observed, [True])
        self.assertIsNone(REQUEST_CANCEL.get())


if __name__ == '__main__':
    unittest.main()
