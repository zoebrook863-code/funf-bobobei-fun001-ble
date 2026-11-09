import io
import json
from pathlib import Path
import queue
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fun001_mcp


class FakeClient:
    def __init__(self):
        self.now = 0.0
        self.calls = []
        self.start_outcome = 'gatt_ack'
        self.zero_outcome = 'gatt_ack'
        self.raise_start = False
        self.raise_zero = False
        self.delay = 0.0
        self.status = {'outcome': 'gatt_ack'}

    def request(self, action, arguments=None):
        self.calls.append((action, arguments))
        if action == 'status':
            return self.status
        if action == 'set_intensity':
            if arguments['level']:
                self.now += self.delay
                if self.raise_start:
                    raise TimeoutError('synthetic')
                return {'outcome': self.start_outcome, 'level': 1}
            if self.raise_zero:
                raise TimeoutError('synthetic')
            return {'outcome': self.zero_outcome, 'level': 0}
        return {'outcome': 'gatt_ack'}

    def wait(self, seconds):
        self.now += seconds


class RunRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def run_for(self, seconds=1, **kwargs):
        return fun001_mcp.run_seconds(
            self.client, {'session_id': 'synthetic', 'seconds': seconds},
            clock=lambda: self.client.now, wait=self.client.wait, **kwargs)

    def test_failed_start_survives_successful_zero(self):
        for outcome in ('blocked_by_disconnect_safety', 'unknown'):
            with self.subTest(outcome=outcome):
                self.client.start_outcome = outcome
                result = self.run_for()
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['zero_outcome'], 'gatt_ack')

    def test_unknown_transport_is_not_replayed(self):
        self.client.raise_start = True
        result = self.run_for()
        self.assertEqual(result['outcome'], 'rejected_or_unknown')
        self.assertEqual([args['level'] for action, args in self.client.calls], [1, 0])

    def test_zero_error_does_not_mask_start_error(self):
        self.client.start_outcome = 'unknown'
        self.client.raise_zero = True
        result = self.run_for()
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(result['zero_outcome'], 'rejected_or_unknown')

    def test_zero_failure_cannot_report_success(self):
        self.client.zero_outcome = 'unknown'
        self.assertEqual(self.run_for()['outcome'], 'unknown')

    def test_resident_ttl_stop_is_recognized_after_cleanup_rejection(self):
        self.client.raise_zero = True
        self.client.status = {
            'outcome': 'gatt_ack', 'reason': 'command_ttl',
            'authorization_active': False, 'unknown_latched': False,
            'desired_level': 0, 'desired_state': {}}
        result = self.run_for()
        self.assertEqual(result['outcome'], 'gatt_ack')
        self.assertEqual(result['zero_outcome'], 'gatt_ack')
        self.assertEqual(result['source'], 'resident_command_ttl_stop')

    def test_ttl_status_cannot_hide_unknown_or_active_authorization(self):
        self.client.raise_zero = True
        baseline = {
            'outcome': 'gatt_ack', 'reason': 'command_ttl',
            'authorization_active': False, 'unknown_latched': False,
            'desired_level': 0}
        for changed in ({'unknown_latched': True}, {'authorization_active': True},
                        {'outcome': 'unknown'}, {'desired_level': 1},
                        {'reason': 'another_stop'}):
            with self.subTest(changed=changed):
                self.client.now = 0
                self.client.status = {**baseline, **changed}
                self.assertEqual(self.run_for()['outcome'], 'unknown')

    def test_resident_ttl_stop_cannot_hide_failed_execution(self):
        self.client.raise_start = True
        self.client.raise_zero = True
        self.client.status = {
            'outcome': 'gatt_ack', 'reason': 'command_ttl',
            'authorization_active': False, 'unknown_latched': False,
            'desired_level': 0}
        result = self.run_for()
        self.assertEqual(result['outcome'], 'rejected_or_unknown')
        self.assertEqual(result['zero_outcome'], 'gatt_ack')

    def test_fractional_duration_obeys_deadline(self):
        self.run_for(1.2)
        self.assertAlmostEqual(self.client.now, 1.2)
        levels = [args['level'] for action, args in self.client.calls
                  if action == 'set_intensity']
        self.assertEqual(levels, [1, 1, 1, 0])

    def test_request_latency_counts_toward_deadline(self):
        self.client.delay = 0.8
        self.run_for(1)
        self.assertAlmostEqual(self.client.now, 1)
        levels = [args['level'] for action, args in self.client.calls
                  if action == 'set_intensity']
        self.assertEqual(levels, [1, 0])

    def test_invalid_arguments_have_no_side_effect(self):
        for seconds in (0.1, 0.999, 301, True, '1', float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.run_for(seconds)
        with self.assertRaises(ValueError):
            fun001_mcp.run_seconds(self.client, {'session_id': '', 'seconds': 1})
        with self.assertRaises(ValueError):
            fun001_mcp.run_seconds(self.client, {'session_id': 's', 'seconds': 1, 'raw': 1})
        self.assertEqual(self.client.calls, [])

    def test_pre_cancelled_run_only_zeroes(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.run_for(cancel=cancel)['outcome'], 'cancelled')
        self.assertEqual([args['level'] for action, args in self.client.calls], [0])

    def test_dispatch_marks_blocked_write_as_error(self):
        self.client.start_outcome = 'blocked_by_disconnect_safety'
        reply = fun001_mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                    'params': {'name': 'run_seconds',
                                               'arguments': {'session_id': 's', 'seconds': 1}}},
                                   self.client)
        self.assertTrue(reply['result']['isError'])


class InputQueue:
    def __init__(self):
        self.lines = queue.Queue()

    def put(self, request):
        self.lines.put(json.dumps(request) + '\n')

    def __iter__(self):
        while True:
            line = self.lines.get(timeout=5)
            if line is None:
                return
            yield line


class ConcurrentClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.zeroed = threading.Event()

    def request(self, action, arguments=None):
        if action == 'set_intensity' and arguments['level']:
            self.started.set()
        if action in ('emergency_stop', 'stop', 'release_control'):
            self.stopped.set()
        if action == 'set_intensity' and not arguments['level']:
            self.zeroed.set()
        return super().request(action, arguments)


class StdioConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.source = InputQueue()
        self.output = io.StringIO()
        self.client = ConcurrentClient()
        self.thread = threading.Thread(target=fun001_mcp.serve,
                                       args=(self.client, self.source, self.output))
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.source.lines.put(None)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def call(self, identifier, name, arguments):
        self.source.put({'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
                         'params': {'name': name, 'arguments': arguments}})

    def test_emergency_processed_while_run_is_waiting(self):
        self.call(1, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.assertTrue(self.client.started.wait(timeout=2))
        self.call(2, 'emergency_stop', {})
        self.assertTrue(self.client.stopped.wait(timeout=2))
        self.assertTrue(self.client.zeroed.wait(timeout=2))
        # Wait for the completed run response before simulating transport EOF.
        # EOF intentionally suppresses replies to the disappearing client.
        deadline = threading.Event()
        for _ in range(100):
            if len(self.output.getvalue().splitlines()) == 2:
                break
            deadline.wait(0.01)
        self.finish()
        replies = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual({reply['id'] for reply in replies}, {1, 2})
        run = next(reply for reply in replies if reply['id'] == 1)
        self.assertEqual(json.loads(run['result']['content'][0]['text'])['outcome'], 'cancelled')
        stop_index = next(i for i, call in enumerate(self.client.calls)
                          if call[0] == 'emergency_stop')
        self.assertFalse(any(action == 'set_intensity' and args['level']
                             for action, args in self.client.calls[stop_index + 1:]))

    def test_eof_cancels_and_zeroes_pending_run(self):
        self.call(1, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.assertTrue(self.client.started.wait(timeout=2))
        self.finish()
        self.assertEqual(self.client.calls[-1][1]['level'], 0)

    def test_cancellation_notification_cancels_active_id(self):
        self.call(1, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.assertTrue(self.client.started.wait(timeout=2))
        self.source.put({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                         'params': {'requestId': 1}})
        self.assertTrue(self.client.zeroed.wait(timeout=2))
        self.finish()
        self.assertEqual(self.client.calls[-1][1]['level'], 0)
        self.assertEqual(self.output.getvalue(), '')

    def test_session_stop_processed_during_long_run(self):
        self.call(1, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.assertTrue(self.client.started.wait(timeout=2))
        self.call(2, 'stop', {'session_id': 's'})
        self.assertTrue(self.client.stopped.wait(timeout=2))
        self.assertTrue(self.client.zeroed.wait(timeout=2))

    def test_second_run_is_rejected_without_nonzero_interleaving(self):
        self.call(1, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.assertTrue(self.client.started.wait(timeout=2))
        self.call(2, 'run_seconds', {'session_id': 's', 'seconds': 300})
        self.call(3, 'emergency_stop', {})
        self.assertTrue(self.client.stopped.wait(timeout=2))
        self.finish()
        replies = [json.loads(line) for line in self.output.getvalue().splitlines()]
        second = next(reply for reply in replies if reply['id'] == 2)
        self.assertEqual(second['error']['code'], -32602)

    def test_invalid_request_does_not_start_run(self):
        self.source.put({'jsonrpc': '1.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': 'run_seconds',
                                    'arguments': {'session_id': 's', 'seconds': 300}}})
        self.finish()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(json.loads(self.output.getvalue())['error']['code'], -32600)


if __name__ == '__main__':
    unittest.main()
