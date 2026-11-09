import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bench_stop import bench_vibration, bench_zero
from fun001_protocol import WRITE_UUID, NOTIFY_UUID


class BenchStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Tests exercise the transport state machine with synthetic devices only.
        policy = patch('bench_stop.nonzero_bench_allowed', return_value=True)
        policy.start()
        self.addCleanup(policy.stop)
        self.events = []
        self.now = 0
        self.mismatch = False
        self.lose_ack = False
        self.expire_during_connect = False
        self.fail_write_number = None
        self.delay_start = False
        case = self

        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                case.events.append('scan')
                device = SimpleNamespace(address='00:00:00:00:00:01')
                return device if predicate(device, None) else None

        class Client:
            def __init__(self, device, **kwargs):
                case.assertFalse(kwargs['pair'])
                self.services = self

            async def __aenter__(self):
                case.events.append('connect')
                if case.expire_during_connect:
                    case.now = 31
                return self

            async def __aexit__(self, *args):
                case.events.append('disconnect')

            def get_service(self, uuid):
                if case.mismatch:
                    return None
                return SimpleNamespace(characteristics=[
                    SimpleNamespace(uuid=WRITE_UUID, properties=['write']),
                    SimpleNamespace(uuid=NOTIFY_UUID, properties=['notify'])])

            async def write_gatt_char(self, characteristic, frame, response):
                case.events.append(('write', frame, response))
                count = sum(isinstance(e, tuple) for e in case.events)
                if case.delay_start and count == 2:
                    await asyncio.sleep(1)
                if case.lose_ack or case.fail_write_number == count:
                    raise OSError('sensitive-address-hidden')

            async def start_notify(self, *args):
                raise AssertionError('Bench zero attempted notification subscription')

        self.scanner = Scanner
        self.client = Client

    async def run_bench(self, confirmation='是'):
        return await bench_zero('00:00:00:00:00:01', confirmation,
                                self.scanner, self.client, lambda: self.now)

    async def test_confirmation_precedes_scanning(self):
        for answer in ('', '否', True, None, 'yes'):
            result = await self.run_bench(answer)
            self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(self.events, [])

    async def test_only_one_observed_zero_packet_and_explicit_response(self):
        result = await self.run_bench()
        writes = [event for event in self.events if isinstance(event, tuple)]
        self.assertEqual(len(writes), 1)
        _, frame, response = writes[0]
        self.assertTrue(response)
        self.assertEqual(frame[1:], bytes.fromhex('010003000111000003110000071100'))
        self.assertEqual(result['status'], 'att_ack_only')
        self.assertFalse(result['physical_stop_verified'])
        self.assertTrue(result['connection_closed'])

    async def test_unknown_outcome_has_no_replay(self):
        self.lose_ack = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(sum(isinstance(e, tuple) for e in self.events), 1)
        self.assertEqual(self.events[-1], 'disconnect')
        self.assertNotIn('sensitive-address-hidden', str(result))

    async def test_gatt_mismatch_prevents_write(self):
        self.mismatch = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'gatt_mismatch')
        self.assertEqual(result['writes_attempted'], 0)

    async def test_expired_confirmation_prevents_write(self):
        self.expire_during_connect = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'confirmation_expired')
        self.assertEqual(result['writes_attempted'], 0)

    async def test_pulse_requires_separate_scope_confirmation(self):
        result = await bench_vibration('00:00:00:00:00:01', '是', self.scanner, self.client)
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(self.events, [])

    async def test_pulse_has_zero_barrier_one_minimum_write_and_final_zero(self):
        result = await bench_vibration('00:00:00:00:00:01', '震动1', self.scanner, self.client)
        writes = [e[1] for e in self.events if isinstance(e, tuple)]
        self.assertEqual([len(frame) for frame in writes], [16, 12, 16])
        self.assertEqual(writes[1][1:], bytes.fromhex('0100020001110100021101'))
        self.assertEqual(result['nonzero_attempts'], 1)
        self.assertTrue(result['stop_ack_received'])

    async def test_uncertain_pulse_always_attempts_zero_without_replay(self):
        self.fail_write_number = 2
        result = await bench_vibration('00:00:00:00:00:01', '震动1', self.scanner, self.client)
        writes = [e[1] for e in self.events if isinstance(e, tuple)]
        self.assertEqual([len(frame) for frame in writes], [16, 12, 16])
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['stop_ack_received'])

    async def test_pulse_ack_timeout_still_attempts_zero(self):
        self.delay_start = True
        result = await bench_vibration('00:00:00:00:00:01', '震动1', self.scanner, self.client)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['stop_ack_received'])
        self.assertEqual(result['nonzero_attempts'], 1)

    async def test_final_zero_failure_remains_unknown(self):
        self.fail_write_number = 3
        result = await bench_vibration('00:00:00:00:00:01', '震动1', self.scanner, self.client)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['stop_ack_received'])

    def test_cli_cancellation_never_starts_worker(self):
        result = subprocess.run([sys.executable, str(ROOT / 'src' / 'bench_stop.py')],
                                input='否\n', capture_output=True, encoding='utf-8',
                                timeout=5, check=True)
        self.assertIn('cancelled', result.stdout)

    def test_worker_requires_confirmation(self):
        result = subprocess.run([sys.executable, str(ROOT / 'src' / 'bench_stop.py'), '--worker'],
                                input=json.dumps({'confirmation': '否'}), capture_output=True,
                                encoding='utf-8', timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'confirmation_required')
