import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics_phase1 as diagnostics
from diagnostics_phase1 import (MeasurementSession, WorkerResult, summarize)
from fun001_protocol import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID

ADDRESS = 'AA:BB:CC:DD:EE:FF'


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, duration):
        self.now += duration
        return self.now


class FakeSleep:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, duration):
        self.calls.append(duration)
        self.clock.advance(duration)


class FakeObserver:
    def __init__(self, advertisements=None):
        self.ads = list(advertisements or [])
        self.started = False
        self.stopped = False
        self.waits = []

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True

    def advertisements(self):
        return list(self.ads)

    def wait_for_advertisement(self, after, timeout):
        self.waits.append((after, timeout))
        return after + 0.5


class FakeRunner:
    def __init__(self, clock, *, error_type=None):
        self.clock = clock
        self.error_type = error_type
        self.calls = []

    def run(self, mode, hold, kill_after=None):
        self.calls.append((mode, hold, kill_after))
        now = self.clock.now
        result = WorkerResult(mode, now, now + 0.1, now + 0.2, True, False,
                              self.error_type)
        self.clock.advance(0.3)
        return result


class FakeDevice:
    def __init__(self, address=ADDRESS):
        self.address = address


class FakeAdvertisement:
    def __init__(self, rssi):
        self.rssi = rssi


class FakeScanScanner:
    def __init__(self, detection_callback=None):
        self.callback = detection_callback
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True
        self.callback(FakeDevice(), FakeAdvertisement(-61))
        self.callback(FakeDevice('11:22:33:44:55:66'), FakeAdvertisement(-40))
        self.callback(FakeDevice(), FakeAdvertisement(-88))

    async def stop(self):
        self.stopped = True


class FakeCharacteristic:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class FakeService:
    def __init__(self, characteristics):
        self.characteristics = characteristics


class FakeServices:
    def __init__(self, write=True):
        characteristics = [FakeCharacteristic(NOTIFY_UUID, ['notify'])]
        if write:
            characteristics.append(FakeCharacteristic(WRITE_UUID, ['write']))
        self.service = FakeService(characteristics)

    def get_service(self, uuid):
        return self.service if uuid == SERVICE_UUID else None


class FakeClient:
    instances = []

    def __init__(self, device, pair, timeout, winrt):
        self.device = device
        self.connected = False
        self.disconnect_calls = 0
        self.services = FakeServices()
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    @property
    def is_connected(self):
        return self.connected


class FakeScan:
    @staticmethod
    async def find_device_by_filter(predicate, timeout):
        device = FakeDevice()
        if not predicate(device, None):
            return None
        return device


class ExitCalled(Exception):
    pass


class ObservationWorkerTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(diagnostics.rssi_bucket(-50), 'near')
        self.assertEqual(diagnostics.rssi_bucket(-70), 'mid')
        self.assertEqual(diagnostics.rssi_bucket(-80), 'far')
        self.assertEqual(diagnostics.rssi_bucket(-95), 'edge')
        self.assertEqual(diagnostics.rssi_bucket(None), 'unknown')

    def test_passive_observer_logs_raw_address_only_in_private_file(self):
        emitted = []
        times = iter([100.0, 100.5, 101.0, 102.0])

        def emit(event, **fields):
            emitted.append({'event': event, **fields})

        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / 'observer.jsonl'
            asyncio.run(diagnostics.observe_worker(
                ADDRESS, 0.01, raw_path, clock=lambda: next(times),
                scanner_type=FakeScanScanner, emit=emit,
                secure=lambda path: None))
            raw = [json.loads(line) for line in raw_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(raw), 2)
        self.assertTrue(all(record['address'] == ADDRESS for record in raw))
        sanitized = [event for event in emitted if event['event'] == 'advertisement']
        self.assertEqual(len(sanitized), 2)
        self.assertEqual([event['rssi_bucket'] for event in sanitized], ['mid', 'far'])
        self.assertIsNone(sanitized[0]['interval_ms'])
        self.assertEqual(sanitized[1]['interval_ms'], 500.0)
        self.assertNotIn('address', sanitized[0])
        self.assertNotIn(ADDRESS, json.dumps(emitted))
        self.assertIn('observer_started', [event['event'] for event in emitted])
        self.assertEqual(emitted[-1]['event'], 'observer_stopped')


class ObservationSummaryTests(unittest.TestCase):
    def test_empty_is_no_advertising(self):
        report = diagnostics.observation_summary([], 60)
        self.assertEqual(report['status'], 'no_advertising')
        self.assertFalse(report['continuous'])
        self.assertEqual(report['advertisement_count'], 0)

    def test_continuous_baseline_and_buckets(self):
        report = diagnostics.observation_summary([
            {'event': 'advertisement', 't': 1.0, 'interval_ms': None,
             'rssi_bucket': 'mid'},
            {'event': 'advertisement', 't': 1.1, 'interval_ms': 100,
             'rssi_bucket': 'mid'},
            {'event': 'advertisement', 't': 1.3, 'interval_ms': 200,
             'rssi_bucket': 'near'}], 60)
        self.assertEqual(report['status'], 'observed')
        self.assertEqual(report['advertisement_count'], 3)
        self.assertEqual(report['median_interval_ms'], 150)
        self.assertEqual(report['max_interval_ms'], 200)
        self.assertEqual(report['rssi_buckets'], {'mid': 2, 'near': 1})
        self.assertTrue(report['continuous'])
        self.assertNotIn('address', json.dumps(report))

    def test_large_gap_is_not_continuous(self):
        report = diagnostics.observation_summary([
            {'event': 'advertisement', 't': 1.0, 'interval_ms': None,
             'rssi_bucket': 'far'},
            {'event': 'advertisement', 't': 7.0, 'interval_ms': 6000,
             'rssi_bucket': 'far'}], 60)
        self.assertFalse(report['continuous'])


class CloseWorkerTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()
        self.emitted = []

    def emit(self, event, **fields):
        self.emitted.append({'event': event, **fields})

    def run_mode(self, mode, **kwargs):
        sleeps = []

        async def sleep(duration):
            sleeps.append(duration)

        asyncio.run(diagnostics.close_worker(
            ADDRESS, mode, 0.01, scanner_type=FakeScan, client_type=FakeClient,
            emit=self.emit, sleep=sleep,
            exit_process=lambda code: (_ for _ in ()).throw(ExitCalled(code)),
            release_sleep=0, **kwargs))
        return sleeps

    def test_mode_a_disconnects_then_releases_references(self):
        self.run_mode('A')
        client = FakeClient.instances[0]
        self.assertEqual(client.disconnect_calls, 1)
        names = [event['event'] for event in self.emitted]
        self.assertLess(names.index('closed'), names.index('references_released'))
        self.assertEqual(names[-1], 'worker_done')
        self.assertTrue(all(event.get('writes_attempted', 0) == 0 for event in self.emitted))

    def test_mode_b_keeps_references_alive(self):
        self.run_mode('B')
        names = [event['event'] for event in self.emitted]
        self.assertNotIn('references_released', names)
        self.assertEqual(names[-1], 'worker_done')
        self.assertEqual(FakeClient.instances[0].disconnect_calls, 1)

    def test_mode_c_disconnects_then_exits_immediately(self):
        with self.assertRaises(ExitCalled):
            self.run_mode('C')
        self.assertEqual(FakeClient.instances[0].disconnect_calls, 1)
        self.assertEqual(self.emitted[-1]['event'], 'closed')

    def test_mode_d_exits_without_disconnect(self):
        with self.assertRaises(ExitCalled):
            self.run_mode('D')
        self.assertEqual(FakeClient.instances[0].disconnect_calls, 0)
        self.assertEqual(self.emitted[-1]['via'], 'process_exit_without_disconnect')

    def test_missing_write_characteristic_is_reported_not_written(self):
        class NoWriteClient(FakeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.services = FakeServices(write=False)

        with self.assertRaises(ExitCalled):
            asyncio.run(diagnostics.close_worker(
                ADDRESS, 'C', 0.01, scanner_type=FakeScan, client_type=NoWriteClient,
                emit=self.emit, sleep=_async_noop,
                exit_process=lambda code: (_ for _ in ()).throw(ExitCalled(code)),
                release_sleep=0))
        checked = [event for event in self.emitted if event['event'] == 'gatt_checked']
        self.assertEqual(checked[0]['write_characteristic'], False)
        self.assertEqual(checked[0]['writes_attempted'], 0)


async def _async_noop(duration=None):
    return None


class MeasurementSessionTests(unittest.TestCase):
    def session(self, observer, runner, clock, sleep):
        return MeasurementSession(observer=observer, runner=runner, clock=clock,
                                  sleep=sleep, settle=10, observation_cap=90, hold=20)

    def test_baseline_requires_continuous_advertising(self):
        clock = FakeClock()
        observer = FakeObserver()
        runner = FakeRunner(clock)
        report = self.session(observer, runner, clock, FakeSleep(clock)).run(
            baseline_seconds=60)
        self.assertEqual(report['status'], 'no_advertising')
        self.assertEqual(runner.calls, [])
        self.assertTrue(observer.stopped)

    def test_calibration_fails_when_advertisements_persist(self):
        clock = FakeClock()
        observer = FakeObserver([
            {'event': 'advertisement', 't': 1001.0, 'interval_ms': 100},
            {'event': 'advertisement', 't': 1001.2, 'interval_ms': 200},
            {'event': 'advertisement', 't': 1060.05, 'interval_ms': 100}])
        runner = FakeRunner(clock)
        report = self.session(observer, runner, clock, FakeSleep(clock)).run(
            baseline_seconds=60)
        self.assertEqual(report['status'], 'calibration_failed')
        self.assertEqual(report['calibration']['advertisements_while_connected'], 1)
        self.assertEqual(runner.calls, [('C', 20, None)])
        self.assertTrue(observer.stopped)

    def test_successful_run_records_resume_and_summary(self):
        clock = FakeClock()
        observer = FakeObserver([
            {'event': 'advertisement', 't': 1001.0, 'interval_ms': 100},
            {'event': 'advertisement', 't': 1001.5, 'interval_ms': 500}])
        runner = FakeRunner(clock)
        sleep = FakeSleep(clock)
        report = self.session(observer, runner, clock, sleep).run(
            modes=('A',), repeats=2, baseline_seconds=60)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['calibration']['status'], 'calibration_ok')
        self.assertEqual(len(report['runs']), 2)
        for run in report['runs']:
            self.assertEqual(run['resume'], 'observed')
            self.assertEqual(run['reference'], 'close')
            self.assertEqual(run['reference_to_resume_ms'], 500)
            self.assertEqual(run['exit_to_resume_ms'], 400)
            self.assertEqual(run['writes_attempted'], 0)
        self.assertEqual(report['summary']['reading_hint'], 'all_within_1s')
        self.assertEqual(report['summary']['per_mode']['A']['observed'], 2)
        self.assertEqual([call[0] for call in runner.calls], ['C', 'A', 'A'])
        self.assertTrue(observer.stopped)

    def test_mode_e_kills_worker_and_measures_from_exit(self):
        clock = FakeClock()
        observer = FakeObserver([
            {'event': 'advertisement', 't': 1001.0, 'interval_ms': 100},
            {'event': 'advertisement', 't': 1001.5, 'interval_ms': 500}])
        runner = FakeRunner(clock)
        report = self.session(observer, runner, clock, FakeSleep(clock)).run(
            modes=('E',), repeats=1, baseline_seconds=60)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(runner.calls[-1], ('E', 20, 2))
        run = report['runs'][0]
        self.assertEqual(run['reference'], 'exit')
        self.assertEqual(run['reference_to_resume_ms'], 500)
        self.assertEqual(run['exit_to_resume_ms'], 500)

    def test_connection_failure_is_recorded_without_resume(self):
        clock = FakeClock()
        observer = FakeObserver([
            {'event': 'advertisement', 't': 1001.0, 'interval_ms': 100},
            {'event': 'advertisement', 't': 1001.5, 'interval_ms': 500}])
        runner = FakeRunner(clock, error_type='bound_device_not_found')
        report = self.session(observer, runner, clock, FakeSleep(clock)).run(
            modes=('A',), repeats=1, baseline_seconds=60)
        self.assertEqual(report['calibration']['status'], 'calibration_error')
        self.assertEqual(report['status'], 'calibration_failed')


class SummaryTests(unittest.TestCase):
    def test_hints(self):
        def runs(mode, values):
            return [{'mode': mode, 'resume': 'observed', 'reference_to_resume_ms': value}
                    for value in values]

        fast = runs('A', [400, 500]) + runs('B', [600]) + runs('C', [300])
        self.assertEqual(summarize(fast)['reading_hint'], 'all_within_1s')
        slow = runs('A', [3000]) + runs('B', [4000]) + runs('C', [5000])
        self.assertEqual(summarize(slow)['reading_hint'], 'all_slow_over_2s')
        reference = runs('A', [500]) + runs('B', [3000]) + runs('C', [600])
        self.assertEqual(summarize(reference)['reading_hint'],
                         'reference_release_delays_disconnect')
        mixed = runs('A', [500]) + runs('B', [1500])
        self.assertEqual(summarize(mixed)['reading_hint'], 'mixed')


if __name__ == '__main__':
    unittest.main()
