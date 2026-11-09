import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench_stop import main, observation_signals


class UTF8MemoryStream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


class BenchWorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        policy = patch('bench_stop.nonzero_bench_allowed', return_value=True)
        policy.start()
        self.addCleanup(policy.stop)

    def test_disconnect_worker_outputs_one_json_for_all_statuses(self):
        for status in ('link_closed_observation_required', 'unknown', 'preflight_error'):
            with self.subTest(status=status):
                expected = {'status': status, 'device_state': 'unknown',
                            'connection_closed': status == 'link_closed_observation_required'}
                source = UTF8MemoryStream(json.dumps({'confirmation': '断连1'}))
                output = UTF8MemoryStream()
                with patch('sys.argv', ['bench_stop.py', '--disconnect-worker']), \
                     patch('sys.stdin', source), patch('sys.stdout', output), \
                     patch('bench_stop.load_binding', return_value='00:00:00:00:00:01'), \
                     patch('bench_stop.bench_disconnect', new=AsyncMock(return_value=expected)):
                    main()
                self.assertEqual(json.loads(output.getvalue()), expected)

    def test_zero_and_pulse_workers_preserve_unknown_details(self):
        for worker, confirmation, target in (
                ('--worker', '是', 'bench_stop.bench_zero'),
                ('--pulse-worker', '震动1', 'bench_stop.bench_vibration')):
            with self.subTest(worker=worker):
                expected = {'status': 'unknown', 'writes_attempted': 1,
                            'connection_closed': True, 'error_type': 'TimeoutError'}
                source = UTF8MemoryStream(json.dumps({'confirmation': confirmation}))
                output = UTF8MemoryStream()
                with patch('sys.argv', ['bench_stop.py', worker]), \
                     patch('sys.stdin', source), patch('sys.stdout', output), \
                     patch('bench_stop.load_binding', return_value='00:00:00:00:00:01'), \
                     patch(target, new=AsyncMock(return_value=expected)):
                    main()
                self.assertEqual(json.loads(output.getvalue()), expected)


class ObservationSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_order_and_two_second_deadline(self):
        events = []
        sound = SimpleNamespace(Beep=lambda hz, ms: events.append(('beep', hz, ms)))
        async def sleep(duration):
            events.append(('sleep', duration))
        with patch.dict('sys.modules', {'winsound': sound}), \
             patch('bench_stop.time.monotonic', side_effect=[0, 0.4]), \
             patch('bench_stop.asyncio.sleep', side_effect=sleep):
            await observation_signals()
        self.assertEqual(events, [('beep', 1100, 400), ('sleep', 1.6),
                                  ('beep', 1500, 150), ('sleep', 0.15),
                                  ('beep', 1500, 150)])
