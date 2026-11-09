import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bench_disconnect import bench_disconnect
from fun001_protocol import WRITE_UUID, NOTIFY_UUID


class BenchDisconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        policy = patch('bench_disconnect.nonzero_bench_allowed', return_value=True)
        policy.start()
        self.addCleanup(policy.stop)
        self.events = []
        self.now = 0
        self.fail_start = False
        self.delay_start = False
        self.fail_disconnect = False
        self.disconnect_has_no_effect = False
        self.mismatch = False
        self.expire_after_zero = False
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
                self.is_connected = True

            async def __aenter__(self):
                case.events.append('connect')
                return self

            async def __aexit__(self, *args):
                case.events.append('context_closed')
                self.is_connected = False

            def get_service(self, uuid):
                if case.mismatch:
                    return None
                return SimpleNamespace(characteristics=[
                    SimpleNamespace(uuid=WRITE_UUID, properties=['write']),
                    SimpleNamespace(uuid=NOTIFY_UUID, properties=['notify'])])

            async def write_gatt_char(self, characteristic, frame, response):
                case.assertTrue(response)
                case.events.append(('write', frame))
                if len(frame) == 12:
                    if case.delay_start:
                        await asyncio.sleep(1)
                    if case.fail_start:
                        raise OSError('sensitive-address-hidden')
                elif case.expire_after_zero:
                    case.now = 31

            async def disconnect(self):
                case.events.append('disconnect_requested')
                if case.fail_disconnect:
                    raise OSError('sensitive-address-hidden')
                if not case.disconnect_has_no_effect:
                    self.is_connected = False

        self.scanner = Scanner
        self.client = Client

    async def run_bench(self, confirmation='断连1', on_disconnected=None):
        async def fake_sleep(duration):
            self.now += duration
        return await bench_disconnect('00:00:00:00:00:01', confirmation,
                                       self.scanner, self.client,
                                       lambda: self.now, fake_sleep, on_disconnected)

    async def test_scope_confirmation_before_scan(self):
        for value in ('是', '震动1', '', None):
            result = await self.run_bench(value)
            self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(self.events, [])

    async def test_successful_disconnect_does_not_mask_firmware_with_zero(self):
        result = await self.run_bench()
        frames = [event[1] for event in self.events if isinstance(event, tuple)]
        self.assertEqual([len(f) for f in frames], [16, 12])
        self.assertEqual(frames[1][1:], bytes.fromhex('0100020001110100021101'))
        self.assertEqual(result['status'], 'link_closed_observation_required')
        self.assertTrue(result['connection_closed'])
        self.assertFalse(result['firmware_disconnect_stop_verified'])
        self.assertTrue(result['manual_power_off_required'])

    async def test_signals_happen_only_after_confirmed_disconnect(self):
        async def signal():
            self.assertIn('disconnect_requested', self.events)
            self.events.append('signals')
        result = await self.run_bench(on_disconnected=signal)
        self.assertTrue(result['observation_signals_completed'])
        self.assertLess(self.events.index('disconnect_requested'), self.events.index('signals'))

    async def test_failed_disconnect_never_signals_observation_start(self):
        self.fail_disconnect = True
        signal = AsyncMock()
        result = await self.run_bench(on_disconnected=signal)
        signal.assert_not_awaited()
        self.assertFalse(result['observation_signals_completed'])

    async def test_uncertain_start_gets_zero_and_is_not_replayed(self):
        self.fail_start = True
        result = await self.run_bench()
        frames = [event[1] for event in self.events if isinstance(event, tuple)]
        self.assertEqual([len(f) for f in frames], [16, 12, 16])
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['recovery_zero_ack'])
        self.assertNotIn('disconnect_requested', self.events)
        self.assertNotIn('sensitive-address-hidden', str(result))

    async def test_start_timeout_gets_zero(self):
        self.delay_start = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['recovery_zero_ack'])
        self.assertFalse(result['start_ack_received'])

    async def test_failed_disconnect_gets_zero(self):
        self.fail_disconnect = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['recovery_zero_ack'])
        self.assertFalse(result['connection_closed'])

    async def test_unconfirmed_disconnect_gets_zero(self):
        self.disconnect_has_no_effect = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['recovery_zero_ack'])

    async def test_expired_confirmation_after_zero_blocks_nonzero(self):
        self.expire_after_zero = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'confirmation_expired')
        self.assertEqual(result['nonzero_attempts'], 0)

    async def test_gatt_mismatch_blocks_all_writes(self):
        self.mismatch = True
        result = await self.run_bench()
        self.assertEqual(result['status'], 'gatt_mismatch')
        self.assertFalse(any(isinstance(event, tuple) for event in self.events))

    def test_worker_does_not_accept_zero_test_confirmation(self):
        result = subprocess.run([sys.executable, str(ROOT / 'src' / 'bench_stop.py'), '--disconnect-worker'],
                                input=json.dumps({'confirmation': '是'}), capture_output=True,
                                encoding='utf-8', timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'confirmation_required')
