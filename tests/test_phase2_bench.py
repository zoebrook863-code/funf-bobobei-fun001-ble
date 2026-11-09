import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import phase2_bench
from fun001_protocol import (NOTIFY_UUID, SERVICE_UUID, WRITE_UUID,
                             build_control_frame, build_stop_frames)

ADDRESS = 'AA:BB:CC:DD:EE:FF'
ZERO_BODY = build_stop_frames()[0][1:]
ONE_BODY = build_control_frame('vibration', 1, mode=1, max_intensity=1)[1:]


async def no_sleep(duration):
    return None


class FakeCharacteristic:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class FakeServices:
    def __init__(self):
        self.service = type('Service', (), {'characteristics': [
            FakeCharacteristic(NOTIFY_UUID, ['notify']),
            FakeCharacteristic(WRITE_UUID, ['write'])]})()

    def get_service(self, uuid):
        return self.service if uuid == SERVICE_UUID else None


class FakeDevice:
    address = ADDRESS


class FakeScan:
    @staticmethod
    async def find_device_by_filter(predicate, timeout):
        device = FakeDevice()
        return device if predicate(device, None) else None


class FakeClient:
    instances = []

    def __init__(self, device, pair, timeout, winrt):
        self.device = device
        self.connected = False
        self.frames = []
        self.calls = 0
        self.fail_on = set()
        self.disconnect_calls = 0
        self.services = FakeServices()
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def write_gatt_char(self, write, frame, response=True):
        self.calls += 1
        if self.calls in self.fail_on:
            raise TimeoutError('mock write timeout')
        self.frames.append(frame)

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    @property
    def is_connected(self):
        return self.connected


class Phase2WorkerTests(unittest.TestCase):
    def run_worker(self, allowed=True, fail_on=()):
        FakeClient.instances.clear()
        events = []

        def client_type(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            client.fail_on = set(fail_on)
            return client

        asyncio.run(phase2_bench.phase2_worker(
            ADDRESS, hold=0, observe=0, scanner_type=FakeScan,
            client_type=client_type, sleep=no_sleep,
            emit=lambda event, **fields: events.append({'event': event, **fields}),
            allowed=lambda: allowed))
        return events, FakeClient.instances

    def test_exactly_one_pulse_with_zero_barrier_and_no_zero_after_disconnect(self):
        events, clients = self.run_worker()
        self.assertEqual(len(clients), 1)
        client = clients[0]
        self.assertEqual([frame[1:] for frame in client.frames],
                         [ZERO_BODY, ONE_BODY])
        self.assertEqual(client.disconnect_calls, 1)
        names = [event['event'] for event in events]
        self.assertEqual(names, ['connected', 'zero_barrier_ack', 'pulse_ack',
                                 'disconnecting', 'disconnected', 'worker_done'])
        self.assertEqual(events[2]['nonzero_attempts'], 1)
        self.assertEqual(events[2]['writes_attempted'], 2)
        self.assertNotIn('recovery_zero_ack', names)

    def test_unapproved_gate_refuses_before_any_connection(self):
        events, clients = self.run_worker(allowed=False)
        self.assertEqual(clients, [])
        self.assertEqual([event['event'] for event in events], ['error'])
        self.assertEqual(events[0]['error_type'], 'phase2_not_approved')
        self.assertEqual(events[0]['nonzero_attempts'], 0)

    def test_pulse_write_unknown_attempts_one_recovery_zero_and_never_replays(self):
        events, clients = self.run_worker(fail_on={2})
        client = clients[0]
        self.assertEqual([frame[1:] for frame in client.frames],
                         [ZERO_BODY, ZERO_BODY])
        names = [event['event'] for event in events]
        self.assertNotIn('pulse_ack', names)
        self.assertIn('error', names)
        self.assertIn('recovery_zero_ack', names)
        self.assertEqual(sum(frame[1:] == ONE_BODY for frame in client.frames), 0)
        self.assertEqual(client.disconnect_calls, 1)


if __name__ == '__main__':
    unittest.main()
