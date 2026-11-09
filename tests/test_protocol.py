from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fun001_protocol import (_packetize, build_control_frame, build_init_frame,
                             build_observed_pause_frame, build_state_frames,
                             build_stop_frames)


class ProtocolTests(unittest.TestCase):
    def test_observed_pause_structure_for_all_sequence_bytes(self):
        for sequence in range(256):
            frame = build_observed_pause_frame(sequence)
            self.assertEqual(len(frame), 16)
            self.assertEqual(frame[:4], bytes((sequence, 1, 0, 3)))
            self.assertEqual(frame[4:], bytes.fromhex('000111000003110000071100'))

    def test_invalid_sequence_rejected(self):
        for value in (-1, 256, True, 1.0, '1', None):
            with self.assertRaises(ValueError):
                build_observed_pause_frame(value)

    def test_apk_connection_payload(self):
        self.assertEqual(build_init_frame(sequence=0),
                         bytes.fromhex('0001000200c81101000e1100'))

    def test_sanitized_owner_capture_regression(self):
        path = Path(__file__).parent / 'fixtures' / 'zero_capture.json'
        fixture = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(fixture['sequence_normalized'])
        self.assertEqual([build_init_frame(sequence=0).hex()], fixture['init'])
        self.assertEqual([f.hex() for f in build_state_frames({}, sequence=0)],
                         fixture['state_zero'])
        self.assertEqual([f.hex() for f in build_stop_frames(sequence=0)],
                         fixture['pause'])

    def test_channel_pairs_and_lowest_value(self):
        for function, channel, mode_channel in (('suction', 7, 8),
                                                ('vibration', 1, 2),
                                                ('electric', 3, 4)):
            frame = build_control_frame(function, 1, sequence=0)
            self.assertEqual(frame, bytes((0, 1, 0, 2, 0, channel, 17, 1,
                                          0, mode_channel, 17, 1)))

    def test_cap_clamping_and_modes(self):
        self.assertEqual(build_control_frame('vibration', 100, sequence=0)[7], 1)
        for mode in range(1, 5):
            frame = build_control_frame('vibration', 100, mode,
                                        max_intensity=2, sequence=0)
            self.assertEqual((frame[7], frame[-1]), (2, mode))
        self.assertEqual(build_control_frame('vibration', 1, max_intensity=0)[7], 0)

    def test_invalid_intensity_mode_function_and_cap(self):
        for intensity in (-1, 101, True, 1.5, '1', None, {}, float('nan')):
            with self.assertRaises(ValueError):
                build_control_frame('vibration', intensity)
        for mode in (0, 5, True, '1', None):
            with self.assertRaises(ValueError):
                build_control_frame('vibration', 1, mode)
        for function in ('unknown', [], None):
            with self.assertRaises(ValueError):
                build_control_frame(function, 1)
        for cap in (-1, 101, True, '1'):
            with self.assertRaises(ValueError):
                build_control_frame('vibration', 1, max_intensity=cap)

    def test_state_fragmentation(self):
        frames = build_state_frames({}, sequence=0)
        self.assertEqual([len(f) for f in frames], [20, 10])
        self.assertEqual([f[:2] for f in frames], [b'\x00\x01', b'\x00\x02'])
        reassembled = b''.join(f[2:] for f in frames)
        self.assertEqual(reassembled, bytes.fromhex(
            '0006000711000008110100011100000211010003110000041101'))

    def test_state_validation_and_caps(self):
        for state in ({'raw': 1}, [], {'vibration': -1}):
            with self.assertRaises(ValueError):
                build_state_frames(state)
        for modes in ({'raw': 1}, [], {'vibration': 5}):
            with self.assertRaises(ValueError):
                build_state_frames({}, modes)
        frames = build_state_frames({'suction': 100, 'vibration': 100, 'electric': 100},
                                    sequence=0)
        data = b''.join(f[2:] for f in frames)
        self.assertEqual([data[i] for i in (5, 13, 21)], [1, 1, 1])

    def test_exact_chunk_boundary_needs_empty_terminator(self):
        frames = _packetize(bytes(17), 0)
        self.assertEqual([len(f) for f in frames], [20, 2])
        self.assertEqual(frames[-1], b'\x00\x02')

    def test_generated_markers_follow_apk_range(self):
        for _ in range(256):
            frames = build_state_frames({})
            self.assertLess(frames[0][0], 255)
            self.assertEqual(frames[0][0], frames[1][0])
        self.assertEqual(build_stop_frames(sequence=0), [build_observed_pause_frame(0)])
