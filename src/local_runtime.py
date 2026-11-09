"""Shared loopback runtime: private directories, instance lock, HTTP wiring.

Both the semantic mock and the real local control service bind only
127.0.0.1, publish a random bearer credential in a current-user-only
directory, and expose the same JSON-RPC-shaped action envelope.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading


class RequestRejected(ValueError):
    pass


def secure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
        return
    # Apply a current-user-only DACL before creating any credentials.
    literal_path = str(path.resolve()).replace("'", "''")
    script = """
$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetOwner($identity.User)
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $identity.User, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl('__PATH__', $acl)
""".replace('__PATH__', literal_path)
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                   capture_output=True, timeout=15, check=True)


class RuntimeLock:
    def __init__(self, directory, message='Instance already running'):
        self.file = (directory / 'instance.lock').open('a+b')
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError(message) from None

    def close(self):
        self.file.close()


def write_private(path, value):
    staging = path.with_name(path.name + '.tmp')
    with staging.open('w', encoding='utf-8') as output:
        if os.name != 'nt':
            os.chmod(staging, 0o600)
        output.write(value)
    staging.replace(path)


def read_endpoint(directory, scope):
    endpoint = json.loads((Path(directory) / 'endpoint.json').read_text(encoding='utf-8'))
    if (not isinstance(endpoint, dict) or endpoint.get('host') != '127.0.0.1'
            or endpoint.get('scope') != scope
            or type(endpoint.get('port')) is not int or not 1 <= endpoint['port'] <= 65535):
        raise ValueError('Invalid loopback endpoint')
    return endpoint


def read_secret(directory, variable):
    credential = (Path(directory) / '.env').read_text(encoding='utf-8').strip()
    prefix = variable + '='
    if not credential.startswith(prefix) or not credential[len(prefix):]:
        raise ValueError('Invalid loopback credential')
    return credential[len(prefix):]


class LoopbackRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(1)

    def log_message(self, *args):
        # Credentials, authorization handles and request contents are never logged.
        pass

    def respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def discard_body(self):
        """Read a bounded request body before an early reply.

        Closing with unread bytes makes Windows emit RST and can abort the
        client's read of the 403/400 response. The socket timeout bounds this.
        """
        length = self.headers.get('Content-Length', '')
        if not length.isascii() or not length.isdigit() or len(length) > 5:
            return
        remaining = int(length)
        if not 0 < remaining <= 8192:
            return
        try:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1024))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return

    def do_POST(self):
        expected_host = f'127.0.0.1:{self.server.server_port}'
        authorization = self.headers.get('Authorization', '')
        expected = 'Bearer ' + self.server.api_token
        if (self.path != '/rpc' or self.headers.get('Host') != expected_host
                or self.headers.get('Origin') is not None
                or not secrets.compare_digest(authorization.encode(), expected.encode())):
            self.discard_body()
            self.respond(403, {'error': 'Request denied'})
            return
        length = self.headers.get('Content-Length', '')
        if (not length.isascii() or not length.isdigit() or len(length) > 5
                or not 0 < int(length) <= 8192
                or self.headers.get('Transfer-Encoding') is not None
                or self.headers.get_content_type() != 'application/json'):
            self.discard_body()
            self.respond(400, {'error': 'Invalid request'})
            return
        try:
            request = json.loads(self.rfile.read(int(length)))
            if not isinstance(request, dict) or set(request) != {'action', 'arguments', 'client_id'}:
                raise RequestRejected('Invalid request')
            result = self.server.state.execute(request['action'], request['arguments'],
                                               request['client_id'])
        except (ValueError, TypeError, RecursionError, OSError):
            self.respond(400, {'error': 'Request rejected'})
            return
        self.respond(200, result)


class LoopbackServer:
    """Threaded 127.0.0.1 service with credential publication and watchdog."""

    def __init__(self, *, state, directory, port, scope, credential,
                 lock_message='Instance already running'):
        self.state = state
        self.directory = Path(directory)
        self.scope = scope
        self.credential_name = credential
        self.stop_event = threading.Event()
        self.api_token = secrets.token_urlsafe(32)
        self.server = None
        self.runtime_lock = None
        self.threads = []
        self.published = False
        self.closed = False
        secure_directory(self.directory)
        try:
            self.runtime_lock = RuntimeLock(self.directory, lock_message)
            self.server = ThreadingHTTPServer(('127.0.0.1', port), LoopbackRequestHandler)
            self.server.daemon_threads = True
            self.server.state = self.state
            self.server.api_token = self.api_token
            write_private(self.directory / '.env',
                          self.credential_name + '=' + self.api_token + '\n')
            endpoint = {'host': '127.0.0.1', 'port': self.server.server_port,
                        'scope': scope}
            write_private(self.directory / 'endpoint.json', json.dumps(endpoint))
            self.published = True
        except Exception:
            self.close()
            raise

    def _watchdog(self):
        while not self.stop_event.wait(0.02):
            try:
                self.state.tick()
            except Exception:
                # Keep the deadline checker alive; the next tick retries.
                # A dead watchdog would be worse than one skipped check.
                pass

    def start(self):
        self.threads = [threading.Thread(target=self.server.serve_forever,
                                         kwargs={'poll_interval': 0.05}, daemon=True),
                        threading.Thread(target=self._watchdog, daemon=True)]
        for thread in self.threads:
            thread.start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.state.close()
        finally:
            self.stop_event.set()
            if self.server is not None:
                if self.threads:
                    self.server.shutdown()
                self.server.server_close()
            for thread in self.threads:
                thread.join(timeout=2)
            if self.published:
                for name in ('endpoint.json', '.env'):
                    (self.directory / name).unlink(missing_ok=True)
            if self.runtime_lock is not None:
                self.runtime_lock.close()


class LoopbackClient:
    def __init__(self, *, directory, scope, credential, error_type=RequestRejected,
                 timeout=2):
        self.directory = Path(directory)
        self.scope = scope
        self.credential_name = credential
        self.error_type = error_type
        self.timeout = timeout
        self.client_id = secrets.token_hex(16)

    def request(self, action, arguments=None):
        endpoint = read_endpoint(self.directory, self.scope)
        credential = read_secret(self.directory, self.credential_name)
        body = json.dumps({'action': action,
                           'arguments': {} if arguments is None else arguments,
                           'client_id': self.client_id})
        connection = http.client.HTTPConnection('127.0.0.1', endpoint['port'],
                                                timeout=self.timeout)
        try:
            connection.request('POST', '/rpc', body,
                               {'Authorization': 'Bearer ' + credential,
                                'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = json.loads(response.read(65536))
            if response.status != 200:
                raise self.error_type('Request rejected')
            return result
        finally:
            connection.close()
