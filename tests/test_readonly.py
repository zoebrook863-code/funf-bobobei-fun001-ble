import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('readonly', ROOT / 'readonly.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ReadOnlyTests(unittest.TestCase):
    def call(self, method, params=None):
        return app.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method,
                             'params': params or {}})

    def test_status_is_explicitly_unknown(self):
        self.assertEqual(app.status()['hardware_state'], 'unknown')
        self.assertTrue(app.status()['real_write_enabled'])
        self.assertTrue(app.status()['live_nonzero_enabled'])
        self.assertFalse(app.status()['manual_nonzero_bench_available'])

    def test_only_readonly_tools(self):
        tools = self.call('tools/list')['result']['tools']
        self.assertEqual(len(tools), 3)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
        for tool in tools:
            self.assertFalse(self.call('tools/call', {'name': tool['name']})['result']['isError'])

    def test_control_rejected(self):
        for name in ('set_intensity', 'start_session', 'raw', 'stop', 'emergency_stop'):
            self.assertEqual(self.call('tools/call', {'name': name})['error']['code'], -32602)

    def test_params_and_malformed_requests(self):
        for request in ([], None, 1, {'jsonrpc': '1.0'}, {'jsonrpc': '2.0', 'id': []}):
            self.assertEqual(app.dispatch(request)['error']['code'], -32600)
        self.assertEqual(self.call('tools/call', {'name': [], 'arguments': {}})['error']['code'], -32602)
        self.assertEqual(self.call('tools/call', {'name': 'fun001_status', 'arguments': {'secret': 'hidden'}})['error']['code'], -32602)

    def test_stdio_lifecycle(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'fun001_status'}}]
        result = subprocess.run([sys.executable, str(ROOT / 'readonly.py'), 'mcp'],
                                input='\ufeff' + '\n'.join(json.dumps(r) for r in requests) + '\n{bad}\n',
                                capture_output=True, encoding='utf-8', timeout=10, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, None])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual(replies[-1]['error']['code'], -32700)
        self.assertEqual(result.stderr, '')

    def test_powershell_forwards_pipeline(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell unavailable')
        command = "'" + json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                   'params': {'protocolVersion': '2025-06-18'}}) + "' | & '"
        command += str(ROOT / 'fun001.ps1').replace("'", "''") + "' mcp"
        # Bypass is scoped to this child process; the machine policy blocks ps1.
        result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                capture_output=True, encoding='utf-8', errors='replace',
                                timeout=15, check=True)
        reply = json.loads(result.stdout)
        self.assertEqual(reply['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
