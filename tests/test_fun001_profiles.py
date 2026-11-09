import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fun001_core import ControlRejected, RealSafetyController, VERIFIED_ENVELOPE
from fun001_protocol import FUNCTIONS, MODE_NAMES, build_control_frame, build_stop_frames
from fun001_service import ControlClient, ControlServer, ControlState, parse_grant, sustained_level
import fun001_mcp
import fun001_service
from test_fun001_core import FakeClock, FakeLink, FakeSleepGuard

ZERO = build_stop_frames()[0][1:]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.link = FakeLink()
        self.core = RealSafetyController(self.link, self.clock, FakeSleepGuard(), lambda: True)

    def grant(self, **kwargs):
        self.token = self.core.authorize(**kwargs)
        self.link.frames.clear()

    def send(self, level=1, key='one', **kwargs):
        return self.core.set_level(self.token, level=level, key=key, **kwargs)

    def test_apk_waveform_labels_are_available_in_status_and_tool_schema(self):
        self.assertEqual(MODE_NAMES, {1: 'Gentle Foreplay', 2: 'Flirt & Heat Up',
                                     3: 'Constant Waves', 4: 'Wild Climax'})
        catalog = self.core.status()['mode_catalog']
        self.assertEqual(catalog, [{'mode': code, 'name': name}
                                   for code, name in MODE_NAMES.items()])
        for tool in fun001_mcp.TOOLS:
            if tool['name'] in ('set_intensity', 'run_seconds'):
                description = tool['inputSchema']['properties']['mode']['description']
                self.assertTrue(all(name in description for name in MODE_NAMES.values()))

    def test_default_keeps_verified_scope(self):
        self.grant()
        self.assertEqual(self.core.status()['verified_envelope'], VERIFIED_ENVELOPE)
        self.assertFalse(self.core.status()['unverified_configuration'])
        self.assertEqual(self.send(100)['level'], 1)
        with self.assertRaises(ControlRejected):
            self.send(mode=2)

    def test_unverified_profiles_require_owner_opt_in_before_connect(self):
        for kwargs in ({'function': 'suction'}, {'function': 'electric'},
                       {'modes': (2,)}, {'modes': (1, 2, 3, 4)}, {'cap': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ControlRejected):
                self.core.authorize(**kwargs)
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.frames, [])

    def test_all_functions_modes_and_intensity_boundaries_match_encoder(self):
        for function in FUNCTIONS:
            for mode in range(1, 5):
                for level in (0, 1, 2, 50, 99, 100):
                    with self.subTest(function=function, mode=mode, level=level):
                        link = FakeLink()
                        core = RealSafetyController(link, self.clock, FakeSleepGuard(), lambda: True)
                        token = core.authorize(function=function, modes=(mode,), cap=100,
                                               allow_unverified=True)
                        result = core.set_level(token, level=level, key='one')
                        expected = (ZERO if level == 0 else build_control_frame(
                            function, level, mode, max_intensity=100)[1:])
                        self.assertEqual(link.frames[-1][1:], expected)
                        self.assertEqual(result['function'], function)
                        self.assertEqual(result['mode'], mode)
                        self.assertEqual(result['level'], level)

    def test_intensity_is_clamped_to_session_cap(self):
        self.grant(function='suction', modes=(3,), cap=7, allow_unverified=True)
        result = self.send(100)
        self.assertEqual(result['level'], 7)
        self.assertEqual(result['requested_level'], 100)
        self.assertTrue(result['clamped'])
        self.assertEqual(self.link.frames[-1][1:],
                         build_control_frame('suction', 7, 3, max_intensity=7)[1:])

    def test_zero_cap_only_writes_stop(self):
        self.grant(cap=0)
        self.assertEqual(self.send(100)['level'], 0)
        self.assertEqual(self.link.frames[-1][1:], ZERO)

    def test_function_and_modes_cannot_escape_session(self):
        self.grant(function='suction', modes=(2, 4), allow_unverified=True)
        for kwargs in ({'function': 'vibration'}, {'mode': 1}, {'mode': True},
                       {'mode': 2.0}, {'function': []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ControlRejected):
                self.send(**kwargs)
        self.assertEqual(self.link.frames, [])

    def test_invalid_grant_rejected_before_zero_barrier(self):
        for kwargs in ({'function': 'other'}, {'modes': ()}, {'modes': (1, 1)},
                       {'modes': (True,)}, {'modes': '1'}, {'modes': (5,)},
                       {'cap': True}, {'cap': -1}, {'cap': 101},
                       {'allow_unverified': 'yes'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ControlRejected):
                self.core.authorize(**kwargs)
        self.assertEqual(self.link.frames, [])

    def test_mode_change_zeroes_before_new_nonzero(self):
        self.grant(modes=(1, 2, 3, 4), cap=5, allow_unverified=True)
        self.send(3, 'a', mode=2)
        self.send(4, 'b', mode=4)
        self.assertEqual([frame[1:] for frame in self.link.frames], [
            build_control_frame('vibration', 3, 2, max_intensity=5)[1:], ZERO,
            build_control_frame('vibration', 4, 4, max_intensity=5)[1:]])

    def test_failed_mode_barrier_never_starts_new_mode(self):
        self.grant(modes=(1, 2), allow_unverified=True)
        self.send(mode=1)
        self.link.fail_writes = ['unknown']
        result = self.send(key='switch', mode=2)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertFalse(self.link.connected)
        self.assertNotIn(build_control_frame('vibration', 1, 2)[1:],
                         [frame[1:] for frame in self.link.frames])

    def test_changed_mode_cannot_reuse_idempotency_key(self):
        self.grant(modes=(1, 2), allow_unverified=True)
        first = self.send(mode=1)
        self.assertEqual(self.send(mode=1), first)
        self.assertEqual(len(self.link.frames), 1)
        with self.assertRaises(ControlRejected):
            self.send(mode=2)

    def test_new_default_grant_resets_extended_permissions(self):
        self.grant(function='electric', modes=(4,), cap=50, allow_unverified=True)
        self.core.stop(self.token)
        self.grant()
        self.assertEqual(self.core.status()['authorized_envelope'],
                         {'function': 'vibration', 'modes': [1], 'max_intensity': 1})

    def test_ttl_zeroes_and_revokes_extended_profile(self):
        self.grant(function='suction', modes=(3,), cap=4, allow_unverified=True)
        self.send(3, ttl=0.25)
        self.clock.now += 0.25
        self.core.tick()
        self.assertIsNone(self.core.token)
        self.assertEqual(self.link.frames[-1][1:], ZERO)
        self.assertFalse(self.link.connected)


class ProfileInterfaceTests(unittest.TestCase):
    def test_grant_parser_and_invalid_forms(self):
        self.assertEqual(parse_grant('grant'),
                         {'function': 'vibration', 'modes': (1,), 'cap': 1})
        self.assertEqual(parse_grant('grant suction all 7'),
                         {'function': 'suction', 'modes': (1, 2, 3, 4), 'cap': 7})
        for value in ('grant electric 0 1', 'grant vibration all 101', 'grant other 1 1',
                      'grant vibration 1', 'grant vibration 1.5 1', 'grant vibration 1 -1'):
            with self.subTest(value=value), self.assertRaises(ControlRejected):
                parse_grant(value)

    def test_run_arguments_reject_invalid_selection_without_requests(self):
        class NoRequests:
            def request(self, *args):
                raise AssertionError('Invalid input reached transport')
        for extra in ({'level': True}, {'level': 101}, {'level': -1},
                      {'mode': True}, {'mode': 5}, {'function': 'other'}, {'raw': 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                fun001_mcp.run_seconds(NoRequests(), {'session_id': 's', 'seconds': 1, **extra})

    def test_http_mcp_and_console_share_authorized_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
            state.authorize(function='suction', modes=(1, 3), cap=4, allow_unverified=True)
            server = ControlServer(state=state, directory=Path(temporary) / 'runtime', port=0)
            server.start()
            client = ControlClient(server.directory)
            try:
                session = state.status()['session_id']
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'set_intensity', 'arguments': {
                               'session_id': session, 'function': 'suction', 'mode': 3,
                               'level': 100, 'key': 'profile'}}}
                reply = fun001_mcp.dispatch(request, client)['result']
                self.assertFalse(reply['isError'])
                self.assertEqual(json.loads(reply['content'][0]['text'])['level'], 4)
                self.assertEqual(state.link.frames[-1][1:],
                                 build_control_frame('suction', 4, 3, max_intensity=4)[1:])
                with self.assertRaises(ControlRejected):
                    client.request('set_intensity', {'session_id': session, 'level': 1,
                                                       'key': 'bad', 'cap': 100})
                with self.assertRaises(ControlRejected):
                    client.request('authorize', {})
                client.close()
                self.assertFalse(state.link.connected)
            finally:
                client.close()
                server.close()
            state = ControlState(FakeLink(), nonzero_allowed=lambda: True)
            state.authorize(function='electric', modes=(4,), cap=3, allow_unverified=True)
            now = [0.0]
            def sleep(seconds):
                now[0] += seconds
            result = sustained_level(state, 50, 1, mode=4,
                                     clock=lambda: now[0], sleep=sleep)
            self.assertEqual(result['last_execution']['function'], 'electric')
            self.assertEqual(result['last_execution']['level'], 3)
            self.assertEqual(state.link.frames[-1][1:], ZERO)
            state.close()

    def test_run_seconds_passes_selection_and_preserves_applied_value(self):
        class Client:
            def __init__(self):
                self.now = 0.0
                self.calls = []
            def request(self, action, args):
                self.calls.append((action, args))
                if action == 'set_intensity':
                    return {'outcome': 'gatt_ack', 'level': min(args['level'], 5),
                            'mode': args.get('mode', 1), 'function': 'suction'}
                return {'outcome': 'gatt_ack'}
            def wait(self, seconds):
                self.now += seconds
        client = Client()
        result = fun001_mcp.run_seconds(client, {
            'session_id': 's', 'seconds': 1.2, 'function': 'suction', 'mode': 4, 'level': 100},
            clock=lambda: client.now, wait=client.wait)
        self.assertEqual(result['requested_level'], 100)
        self.assertEqual(result['applied_level'], 5)
        self.assertEqual(result['applied_mode'], 4)
        for action, args in client.calls[:-1]:
            if action == 'set_intensity':
                self.assertEqual(args['mode'], 4)
                self.assertEqual(args['function'], 'suction')
        self.assertEqual(client.calls[-1][1]['level'], 0)

    def test_owner_console_accepts_selection_and_applies_cap(self):
        link = FakeLink()
        state = ControlState(link, nonzero_allowed=lambda: True)
        server = MagicMock()
        server.close.side_effect = state.close
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        def run(state, level, seconds, *, mode):
            return sustained_level(state, level, seconds, mode=mode,
                                   clock=lambda: now[0], sleep=sleep)
        commands = ['functions', 'grant suction all 4', '是', '接受本次未验收配置',
                    'level 100 1 3', 'stop', 'quit']
        with patch('fun001_service.load_binding', return_value='synthetic'), \
             patch('fun001_service.BleLink'), \
             patch('fun001_service.ControlState', return_value=state), \
             patch('fun001_service.ControlServer', return_value=server), \
             patch('fun001_service.sustained_level', side_effect=run), \
             patch('builtins.input', side_effect=commands), \
             patch('sys.stdout', new=io.StringIO()):
            fun001_service.owner_console()
        self.assertIn(build_control_frame('suction', 4, 3, max_intensity=4)[1:],
                      [frame[1:] for frame in link.frames])
        self.assertFalse(link.connected)

    def test_owner_cancellation_prevents_extended_authorization(self):
        link = FakeLink()
        state = ControlState(link, nonzero_allowed=lambda: True)
        server = MagicMock()
        server.close.side_effect = state.close
        with patch('fun001_service.load_binding', return_value='synthetic'), \
             patch('fun001_service.BleLink'), \
             patch('fun001_service.ControlState', return_value=state), \
             patch('fun001_service.ControlServer', return_value=server), \
             patch('builtins.input', side_effect=['grant electric all 100', '是', '取消', 'quit']), \
             patch('sys.stdout', new=io.StringIO()):
            fun001_service.owner_console()
        self.assertEqual(link.frames, [])
        self.assertIsNone(state.controller.token)


if __name__ == '__main__':
    unittest.main()
