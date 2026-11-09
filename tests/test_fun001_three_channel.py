import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fun001_core import ControlRejected, RealSafetyController, UnknownExecution
from fun001_protocol import FUNCTIONS, build_state_frames, build_stop_frames
from fun001_service import ControlState, ControlServer, ControlClient, parse_grant, sustained_state
import fun001_mcp
import fun001_service
from test_fun001_core import FakeClock, FakeLink, FakeSleepGuard
from test_fun001_mcp_regressions import InputQueue

ZERO = build_stop_frames()[0][1:]
PROFILES = {'suction': {'cap': 2, 'modes': (1, 2)},
            'vibration': {'cap': 5, 'modes': (3, 4)},
            'electric': {'cap': 1, 'modes': (1,)}}
TARGET = {'suction': {'level': 2, 'mode': 2},
          'vibration': {'level': 4, 'mode': 3},
          'electric': {'level': 1, 'mode': 1}}


class ThreeChannelTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.link = FakeLink()
        self.core = RealSafetyController(self.link, self.clock, FakeSleepGuard(), lambda: True)
        self.token = self.core.authorize(channels=PROFILES, allow_unverified=True)
        self.link.frames.clear()

    def send(self, state=TARGET, key='state', ttl=1):
        return self.core.set_state(self.token, state=state, key=key, ttl=ttl)

    def test_full_state_uses_two_ordered_fragments_with_one_marker(self):
        result = self.send()
        expected = build_state_frames({n: v['level'] for n, v in TARGET.items()},
                                      {n: v['mode'] for n, v in TARGET.items()}, max_intensity=100)
        self.assertEqual([len(frame) for frame in self.link.frames], [20, 10])
        self.assertEqual(self.link.frames[0][0], self.link.frames[1][0])
        self.assertEqual([frame[1:] for frame in self.link.frames], [f[1:] for f in expected])
        self.assertEqual(result['state'], TARGET)
        self.assertEqual(self.core.status()['desired_state'], TARGET)

    def test_caps_are_independent(self):
        result = self.send({name: {'level': 100} for name in FUNCTIONS})
        self.assertEqual({n: v['level'] for n, v in result['state'].items()},
                         {'suction': 2, 'vibration': 5, 'electric': 1})
        self.assertEqual(set(result['clamped_channels']), set(FUNCTIONS))

    def test_omitted_channels_are_zero(self):
        self.send()
        result = self.send({'suction': {'level': 1, 'mode': 2}}, key='replace')
        self.assertEqual(result['state']['suction']['level'], 1)
        self.assertEqual(result['state']['vibration']['level'], 0)
        self.assertEqual(result['state']['electric']['level'], 0)

    def test_single_update_preserves_other_channels(self):
        self.send()
        self.core.set_level(self.token, function='suction', mode=2, level=1, key='patch')
        self.assertEqual(self.core.desired_state['vibration'], TARGET['vibration'])
        self.assertEqual(self.core.desired_state['electric'], TARGET['electric'])
        self.assertEqual(self.core.desired_state['suction']['level'], 1)

    def test_explicit_channel_zero_preserves_others(self):
        self.send()
        self.core.set_level(self.token, function='suction', mode=2, level=0, key='off')
        self.assertEqual(self.core.desired_state['suction']['level'], 0)
        self.assertEqual(self.core.desired_state['vibration']['level'], 4)

    def test_legacy_unqualified_zero_stops_every_channel(self):
        self.send()
        self.core.set_level(self.token, level=0, key='alloff')
        self.assertEqual(self.link.frames[-1][1:], ZERO)
        self.assertTrue(all(v['level'] == 0 for v in self.core.desired_state.values()))

    def test_empty_state_stops_every_channel(self):
        self.send()
        self.send({}, key='empty')
        self.assertEqual(self.link.frames[-1][1:], ZERO)
        self.assertEqual(self.core.desired_level, 0)

    def test_mode_change_has_zero_barrier(self):
        self.send()
        before = len(self.link.frames)
        changed = {name: dict(value) for name, value in TARGET.items()}
        changed['vibration']['mode'] = 4
        self.send(changed, key='modechange')
        self.assertEqual(self.link.frames[before][1:], ZERO)
        self.assertEqual(len(self.link.frames) - before, 3)

    def test_idempotent_state_does_not_write_or_extend_ttl(self):
        result = self.send(ttl=0.25)
        result['state']['suction']['level'] = 99
        self.clock.now += 0.1
        again = self.send(ttl=0.25)
        self.assertEqual(again['state']['suction']['level'], 2)
        self.assertEqual(len(self.link.frames), 2)
        self.clock.now += 0.15
        self.core.tick()
        self.assertIsNone(self.core.token)
        self.assertEqual(self.link.frames[-1][1:], ZERO)

    def test_same_key_different_state_is_rejected(self):
        self.send()
        with self.assertRaises(ControlRejected):
            self.send({})
        self.assertEqual(len(self.link.frames), 2)

    def test_input_rejection_has_no_writes(self):
        for state in (None, [], {'other': {'level': 1}}, {'suction': 1},
                      {'suction': {}}, {'suction': {'level': True}},
                      {'suction': {'level': -1}}, {'suction': {'level': 101}},
                      {'suction': {'level': 1, 'mode': 4}},
                      {'suction': {'level': 1, 'mode': True}},
                      {'suction': {'level': 1, 'raw': 1}}):
            with self.subTest(state=state), self.assertRaises(ControlRejected):
                self.send(state)
        self.assertEqual(self.link.frames, [])

    def test_nonzero_in_disabled_channel_is_clamped_to_zero(self):
        self.core.stop(self.token)
        self.token = self.core.authorize(channels={'suction': {'cap': 1, 'modes': (1,)},
                                                   'electric': {'cap': 0, 'modes': (1,)}},
                                          allow_unverified=True)
        result = self.send({'suction': {'level': 1}, 'electric': {'level': 100}})
        self.assertEqual(result['state']['electric']['level'], 0)
        with self.assertRaises(ControlRejected):
            self.send({'vibration': {'level': 1}}, key='unauthorized')

    def test_global_live_policy_still_blocks_combined_nonzero(self):
        self.core._nonzero_allowed = lambda: False
        self.assertEqual(self.send()['outcome'], 'blocked_by_disconnect_safety')
        self.assertEqual(self.link.frames, [])

    def test_combined_grant_needs_owner_confirmation(self):
        core = RealSafetyController(FakeLink(), self.clock, FakeSleepGuard(), lambda: True)
        with self.assertRaises(ControlRejected):
            core.authorize(channels=PROFILES)
        token = core.authorize()
        with self.assertRaises(ControlRejected):
            core.set_state(token, state={'vibration': {'level': 1}}, key='not-granted')

    def test_invalid_channel_profiles_are_rejected_before_connect(self):
        for profiles in ({}, {'bad': {'cap': 1, 'modes': [1]}},
                         {'suction': {'cap': True, 'modes': [1]}},
                         {'suction': {'cap': 1, 'modes': []}},
                         {'suction': {'cap': 1, 'modes': [1], 'extra': 1}}):
            with self.subTest(profiles=profiles):
                link = FakeLink()
                core = RealSafetyController(link)
                with self.assertRaises(ControlRejected):
                    core.authorize(channels=profiles, allow_unverified=True)
                self.assertFalse(link.connected)

    def test_only_suction_nonzero_still_expires_when_primary_is_zero(self):
        self.send({'suction': {'level': 1}}, ttl=0.25)
        self.assertEqual(self.core.desired_level, 1)
        self.clock.now += 0.25
        self.core.tick()
        self.assertIsNone(self.core.token)
        self.assertFalse(self.link.connected)

    def test_emergency_clears_every_channel(self):
        self.send()
        self.core.emergency_stop()
        self.assertTrue(all(v['level'] == 0 for v in self.core.desired_state.values()))
        self.assertIsNone(self.core.token)
        self.assertEqual(self.link.frames[-1][1:], ZERO)

    def test_expired_session_rejects_state(self):
        self.clock.now += 30
        with self.assertRaises(ControlRejected):
            self.send()


class FragmentFailureTests(unittest.TestCase):
    def setup_link(self, fail_at=None, expire=False, emergency=False):
        clock = FakeClock()
        class Link(FakeLink):
            def __init__(self):
                super().__init__()
                self.attempts = 0
            def write(self, frame):
                self.attempts += 1
                if self.attempts == fail_at:
                    raise UnknownExecution('synthetic')
                super().write(frame)
                if self.attempts == 1 and expire:
                    clock.now += 1
                if self.attempts == 1 and emergency:
                    core.request_emergency_stop()
        link = Link()
        core = RealSafetyController(link, clock, FakeSleepGuard(), lambda: True)
        # Initialize the grant without triggering the injected transaction fault.
        link.attempts = -100
        token = core.authorize(channels=PROFILES, allow_unverified=True)
        link.frames.clear()
        link.attempts = 0
        return core, token, link

    def test_second_fragment_failure_zeroes_and_never_replays(self):
        core, token, link = self.setup_link(fail_at=2)
        result = core.set_state(token, state=TARGET, key='one')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(link.attempts, 3)
        self.assertEqual([len(f) for f in link.frames], [20, 16])
        self.assertEqual(link.frames[-1][1:], ZERO)
        self.assertFalse(link.connected)
        self.assertTrue(core.status()['unknown_latched'])
        with self.assertRaises(ControlRejected):
            core.set_state(token, state=TARGET, key='one')
        self.assertEqual(link.attempts, 3)

    def test_first_fragment_failure_does_not_send_second(self):
        core, token, link = self.setup_link(fail_at=1)
        self.assertEqual(core.set_state(token, state=TARGET, key='one')['outcome'], 'unknown')
        self.assertEqual(link.attempts, 2)
        self.assertEqual([f[1:] for f in link.frames], [ZERO])

    def test_ttl_between_fragments_stops_before_second_fragment(self):
        core, token, link = self.setup_link(expire=True)
        self.assertEqual(core.set_state(token, state=TARGET, key='one')['outcome'], 'unknown')
        self.assertEqual([len(f) for f in link.frames], [20, 16])

    def test_emergency_between_fragments_stops_before_second_fragment(self):
        core, token, link = self.setup_link(emergency=True)
        self.assertEqual(core.set_state(token, state=TARGET, key='one')['outcome'], 'unknown')
        self.assertEqual([len(f) for f in link.frames], [20, 16])


class CombinedInterfaceTests(unittest.TestCase):
    def test_grant3_parser(self):
        profile = parse_grant('grant3 1 2 0')['channels']
        self.assertEqual({n: p['cap'] for n, p in profile.items()},
                         {'suction': 1, 'vibration': 2, 'electric': 0})
        self.assertEqual(profile['suction']['modes'], (1, 2, 3, 4))
        self.assertEqual(parse_grant('grant3 1 1 0 2')['channels']['vibration']['modes'], (2,))
        for command in ('grant3 1 2', 'grant3 1 2 101', 'grant3 1 x 1', 'grant3 1 1 1 5'):
            with self.assertRaises(ControlRejected):
                parse_grant(command)

    def test_http_and_mcp_combined_state_and_client_close(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
            state.authorize(channels=PROFILES, allow_unverified=True)
            server = ControlServer(state=state, directory=Path(directory)/'runtime', port=0)
            server.start()
            client = ControlClient(server.directory)
            try:
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                    'name': 'set_state', 'arguments': {'session_id': state.session_id,
                                                       'state': TARGET, 'key': 'one'}}}
                result = fun001_mcp.dispatch(request, client)['result']
                self.assertFalse(result['isError'])
                self.assertEqual(json.loads(result['content'][0]['text'])['state'], TARGET)
                with self.assertRaises(ControlRejected):
                    client.request('set_state', {'session_id': state.session_id,
                                                 'state': TARGET, 'key': 'bad', 'channels': PROFILES})
                client.close()
                self.assertFalse(state.link.connected)
                self.assertEqual(state.link.frames[-1][1:], ZERO)
            finally:
                client.close()
                server.close()

    def test_mcp_timed_state_zeroes_and_preserves_applied_state(self):
        class Client:
            def __init__(self):
                self.calls = []
                self.now = 0.0
            def request(self, action, args):
                self.calls.append((action, args))
                return {'outcome': 'gatt_ack', 'state': args.get('state')}
            def wait(self, seconds):
                self.now += seconds
        client = Client()
        result = fun001_mcp.run_state_seconds(client, {
            'session_id': 's', 'seconds': 1.2, 'state': TARGET},
            clock=lambda: client.now, wait=client.wait)
        self.assertAlmostEqual(client.now, 1.2)
        self.assertEqual(result['applied_state'], TARGET)
        self.assertEqual(client.calls[-1][1]['state'], {})

    def test_mcp_bad_state_rejected_without_calls(self):
        client = MagicMock()
        for value in ([], {'other': {'level': 1}}, {'suction': {'level': True}},
                      {'suction': {'level': 1, 'mode': 5}}):
            with self.assertRaises(ValueError):
                fun001_mcp.run_state_seconds(client, {'session_id': 's', 'seconds': 1, 'state': value})
        client.request.assert_not_called()

    def test_owner_console_grant3_and_timed_state(self):
        state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
        server = MagicMock()
        server.close.side_effect = state.close
        commands = ['grant3 1 1 0', '是', '接受本次未验收配置',
                    'state {"suction":{"level":1,"mode":2},"vibration":{"level":1,"mode":3}} 1',
                    'stop', 'quit']
        original = sustained_state
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        def sustain(state, channels, seconds):
            return original(state, channels, seconds, clock=lambda: now[0], sleep=sleep)
        with patch('fun001_service.load_binding', return_value='synthetic'), \
             patch('fun001_service.BleLink'), \
             patch('fun001_service.ControlState', return_value=state), \
             patch('fun001_service.ControlServer', return_value=server), \
             patch('fun001_service.sustained_state', side_effect=sustain), \
             patch('builtins.input', side_effect=commands), patch('sys.stdout', new=io.StringIO()):
            fun001_service.owner_console()
        self.assertTrue(any(len(frame) == 20 for frame in state.link.frames))
        self.assertEqual(state.link.frames[-1][1:], ZERO)

    def test_state_emergency_is_signalled_before_outer_lock(self):
        state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
        state.authorize(channels=PROFILES, allow_unverified=True)
        entered = threading.Event()
        def stop():
            entered.set()
            state.execute('emergency_stop', {}, 'other')
        with state.lock:
            worker = threading.Thread(target=stop)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(state.controller._stop_requested.wait(1))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(state.controller.token)

    def test_real_stdio_timed_state_can_be_emergency_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
            state.authorize(channels=PROFILES, allow_unverified=True)
            server = ControlServer(state=state, directory=Path(directory)/'runtime', port=0)
            server.start()
            script = ('import sys\nfrom fun001_service import ControlClient\n'
                      'from fun001_mcp import serve\nclient=ControlClient(sys.argv[1])\n'
                      'try:\n    serve(client)\nfinally:\n    client.close()\n')
            process = subprocess.Popen([sys.executable, '-B', '-c', script, str(server.directory)],
                                       cwd=Path(__file__).resolve().parents[1] / 'src', text=True,
                                       encoding='utf-8', stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def send(identifier, name, arguments):
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identifier,
                    'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})+'\n')
                process.stdin.flush()
            try:
                send(1, 'run_state_seconds', {'session_id': state.session_id,
                                               'state': TARGET, 'seconds': 300})
                deadline = time.monotonic()+5
                while len(state.link.frames) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(state.link.frames), 3)
                self.assertEqual([len(f) for f in state.link.frames[1:3]], [20, 10])
                send(2, 'emergency_stop', {})
                deadline = time.monotonic()+3
                while state.link.connected and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(state.link.connected)
                self.assertEqual(state.link.frames[-1][1:], ZERO)
            finally:
                try:
                    output, errors = process.communicate(timeout=10)
                finally:
                    server.close()
            self.assertEqual(process.returncode, 0)
            self.assertEqual(errors, '')
            replies = [json.loads(line) for line in output.splitlines()]
            stop = next(reply for reply in replies if reply['id'] == 2)
            self.assertFalse(stop['result']['isError'])

    def test_suction_keeps_watchdog_active_when_primary_is_zero(self):
        now = [100.0]
        state = ControlState(FakeLink(), clock=lambda: now[0], nonzero_allowed=lambda: True)
        state.authorize(channels=PROFILES, allow_unverified=True)
        state.execute('set_state', {'session_id': state.session_id,
                                    'state': {'suction': {'level': 1}}, 'key': 's', 'ttl': 0.25}, 'a')
        now[0] += 0.25
        state.tick()
        self.assertIsNone(state.session_id)
        self.assertFalse(state.link.connected)


if __name__ == '__main__':
    unittest.main()
