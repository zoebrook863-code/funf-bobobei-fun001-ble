import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench_disconnect import bench_disconnect
from bench_stop import bench_vibration
from device_safety import live_nonzero_allowed, nonzero_bench_allowed


class LiveSafetyLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_stays_closed(self):
        self.assertFalse(nonzero_bench_allowed())

    async def test_live_gate_is_open_while_legacy_bench_stays_closed(self):
        self.assertTrue(live_nonzero_allowed())
        self.assertFalse(nonzero_bench_allowed())

    async def test_pulse_is_blocked_before_driver_import_or_scan(self):
        result = await bench_vibration('unused', '震动1')
        self.assertEqual(result['status'], 'blocked_by_disconnect_safety')
        self.assertEqual(result['nonzero_attempts'], 0)

    async def test_disconnect_is_blocked_before_driver_import_or_scan(self):
        result = await bench_disconnect('unused', '断连1')
        self.assertEqual(result['status'], 'blocked_by_disconnect_safety')
        self.assertEqual(result['nonzero_attempts'], 0)

    def test_cli_and_workers_cannot_override_with_confirmation(self):
        for action, confirmation in (('pulse', '震动1'), ('disconnect', '断连1'),
                                     ('--pulse-worker', '震动1'),
                                     ('--disconnect-worker', '断连1')):
            with self.subTest(action=action):
                result = subprocess.run([sys.executable, str(ROOT / 'bench_stop.py'), action],
                                        input=json.dumps({'confirmation': confirmation}),
                                        capture_output=True, encoding='utf-8', timeout=5, check=True)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['status'], 'blocked_by_disconnect_safety')
                self.assertEqual(payload['nonzero_attempts'], 0)
