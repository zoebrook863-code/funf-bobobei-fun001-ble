from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fun001_core import ControlRejected
from fun001_service import ControlState
from standing_permit import CHANNELS, StandingPermit
from test_fun001_core import FakeClock, FakeLink


class StandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'permission'
        self.permit = StandingPermit(self.directory)
        self.clock = FakeClock()
        self.link = FakeLink()
        self.states = []
        self.state = self.build(self.permit, self.link)

    def build(self, permit, link=None):
        state = ControlState(link or FakeLink(), clock=self.clock,
                             nonzero_allowed=lambda: True, permit=permit)
        self.states.append(state)
        return state

    def tearDown(self):
        for state in self.states:
            worker = state.connect_worker
            if worker:
                worker.join(3)
            state.close()
        self.temp.cleanup()

    def ready(self, state=None):
        state = state or self.state
        state.tick()
        if state.connect_worker:
            state.connect_worker.join(3)
            self.assertFalse(state.connect_worker.is_alive())
        self.assertTrue(state.status()['authorization_active'])

    def enable(self):
        self.state.enable_standing()
        self.ready()

    def output(self, state=None, key='output'):
        state = state or self.state
        return state.execute('set_state', {
            'session_id': state.session_id, 'key': key,
            'state': {'suction': {'level': 1}, 'vibration': {'level': 1}}}, 'client')

    def test_disabled_permission_does_not_connect(self):
        self.clock.now += 3600
        self.assertFalse(self.state.status()['standing_enabled'])
        self.assertEqual(self.link.connect_count, 0)

    def test_standing_has_no_twenty_minute_expiry(self):
        self.enable()
        session = self.state.session_id
        self.clock.now += 48 * 3600
        status = self.state.status()
        self.assertEqual(status['session_id'], session)
        self.assertEqual(status['authorization_mode'], 'standing')
        self.assertEqual(json.loads(json.dumps(status['authorized_channels'])), CHANNELS)
        json.dumps(status, allow_nan=False)

    def test_clean_restart_restores_permission_at_zero(self):
        self.enable()
        old = self.state.session_id
        self.state.close()
        new = self.build(StandingPermit(self.directory))
        self.ready(new)
        self.assertNotEqual(new.session_id, old)
        self.assertEqual(new.controller.desired_level, 0)
        self.assertEqual(len(new.link.frames), 1)

    def test_local_revoke_survives_restart(self):
        self.enable()
        self.state.revoke_standing()
        self.clock.now += 10000
        self.assertFalse(self.state.status()['authorization_active'])
        new = self.build(StandingPermit(self.directory))
        self.assertFalse(new.status()['standing_enabled'])
        self.assertEqual(new.link.connect_count, 0)

    def test_stop_zeroes_then_allows_a_new_zero_session(self):
        self.enable()
        old = self.state.session_id
        self.output()
        self.state.execute('stop', {'session_id': old}, 'client')
        self.assertIsNone(self.state.session_id)
        self.assertTrue(self.permit.snapshot()['enabled'])
        self.clock.now += 5
        self.ready()
        self.assertNotEqual(self.state.session_id, old)
        self.assertEqual(self.state.controller.desired_level, 0)

    def test_emergency_disables_standing_permission(self):
        self.enable()
        self.output()
        self.state.execute('emergency_stop', {}, 'another-client')
        self.clock.now += 5
        self.assertFalse(self.state.status()['standing_enabled'])
        self.assertFalse(StandingPermit(self.directory).snapshot()['enabled'])

    def test_release_control_disables_standing_permission(self):
        self.enable()
        self.state.execute('release_control', {'session_id': self.state.session_id}, 'client')
        self.assertFalse(self.permit.snapshot()['enabled'])

    def test_output_is_journaled_and_zero_clears_marker(self):
        self.enable()
        self.assertEqual(self.output()['outcome'], 'gatt_ack')
        self.assertTrue(json.loads(self.permit.path.read_text())['output_pending'])
        self.state.execute('set_state', {'session_id': self.state.session_id,
                                        'state': {}, 'key': 'zero'}, 'client')
        self.assertFalse(json.loads(self.permit.path.read_text())['output_pending'])

    def test_unclean_restart_requires_owner_recovery(self):
        self.enable()
        self.output()
        restored = StandingPermit(self.directory)
        self.assertEqual(restored.snapshot()['suspended_reason'], 'unclean_output')
        new = self.build(restored)
        self.assertFalse(new.status()['authorization_active'])
        self.assertTrue(new.status()['manual_power_off_required'])
        self.assertEqual(new.link.connect_count, 0)

    def test_idle_disconnect_reconnects_without_replaying_output(self):
        self.enable()
        old = self.state.session_id
        self.link.connected = False
        self.link.lost = True
        self.clock.now += 5
        self.state.tick()
        self.clock.now += 5
        self.ready()
        self.assertNotEqual(self.state.session_id, old)
        self.assertEqual(self.state.controller.desired_level, 0)
        self.assertEqual(len(self.link.frames), 2)

    def test_nonzero_disconnect_suspends_auto_reconnect(self):
        self.enable()
        self.output()
        self.link.connected = False
        self.link.lost = True
        self.clock.now += 5
        status = self.state.status()
        self.assertFalse(status['authorization_active'])
        self.assertEqual(status['standing_suspended'], 'unknown_execution')
        self.assertTrue(status['manual_power_off_required'])
        self.assertEqual(self.link.connect_count, 1)

    def test_unknown_write_remains_suspended_after_cleanup(self):
        self.enable()
        self.link.fail_writes = ['unknown']
        self.assertEqual(self.output()['outcome'], 'unknown')
        self.clock.now += 5
        self.assertFalse(self.state.status()['authorization_active'])
        self.assertEqual(StandingPermit(self.directory).snapshot()['suspended_reason'],
                         'unknown_execution')

    def test_owner_recovery_requires_a_new_zero_barrier(self):
        self.enable()
        self.link.fail_writes = ['unknown']
        self.output()
        self.state.status()
        before = len(self.link.frames)
        self.state.enable_standing()
        self.ready()
        self.assertGreater(len(self.link.frames), before)
        self.assertFalse(self.state.controller.unknown_latched)
        self.assertIsNone(self.permit.snapshot()['suspended_reason'])

    def test_command_ttl_remains_active_in_standing_mode(self):
        self.enable()
        self.output()
        self.clock.now += 1
        self.state.tick()
        self.assertEqual(self.state.controller.desired_level, 0)
        self.assertEqual(self.state.controller.reason, 'command_ttl')
        self.assertTrue(self.permit.snapshot()['enabled'])

    def test_ttl_has_a_reconnect_delay_for_cleanup_observation(self):
        self.enable()
        self.output()
        self.clock.now += 1
        self.state.tick()
        status = self.state.status()
        self.assertFalse(status['authorization_active'])
        self.assertEqual(status['standing_retry_after'], 5)
        self.assertEqual(self.link.connect_count, 1)

    def test_timed_mcp_cleanup_recognizes_ttl_stop_before_reconnection(self):
        import fun001_mcp
        self.enable()
        state = self.state
        class DirectClient:
            def request(self, action, arguments=None):
                return state.execute(action, arguments or {}, 'client')
        def wait(seconds):
            self.clock.now += seconds
            self.state.tick()
        result = fun001_mcp.run_state_seconds(DirectClient(), {
            'session_id': self.state.session_id, 'seconds': 1,
            'state': {'suction': {'level': 1}, 'vibration': {'level': 1}}},
            clock=self.clock, wait=wait)
        self.assertEqual(result['outcome'], 'gatt_ack')
        self.assertEqual(result['zero_outcome'], 'gatt_ack')
        self.assertEqual(result['source'], 'resident_command_ttl_stop')

    def test_revoke_fences_inflight_connection(self):
        entered, release = threading.Event(), threading.Event()
        original = self.link.ensure_connected
        def blocked_connect():
            entered.set()
            release.wait(3)
            original()
        self.link.ensure_connected = blocked_connect
        self.state.enable_standing()
        self.assertTrue(entered.wait(2))
        worker = threading.Thread(target=self.state.revoke_standing)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while self.permit.snapshot()['enabled'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(self.permit.snapshot()['enabled'])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.state.connect_worker.join(3)
        self.assertFalse(self.state.status()['authorization_active'])

    def test_status_remains_responsive_during_connect(self):
        entered, release = threading.Event(), threading.Event()
        original = self.link.ensure_connected
        def blocked_connect():
            entered.set()
            release.wait(3)
            original()
        self.link.ensure_connected = blocked_connect
        self.state.enable_standing()
        self.assertTrue(entered.wait(2))
        try:
            started = time.monotonic()
            status = self.state.status()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(status['standing_connecting'])
        finally:
            release.set()

    def test_invalid_permission_file_fails_closed(self):
        self.permit.path.write_text('{"enabled":true}')
        with self.assertRaises(ValueError):
            StandingPermit(self.directory)

    def test_boolean_profile_values_fail_closed(self):
        data = self.permit.snapshot()
        data.pop('generation')
        data['channels']['suction']['cap'] = True
        self.permit.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            StandingPermit(self.directory)

    def test_revocation_storage_failure_still_stops_output(self):
        self.enable()
        self.output()
        with patch.object(self.permit, '_save', side_effect=OSError('synthetic')):
            with self.assertRaises(ControlRejected):
                self.state.revoke_standing()
        self.assertFalse(self.permit.snapshot()['enabled'])
        self.assertEqual(self.state.controller.desired_level, 0)

    def test_journal_failure_prevents_a_nonzero_write(self):
        self.enable()
        before = len(self.link.frames)
        with patch.object(self.permit, '_save', side_effect=OSError('synthetic')):
            self.assertEqual(self.output()['outcome'], 'unknown')
        zero_body = self.link.frames[0][1:]
        self.assertTrue(all(frame[1:] == zero_body for frame in self.link.frames[before:]))

    def test_standing_idle_does_not_hold_the_sleep_guard(self):
        from test_fun001_core import FakeSleepGuard
        guard = FakeSleepGuard()
        self.state.controller.sleep_guard = guard
        self.enable()
        self.assertFalse(guard.engaged)
        self.output()
        self.state.tick()
        self.assertTrue(guard.engaged)

    def test_api_cannot_enable_or_expand_permission(self):
        for action in ('authorize', 'enable_standing', 'permit'):
            with self.assertRaises(ControlRejected):
                self.state.execute(action, {}, 'client')
        self.assertFalse(self.permit.snapshot()['enabled'])

    def test_selected_caps_and_modes_are_enforced(self):
        self.enable()
        result = self.state.execute('set_state', {'session_id': self.state.session_id,
            'state': {'suction': {'level': 100}, 'vibration': {'level': 100},
                      'electric': {'level': 100}}, 'key': 'caps'}, 'client')
        self.assertEqual([result['state'][name]['level'] for name in CHANNELS], [1, 1, 0])
        with self.assertRaises(ControlRejected):
            self.state.execute('set_state', {'session_id': self.state.session_id,
                'state': {'suction': {'level': 1, 'mode': 2}}, 'key': 'mode'}, 'client')


if __name__ == '__main__':
    unittest.main()
