"""Supervised BLE disconnection check at a fixed minimum vibration value."""
import asyncio
import time

from ble_readonly import verified_write_characteristic
from fun001_protocol import build_control_frame, build_stop_frames
from device_safety import blocked_nonzero_result, nonzero_bench_allowed


async def bench_disconnect(address, confirmation, scanner=None, client_type=None,
                           clock=time.monotonic, sleep=asyncio.sleep,
                           on_disconnected=None):
    result = {'status': 'confirmation_required', 'nonzero_attempts': 0,
              'start_ack_received': False, 'connection_closed': False,
              'connection_scope': 'application_gatt_session',
              'physical_radio_disconnect_verified': False,
              'recovery_zero_ack': False, 'device_state': 'unknown',
              'firmware_disconnect_stop_verified': False,
              'observation_signals_completed': False,
              'manual_power_off_required': True}
    if confirmation != '断连1':
        return result
    if not nonzero_bench_allowed():
        return blocked_nonzero_result()
    deadline = clock() + 30
    disconnected = False
    try:
        if scanner is None or client_type is None:
            from bleak import BleakClient, BleakScanner
            scanner = BleakScanner
            client_type = BleakClient
        device = await scanner.find_device_by_filter(
            lambda device, adv: device.address.upper() == address, timeout=10)
        if device is None or device.address.upper() != address:
            result['status'] = 'bound_device_not_found'
            return result
        async with client_type(device, pair=False, timeout=15,
                               winrt={'use_cached_services': False}) as client:
            write = verified_write_characteristic(client.services)
            if write is None:
                result['status'] = 'gatt_mismatch'
            elif clock() >= deadline:
                result['status'] = 'confirmation_expired'
            else:
                zero, = build_stop_frames()
                await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
                if clock() >= deadline:
                    result['status'] = 'confirmation_expired'
                else:
                    pulse = build_control_frame('vibration', 1, mode=1, max_intensity=1)
                    result['nonzero_attempts'] = 1
                    try:
                        await asyncio.wait_for(client.write_gatt_char(write, pulse, response=True), 0.25)
                        result['start_ack_received'] = True
                        await sleep(0.25)
                        await asyncio.wait_for(client.disconnect(), 3)
                        disconnected = not client.is_connected
                        if not disconnected:
                            raise RuntimeError('Disconnection unconfirmed')
                        result['connection_closed'] = True
                        result['status'] = 'link_closed_observation_required'
                        if on_disconnected is not None:
                            await on_disconnected()
                            result['observation_signals_completed'] = True
                    finally:
                        # Only a confirmed deliberate disconnect skips software zero.
                        # Uncertain starts/disconnects take the ordinary stop path.
                        if not disconnected:
                            zero, = build_stop_frames()
                            await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
                            result['recovery_zero_ack'] = True
        if result['nonzero_attempts'] == 0:
            result['connection_closed'] = True
    except Exception as exc:
        result['status'] = 'unknown' if result['nonzero_attempts'] else 'preflight_error'
        result['error_type'] = type(exc).__name__
    return result
