from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fun001_core import (ES_CONTINUOUS, ES_SYSTEM_REQUIRED, MAX_GRANT_SECONDS,
                         ControlRejected, LinkUnavailable, RealSafetyController,
                         SleepGuard, UnknownExecution)
from fun001_protocol import build_control_frame, build_stop_frames


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeSleepGuard:
    def __init__(self):
        self.calls = []
        self.engaged = False

    def engage(self):
        if not self.engaged:
            self.calls.append('engage')
            self.engaged = True

    def disengage(self):
        if self.engaged:
            self.calls.append('disengage')
            self.engaged = False


class FakeLink:
    def __init__(self):
        self.connected = False
        self.frames = []
        self.connect_count = 0
        self.disconnect_count = 0
        self.close_count = 0
        self.fail_connect = None
        self.fail_writes = []
        self.lost = False

    def ensure_connected(self):
        self.connect_count += 1
        if self.fail_connect is not None:
            raise LinkUnavailable(self.fail_connect)
        self.connected = True

    def write(self, frame):
        if self.fail_writes:
            kind = self.fail_writes.pop(0)
            raise UnknownExecution('mock unknown') if kind == 'unknown' else LinkUnavailable('mock unavailable')
        if not self.connected:
            raise LinkUnavailable('link_not_open')
        self.frames.append(frame)

    def disconnect(self):
        self.connected = False
        self.disconnect_count += 1

    def take_link_lost(self):
        lost, self.lost = self.lost, False
        return lost

    def close(self):
        self.connected = False
        self.close_count += 1


ZERO = build_stop_frames()[0]
ONE = build_control_frame('vibration', 1, mode=1, max_intensity=1)
ZERO_BODY = ZERO[1:]
ONE_BODY = ONE[1:]


class SafetyCoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = FakeClock(self.now)
        self.link = FakeLink()
        self.sleep = FakeSleepGuard()
        self.controller = None

    def build(self, allowed=False):
        self.controller = RealSafetyController(
            self.link, self.clock, self.sleep, lambda: allowed)
        return self.controller

    def advance(self, duration):
        self.now += duration
        self.clock.now = self.now

    def authorize(self, allowed=False, **kwargs):
        controller = self.build(allowed)
        self.token = controller.authorize(duration=kwargs.pop('duration', 30), **kwargs)
        self.link.frames.clear()
        return controller

    def send(self, level=1, key='request', **kwargs):
        return self.controller.set_level(self.token, level=level, key=key, **kwargs)

    def test_fresh_service_is_idle_not_unknown(self):
        controller = self.build()
        status = controller.status()
        self.assertEqual(status['outcome'], 'idle')
        self.assertFalse(status['unknown_latched'])
        self.assertFalse(status['manual_power_off_required'])
        self.assertFalse(status['authorization_active'])

    def test_authorize_runs_zero_barrier_and_reports_unknown_device_state(self):
        controller = self.authorize()
        self.assertEqual(self.link.frames, [])
        self.assertEqual(self.link.connect_count, 1)
        self.assertEqual(self.link.disconnect_count, 0)
        self.assertEqual(self.link.frames, [])
        status = controller.status()
        self.assertTrue(status['authorization_active'])
        self.assertTrue(status['link_connected'])
        self.assertEqual(status['application_gatt_session'], 'open')
        self.assertFalse(status['physical_radio_disconnect_verified'])
        self.assertFalse(status['physical_stop_verified'])
        self.assertFalse(status['device_confirmed'])
        self.assertEqual(status['device_state'], 'unknown')
        self.assertFalse(status['real_write_enabled'])
        self.assertEqual(status['desired_level'], 0)
        self.assertEqual(status['outcome'], 'gatt_ack')
        self.assertFalse(status['unknown_latched'])
        self.assertFalse(status['manual_power_off_required'])
        self.assertEqual(self.sleep.calls, ['engage'])

    def test_zero_barrier_frame_is_the_observed_stop_frame(self):
        controller = self.build()
        controller.authorize()
        self.assertEqual([frame[1:] for frame in self.link.frames], [ZERO_BODY])

    def test_owner_cap_and_duration_are_fixed_to_verified_envelope(self):
        controller = self.build()
        with self.assertRaises(ControlRejected):
            controller.authorize(cap=2)
        with self.assertRaises(ControlRejected):
            controller.authorize(duration=MAX_GRANT_SECONDS + 1)
        self.assertFalse(self.link.connected)

    def test_zero_barrier_failure_rejects_and_closes(self):
        controller = self.build()
        self.link.fail_writes = ['unknown']
        with self.assertRaises(ControlRejected):
            controller.authorize()
        self.assertIsNone(controller.token)
        self.assertFalse(self.link.connected)
        self.assertEqual(controller.result, 'unknown')
        self.assertEqual(controller.reason, 'zero_barrier_failed:UnknownExecution')

    def test_connect_failure_rejects(self):
        controller = self.build()
        self.link.fail_connect = 'bound_device_not_found'
        with self.assertRaises(ControlRejected):
            controller.authorize()
        self.assertIsNone(controller.token)

    def test_nonzero_is_blocked_by_default_without_writing(self):
        self.authorize(allowed=False)
        result = self.send()
        self.assertEqual(result['outcome'], 'blocked_by_disconnect_safety')
        self.assertIsNone(result['level'])
        self.assertEqual(self.link.frames, [])
        self.assertTrue(self.link.connected)

    def test_allowed_nonzero_uses_verified_envelope_and_clamps(self):
        self.authorize(allowed=True)
        self.assertEqual(self.send(1, 'one')['outcome'], 'gatt_ack')
        self.assertEqual([frame[1:] for frame in self.link.frames], [ONE_BODY])
        self.assertEqual(self.send(100, 'hundred')['level'], 1)
        self.assertEqual([frame[1:] for frame in self.link.frames], [ONE_BODY, ONE_BODY])
        status = self.controller.status()
        self.assertTrue(status['real_write_enabled'])
        self.assertEqual(status['desired_level'], 1)
        self.assertEqual(status['outcome'], 'gatt_ack')

    def test_zero_command_uses_observed_stop_frame(self):
        self.authorize(allowed=True)
        result = self.send(0, 'zero')
        self.assertEqual(result['outcome'], 'gatt_ack')
        self.assertEqual([frame[1:] for frame in self.link.frames], [ZERO_BODY])
        self.assertIsNone(self.controller.command_deadline)

    def test_stop_writes_zero_then_disconnects_and_revokes(self):
        self.authorize(allowed=True)
        self.send()
        self.controller.stop(self.token)
        self.assertEqual([frame[1:] for frame in self.link.frames], [ONE_BODY, ZERO_BODY])
        self.assertFalse(self.link.connected)
        self.assertIsNone(self.controller.token)
        self.assertEqual(self.controller.reason, 'stop_and_release')
        with self.assertRaises(ControlRejected):
            self.send(key='late')
        self.assertEqual(self.sleep.calls[-1], 'disengage')

    def test_ttl_zeroes_and_releases(self):
        self.authorize(allowed=True)
        self.send(ttl=0.25)
        self.advance(0.25)
        self.controller.tick()
        self.assertEqual([frame[1:] for frame in self.link.frames], [ONE_BODY, ZERO_BODY])
        self.assertFalse(self.link.connected)
        self.assertIsNone(self.controller.token)
        self.assertEqual(self.controller.reason, 'command_ttl')

    def test_heartbeat_timeout_and_lock_expiry(self):
        self.authorize(allowed=True)
        self.advance(2)
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'heartbeat_timeout')
        self.assertFalse(self.link.connected)
        self.authorize(allowed=True)
        self.advance(1.5)
        self.controller.heartbeat(self.token)
        self.advance(1.5)
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'lock_expired')

    def test_unknown_nonzero_is_never_replayed(self):
        self.authorize(allowed=True)
        self.link.fail_writes = ['unknown']
        result = self.send()
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(sum(frame[1:] == ONE_BODY for frame in self.link.frames), 0)
        self.assertIsNone(self.controller.token)
        self.assertFalse(self.link.connected)
        with self.assertRaises(ControlRejected):
            self.send(key='retry')
        self.assertEqual([frame[1:] for frame in self.link.frames], [ZERO_BODY])
        status = self.controller.status()
        self.assertEqual(status['outcome'], 'unknown')
        self.assertTrue(status['unknown_latched'])
        self.assertTrue(status['manual_power_off_required'])
        self.token = self.controller.authorize()
        status = self.controller.status()
        self.assertEqual(status['outcome'], 'gatt_ack')
        self.assertFalse(status['unknown_latched'])
        self.assertFalse(status['manual_power_off_required'])

    def test_unavailable_link_marks_unknown_without_attempting_a_write(self):
        self.authorize(allowed=True)
        self.link.connected = False
        result = self.send()
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIsNone(self.controller.token)
        self.assertEqual(self.link.frames, [])

    def test_emergency_stop_preempts_rate_limit(self):
        self.authorize(allowed=True)
        for index in range(5):
            self.send(key=str(index))
        with self.assertRaises(ControlRejected):
            self.send(key='rate_limited')
        self.controller.emergency_stop()
        self.assertFalse(self.link.connected)
        self.assertIsNone(self.controller.token)
        self.assertEqual(self.controller.reason, 'local_emergency')

    def test_planned_stop_does_not_report_link_loss(self):
        self.authorize(allowed=True)
        self.send()
        self.controller.stop(self.token)
        self.assertFalse(self.link.take_link_lost())
        self.assertEqual(self.controller.reason, 'stop_and_release')

    def test_unexpected_link_loss_zero_is_impossible_and_reported_unknown(self):
        self.authorize(allowed=True)
        self.send()
        frames = len(self.link.frames)
        self.link.connected = False
        self.link.lost = True
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'ble_disconnected')
        self.assertEqual(self.controller.result, 'unknown')
        self.assertTrue(self.controller.status()['unknown_latched'])
        self.assertTrue(self.controller.status()['manual_power_off_required'])
        self.assertEqual(len(self.link.frames), frames)
        self.assertIsNone(self.controller.token)

    def test_client_disconnect_zeroes_and_releases(self):
        self.authorize(allowed=True)
        self.send()
        self.controller.client_disconnected()
        self.assertEqual([frame[1:] for frame in self.link.frames], [ONE_BODY, ZERO_BODY])
        self.assertFalse(self.link.connected)
        self.assertEqual(self.controller.reason, 'client_disconnected')

    def test_duplicate_and_changed_key(self):
        self.authorize(allowed=True)
        first = self.send()
        count = len(self.link.frames)
        self.assertEqual(self.send(), first)
        self.assertEqual(len(self.link.frames), count)
        with self.assertRaises(ControlRejected):
            self.send(level=0)

    def test_concurrent_duplicate_is_single_write(self):
        self.authorize(allowed=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(), range(8)))
        self.assertTrue(all(result['outcome'] == 'gatt_ack' for result in results))
        self.assertEqual(sum(frame[1:] == ONE_BODY for frame in self.link.frames), 1)

    def test_close_stops_and_closes_link(self):
        self.authorize(allowed=True)
        self.controller.close()
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.close_count, 1)
        with self.assertRaises(ControlRejected):
            self.controller.authorize()

    def test_invalid_inputs_do_not_write(self):
        self.authorize(allowed=True)
        for kwargs in ({'level': True}, {'level': -1}, {'level': 101},
                       {'level': 1.5}, {'ttl': 0}, {'ttl': 2},
                       {'ttl': float('nan')}, {'key': ''}, {'device': 'other'},
                       {'mode': 'unknown'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ControlRejected):
                self.send(**kwargs)
        self.assertEqual(self.link.frames, [])

    def test_token_from_previous_authorization_is_rejected(self):
        self.authorize(allowed=True)
        old_token = self.token
        self.controller.stop(old_token)
        self.authorize(allowed=True)
        with self.assertRaises(ControlRejected):
            self.controller.set_level(old_token, level=1, key='old')


class SleepGuardTests(unittest.TestCase):
    def test_state_is_per_thread_and_not_reapplied(self):
        calls = []
        guard = SleepGuard(calls.append)
        guard.engage()
        guard.engage()
        self.assertEqual(calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED])
        guard.disengage()
        guard.disengage()
        self.assertEqual(calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS])

        guard.engage()
        calls.clear()

        def worker():
            guard.engage()
            guard.disengage()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS])


if __name__ == '__main__':
    unittest.main()
