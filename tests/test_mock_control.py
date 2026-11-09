from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mock_control import ControlRejected, MockSafetyController, MockTransport


class MockControlTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.transport = MockTransport()
        self.controller = MockSafetyController(self.transport, lambda: self.now)
        self.token = self.controller.authorize()

    def send(self, level=1, key='request', **kwargs):
        return self.controller.set_level(self.token, level=level, key=key, **kwargs)

    def test_minimum_and_cap_clamping(self):
        self.assertEqual(self.send()['level'], 1)
        self.assertEqual(self.send(100, 'clamped')['level'], 1)

    def test_invalid_intensity_and_unknown_mode(self):
        for value in (-1, 101, True, 1.5, float('nan'), '1', {}, None):
            with self.assertRaises(ControlRejected):
                self.send(value)
        with self.assertRaises(ControlRejected):
            self.send(mode='unknown')

    def test_input_and_authorization_scopes(self):
        for kwargs in ({'device': 'other'}, {'key': ''}, {'ttl': True},
                       {'ttl': 0}, {'ttl': 2}, {'ttl': float('inf')},
                       {'ttl': float('nan')}, {'ttl': 10 ** 400}):
            with self.assertRaises(ControlRejected):
                self.send(**kwargs)
        with self.assertRaises(ControlRejected):
            self.controller.set_level('wrong', level=1, key='wrong')
        with self.assertRaises(ControlRejected):
            self.controller.set_level('无效授权', level=1, key='wrong')
        with self.assertRaises(ControlRejected):
            self.controller.authorize()

    def test_duplicate_does_not_write_or_extend_ttl(self):
        result = self.send()
        count = len(self.transport.writes)
        self.now = 0.5
        self.assertEqual(self.send(), result)
        self.assertEqual(len(self.transport.writes), count)
        self.now = 1
        self.controller.tick()
        self.assertEqual(self.transport.level, 0)
        self.assertEqual(self.controller.reason, 'command_ttl')
        with self.assertRaises(ControlRejected):
            self.send()

    def test_changed_input_cannot_reuse_key(self):
        self.send()
        with self.assertRaises(ControlRejected):
            self.send(0)

    def test_network_disconnect_zeros_and_revokes(self):
        self.send()
        self.controller.network_disconnected()
        self.assertEqual(self.transport.level, 0)
        with self.assertRaises(ControlRejected):
            self.send(key='new')

    def test_heartbeat_timeout(self):
        self.now = 2
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'heartbeat_timeout')
        self.assertIsNone(self.controller.token)

    def test_lock_expires_even_with_heartbeat(self):
        self.now = 1.5
        self.controller.heartbeat(self.token)
        self.now = 3
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'lock_expired')
        self.assertEqual(self.transport.level, 0)

    def test_session_expiry(self):
        self.controller.stop(self.token)
        self.token = self.controller.authorize(duration=0.5)
        self.send()
        self.now = 0.5
        self.controller.tick()
        self.assertEqual(self.controller.reason, 'session_expired')
        self.assertEqual(self.transport.level, 0)

    def test_emergency_preempts_rate_limit_and_invalidates(self):
        for i in range(5):
            self.send(key=str(i))
        with self.assertRaises(ControlRejected):
            self.send(key='rate_limited')
        self.controller.emergency_stop()
        self.assertEqual(self.transport.level, 0)
        with self.assertRaises(ControlRejected):
            self.send(key='after_emergency')

    def test_unknown_nonzero_outcome_is_not_replayed(self):
        self.transport.fail_next_nonzero = True
        self.assertEqual(self.send()['outcome'], 'unknown')
        with self.assertRaises(ControlRejected):
            self.send()
        self.assertEqual(self.transport.writes.count(1), 1)
        self.assertEqual(self.transport.level, 0)

    def test_ble_disconnect_is_unknown_until_reconnect_zero_barrier(self):
        self.send()
        self.controller.ble_disconnected()
        self.assertEqual(self.controller.desired_level, 0)
        self.assertEqual(self.controller.result, 'unknown')
        self.assertEqual(self.transport.level, 1)
        self.controller.reconnect()
        self.assertEqual(self.transport.level, 0)
        self.assertIsNone(self.controller.token)

    def test_concurrent_duplicate_is_single_write(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(), range(8)))
        self.assertTrue(all(r['outcome'] == 'mock_ack' for r in results))
        self.assertEqual(self.transport.writes.count(1), 1)

    def test_release_and_new_authorization_start_at_zero(self):
        self.send()
        self.controller.stop(self.token)
        self.token = self.controller.authorize(cap=2)
        self.assertEqual(self.transport.level, 0)
        self.assertEqual(self.send(100, 'new')['level'], 2)

    def test_real_transport_is_rejected(self):
        with self.assertRaises(TypeError):
            MockSafetyController(object())
