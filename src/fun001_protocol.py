"""Offline FUN-001 encoding from APK 3.0.7 and authorized-device captures.

Physical acceptance is limited to the cases documented in docs/SAFETY.md.
"""
import secrets

VERSION = 'fun001-apk307-offline-v2'
SERVICE_UUID = '0000ee01-0000-1000-8000-00805f9b34fb'
WRITE_UUID = '0000ee03-0000-1000-8000-00805f9b34fb'
NOTIFY_UUID = '0000ee02-0000-1000-8000-00805f9b34fb'
PAUSE_BODY = bytes.fromhex('03000111000003110000071100')
INIT_BODY = bytes.fromhex('0200c81101000e1100')
FUNCTIONS = {'suction': (7, 8), 'vibration': (1, 2), 'electric': (3, 4)}
# APK 3.0.7 function 16615 modeName and its English resource table.
# These are UI preset labels; physical waveform timing remains unmeasured.
MODE_NAMES = {1: 'Gentle Foreplay', 2: 'Flirt & Heat Up',
              3: 'Constant Waves', 4: 'Wild Climax'}


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} outside supported integer range')
    return value


def _packetize(payload, sequence=None):
    # RNBLEModule.sendData2 prepends 00, then chunks at 18 data bytes.
    if sequence is None:
        sequence = secrets.randbelow(255)
    sequence = _integer(sequence, 0, 255, 'Sequence')
    data = b'\x00' + payload
    chunks = [data[offset:offset + 18] for offset in range(0, len(data), 18)]
    if len(chunks[-1]) == 18:
        chunks.append(b'')
    return [bytes((sequence, index)) + chunk
            for index, chunk in enumerate(chunks, 1)]


def _control_payload(function, intensity, mode, maximum):
    if not isinstance(function, str) or function not in FUNCTIONS:
        raise ValueError('Unknown function')
    intensity = _integer(intensity, 0, 100, 'Intensity')
    mode = _integer(mode, 1, 4, 'Mode')
    maximum = _integer(maximum, 0, 100, 'Owner cap')
    intensity = min(intensity, maximum)
    channel, mode_channel = FUNCTIONS[function]
    return bytes((2, 0, channel, 0x11, intensity, 0, mode_channel, 0x11, mode))


def build_observed_pause_frame(sequence):
    """Preserve the explicit-sequence entry point for the observed zero frame."""
    sequence = _integer(sequence, 0, 255, 'Sequence')
    return _packetize(PAUSE_BODY, sequence)[0]


def build_control_frame(function, intensity, mode=1, *, max_intensity=1, sequence=None):
    """Encode one channel pair with strict input validation and owner cap."""
    payload = _control_payload(function, intensity, mode, max_intensity)
    return _packetize(payload, sequence)[0]


def build_init_frame(*, sequence=None):
    """Encode the connection query/init payload observed in APK and HCI."""
    return _packetize(INIT_BODY, sequence)[0]


def build_stop_frames(*, sequence=None):
    return _packetize(PAUSE_BODY, sequence)


def build_state_frames(state, modes=None, *, max_intensity=1, sequence=None):
    """Encode the APK's three-function, six-record state as one packet group."""
    if not isinstance(state, dict) or set(state) - FUNCTIONS.keys():
        raise ValueError('Invalid state fields')
    if modes is None:
        modes = {}
    if not isinstance(modes, dict) or set(modes) - FUNCTIONS.keys():
        raise ValueError('Invalid mode fields')
    records = []
    for function in FUNCTIONS:
        payload = _control_payload(function, state.get(function, 0),
                                   modes.get(function, 1), max_intensity)
        records.append(payload[1:])
    return _packetize(b'\x06' + b''.join(records), sequence)
