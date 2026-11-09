import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import diagnostics_sentinel as sentinel
from fun001_protocol import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID, build_stop_frames

ADDRESS = 'AA:BB:CC:DD:EE:FF'
ZERO_BODY = build_stop_frames()[0][1:]


async def no_sleep(duration):
    return None


class FakeObserver:
    def __init__(self, advertisements=None, resume_after=3.3):
        self.ads = list(advertisements or [])
        self.resume_after = resume_after
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def advertisements(self):
        return list(self.ads)

    def wait_for_advertisement(self, after, timeout):
        return None if self.resume_after is None else after + self.resume_after


class FakePrimary:
    def __init__(self, connected=True, gatt_ready=True):
        self.connected = connected
        self.gatt_ready = gatt_ready
        self.started = False
        self.killed = False
        self.closed = False

    def start(self):
        self.started = True

    def find(self, name):
        if name == 'connected' and self.connected:
            return {'event': 'connected', 't': 100.0}
        if name == 'gatt_checked' and self.connected:
            return {'event': 'gatt_checked', 'write_characteristic': self.gatt_ready}
        return None

    def alive(self):
        return self.connected and not self.killed

    def kill(self):
        if not self.alive():
            return False
        self.killed = True
        return True

    def close(self):
        self.closed = True


class FakeCharacteristic:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class FakeServices:
    def __init__(self, write=True):
        notify = FakeCharacteristic(NOTIFY_UUID, ['notify'])
        characteristics = [notify]
        if write:
            characteristics.append(FakeCharacteristic(WRITE_UUID, ['write']))
        self.service = type('Service', (), {'characteristics': characteristics})()
        self.services = {1: self.service}
        self.characteristics = dict(enumerate(characteristics, 1))

    def get_service(self, uuid):
        return self.service if uuid == SERVICE_UUID else None


class FakeBackup:
    instances = []

    def __init__(self, device, pair, timeout, winrt, write=True):
        self.device = device
        self.winrt = winrt
        self.connected = False
        self.frames = []
        self.services = FakeServices(write=write)
        self.fail_write = False
        FakeBackup.instances.append(self)

    async def connect(self):
        self.connected = True

    @property
    def is_connected(self):
        return self.connected

    async def write_gatt_char(self, write, frame, response=True):
        if self.fail_write:
            raise TimeoutError('mock write timeout')
        self.frames.append(frame)

    async def disconnect(self):
        self.connected = False


class FakeDevice:
    address = ADDRESS


class FakeScan:
    @staticmethod
    async def find_device_by_filter(predicate, timeout):
        device = FakeDevice()
        return device if predicate(device, None) else None


class FakePrimaryClient:
    def __init__(self, device, pair, timeout, winrt):
        self.device = device
        self.connected = False
        self.services = FakeServices()
        self.disconnect_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


class PrimaryWorkerTests(unittest.TestCase):
    def test_primary_connects_holds_and_never_writes(self):
        events = []
        asyncio.run(sentinel.primary_worker(
            ADDRESS, 0.01, scanner_type=FakeScan, client_type=FakePrimaryClient,
            emit=lambda event, **fields: events.append({'event': event, **fields}),
            sleep=no_sleep))
        names = [event['event'] for event in events]
        self.assertEqual(names, ['connected', 'gatt_checked', 'closed'])
        checked = events[1]
        self.assertEqual(checked['writes_attempted'], 0)
        self.assertTrue(checked['write_characteristic'])
        self.assertFalse(hasattr(FakePrimaryClient, 'write_gatt_char'))


class VerdictTests(unittest.TestCase):
    def test_verdict_requires_every_shared_link_and_ack_condition(self):
        good = {'p2_connected': True, 'p2_write_characteristic': True,
                'ads_during_dual_connection': 0, 'p1_killed': True,
                'zero_ack_received': True,
                'ads_during_backup_hold_after_kill': 0,
                'backup_still_connected': True}
        self.assertTrue(sentinel.sentinel_verdict(good))
        for key, value in (('p2_connected', False),
                           ('ads_during_dual_connection', 1),
                           ('p1_killed', False),
                           ('zero_ack_received', False),
                           ('ads_during_backup_hold_after_kill', 2),
                           ('backup_still_connected', False)):
            with self.subTest(key=key):
                broken = dict(good)
                broken[key] = value
                self.assertFalse(sentinel.sentinel_verdict(broken))


class SentinelAsyncTests(unittest.TestCase):
    def setUp(self):
        FakeBackup.instances.clear()

    def run_sentinel(self, observer, primary=None, fail_write=False, write=True):
        backup = None

        def build(*args, **kwargs):
            nonlocal backup
            backup = FakeBackup(*args, **kwargs, write=write)
            backup.fail_write = fail_write
            return backup

        report = asyncio.run(sentinel.sentinel_async(
            ADDRESS, 10, observer, FakeDevice(), client_type=lambda *a, **k: build(*a, **k),
            primary=primary or FakePrimary(), sleep=no_sleep))
        return report, backup

    def test_shared_link_and_zero_ack_is_feasible(self):
        observer = FakeObserver()
        primary = FakePrimary()
        report, backup = self.run_sentinel(observer, primary)
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['sentinel_feasible'])
        self.assertTrue(report['p1_connected'])
        self.assertTrue(report['p2_connected'])
        self.assertEqual(report['ads_during_dual_connection'], 0)
        self.assertTrue(report['p1_killed'])
        self.assertTrue(report['zero_ack_received'])
        self.assertEqual(report['writes_attempted'], 1)
        self.assertEqual(report['nonzero_attempts'], 0)
        self.assertEqual(report['device_state'], 'unknown')
        self.assertFalse(report['physical_stop_verified'])
        self.assertEqual(report['p2_service_count'], 1)
        self.assertEqual(report['p2_characteristic_count'], 2)
        self.assertTrue(report['p2_has_fun001_service'])
        self.assertTrue(report['p1_gatt_checked'])
        self.assertTrue(report['p2_cached_services'])
        self.assertEqual(backup.winrt, {'use_cached_services': True})
        self.assertEqual(report['ads_during_backup_hold_after_kill'], 0)
        self.assertEqual(report['backup_close_to_resume_ms'], 3300)
        self.assertEqual(len(backup.frames), 1)
        self.assertEqual(backup.frames[0][1:], ZERO_BODY)
        self.assertTrue(observer.started)
        self.assertTrue(observer.stopped)
        self.assertTrue(primary.closed)
        self.assertFalse(backup.connected)

    def test_persistent_advertisements_abort_before_kill_or_write(self):
        observer = FakeObserver([{'event': 'advertisement', 't': 102.0,
                                  'rssi_bucket': 'mid'}])
        primary = FakePrimary()
        report, backup = self.run_sentinel(observer, primary)
        self.assertEqual(report['status'], 'shared_link_not_observed')
        self.assertFalse(report['sentinel_feasible'])
        self.assertFalse(report['p1_killed'])
        self.assertEqual(report['writes_attempted'], 0)
        self.assertEqual(backup.frames, [])
        self.assertTrue(primary.closed)

    def test_primary_gatt_must_be_ready_before_backup(self):
        report, backup = self.run_sentinel(
            FakeObserver(), primary=FakePrimary(gatt_ready=False))
        self.assertEqual(report['status'], 'p1_gatt_not_ready')
        self.assertFalse(report['p1_gatt_checked'])
        self.assertFalse(report['p2_connected'])
        self.assertFalse(report['sentinel_feasible'])
        self.assertEqual(backup, None)

    def test_missing_write_characteristic_after_kill_is_reported(self):
        report, backup = self.run_sentinel(FakeObserver(), write=False)
        self.assertEqual(report['status'], 'p2_write_characteristic_missing_after_kill')
        self.assertTrue(report['p1_killed'])
        self.assertFalse(report['p2_write_characteristic'])
        self.assertEqual(report['writes_attempted'], 0)
        self.assertFalse(report['sentinel_feasible'])
        self.assertEqual(backup.frames, [])

    def test_zero_write_unknown_is_not_feasible(self):
        report, backup = self.run_sentinel(FakeObserver(), fail_write=True)
        self.assertEqual(report['status'], 'zero_write_unknown')
        self.assertFalse(report['zero_ack_received'])
        self.assertFalse(report['sentinel_feasible'])
        self.assertEqual(report['writes_attempted'], 1)
        self.assertEqual(backup.frames, [])


if __name__ == '__main__':
    unittest.main()
