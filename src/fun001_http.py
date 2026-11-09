"""Authenticated loopback Streamable HTTP MCP for the local FUN-001 core."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import secrets
import threading

from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

import fun001_mcp
from fun001_service import ControlClient
from local_runtime import RuntimeLock, read_secret, secure_directory, write_private

RUNTIME = Path(__file__).resolve().parents[1] / 'private' / 'http-mcp'
CREDENTIAL = 'FUN001_HTTP_TOKEN'
PORT = 8769
HOSTS = ('127.0.0.1:8769', '127.0.0.1:38473')
PING_INTERVAL = 0.5
PING_TIMEOUT = 1.5
REQUEST_CANCEL = ContextVar('fun001_http_cancel', default=None)


class RequestCancel(threading.Event):
    client_disconnected = False


class GatewayBackend:
    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()
        self.active = None
        self.closed = False

    @staticmethod
    def rejected():
        return {'content': [{'type': 'text', 'text': json.dumps({
            'outcome': 'rejected', 'reason': 'gateway_busy_or_closed',
            'retry_nonzero': False})}], 'isError': True}

    def call(self, name, arguments, cancel):
        long_run = name in fun001_mcp.RUN_TOOLS
        write = name in ('set_intensity', 'set_state')
        with self.lock:
            if self.closed or ((long_run or write) and self.active is not None):
                return self.rejected()
            if self.active is not None:
                event, session = self.active
                if ((name == 'emergency_stop' and not arguments)
                        or (name in ('stop', 'release_control')
                            and arguments == {'session_id': session})):
                    event.set()
            if long_run:
                self.active = (cancel, arguments.get('session_id'))
            if write:
                # Serialize a short update with the start of a sustained run.
                return self._dispatch(name, arguments, cancel)
        try:
            return self._dispatch(name, arguments, cancel)
        finally:
            if long_run:
                if getattr(cancel, 'client_disconnected', False):
                    self.client.close()
                with self.lock:
                    self.active = None

    def _dispatch(self, name, arguments, cancel):
        response = fun001_mcp.dispatch(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': name, 'arguments': arguments}}, self.client, cancel=cancel)
        return response.get('result', self.rejected())

    def close(self):
        with self.lock:
            self.closed = True
            if self.active is not None:
                self.active[0].set()
        self.client.close()


class GatewayGuard:
    """Checks credentials and signals cancellation on a dropped HTTP request."""
    def __init__(self, app, token, hosts):
        self.app = app
        self.authorization = ('Bearer ' + token).encode('ascii')
        self.hosts = {host.encode('ascii') for host in hosts}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = scope.get('headers', [])
        auth = [value for key, value in headers if key.lower() == b'authorization']
        hosts = [value for key, value in headers if key.lower() == b'host']
        denied = (scope['path'] != '/mcp' or len(auth) != 1 or len(hosts) != 1
                  or hosts[0] not in self.hosts
                  or any(key.lower() == b'origin' for key, value in headers))
        if denied or not secrets.compare_digest(auth[0], self.authorization):
            return await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
        if scope['method'] == 'POST':
            lengths = [value for key, value in headers if key.lower() == b'content-length']
            if (len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 5
                    or not 0 < int(lengths[0]) <= 16384
                    or any(key.lower() == b'transfer-encoding' for key, value in headers)):
                return await JSONResponse({'error': 'invalid_request'}, status_code=400)(scope, receive, send)
        cancel = RequestCancel()
        scope = dict(scope)
        scope['fun001_cancel'] = cancel
        context = REQUEST_CANCEL.set(cancel)
        messages = asyncio.Queue(maxsize=2)

        async def pump():
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    cancel.client_disconnected = True
                    cancel.set()
                await messages.put(message)
                if message['type'] == 'http.disconnect':
                    return

        reader = asyncio.create_task(pump())
        try:
            await self.app(scope, messages.get, send)
        finally:
            cancel.set()
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            REQUEST_CANCEL.reset(context)


def create_app(token, *, client=None, hosts=HOSTS):
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise ValueError('Invalid HTTP MCP credential')
    backend = GatewayBackend(ControlClient() if client is None else client)
    server = Server('fun001-http-control', version='0.1.0')

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**spec) for spec in fun001_mcp.TOOLS]

    @server.call_tool()
    async def call_tool(name, arguments):
        context = server.request_context
        cancel = context.request.scope.get('fun001_cancel') if context.request else None
        if cancel is None:
            return types.CallToolResult(content=[types.TextContent(
                type='text', text='HTTP request context unavailable')], isError=True)
        task = asyncio.create_task(asyncio.to_thread(backend.call, name, arguments, cancel))
        try:
            if name in fun001_mcp.RUN_TOOLS:
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=PING_INTERVAL)
                    if done:
                        break
                    try:
                        await asyncio.wait_for(context.session.send_ping(), timeout=PING_TIMEOUT)
                    except Exception:
                        cancel.client_disconnected = True
                        cancel.set()
                        break
            result = await task
        except asyncio.CancelledError:
            cancel.client_disconnected = True
            cancel.set()
            raise
        return types.CallToolResult(**result)

    manager = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=False,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(hosts), allowed_origins=[]))

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with manager.run():
                yield
        finally:
            await asyncio.to_thread(backend.close)

    app = Starlette(routes=[Route('/mcp', Endpoint(), methods=['POST', 'GET', 'DELETE'])],
                    lifespan=lifespan)
    return GatewayGuard(app, token, hosts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'serve'))
    args = parser.parse_args()
    secure_directory(RUNTIME)
    lock = RuntimeLock(RUNTIME, 'HTTP MCP instance already running')
    try:
        if args.action == 'init':
            if not (RUNTIME / '.env').exists():
                write_private(RUNTIME / '.env', CREDENTIAL + '=' + secrets.token_urlsafe(32) + '\n')
            read_secret(RUNTIME, CREDENTIAL)
            print('HTTP MCP private credential ready.')
            return
        token = read_secret(RUNTIME, CREDENTIAL)
        app = create_app(token)
        uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False, log_level='warning')
    finally:
        lock.close()


if __name__ == '__main__':
    main()
