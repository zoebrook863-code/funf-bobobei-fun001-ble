"""Supervised bench commands enforcing the current live safety policy."""
import asyncio
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

from ble_readonly import load_binding, verified_write_characteristic
from bench_disconnect import bench_disconnect
from device_safety import blocked_nonzero_result, nonzero_bench_allowed
from fun001_protocol import build_control_frame, build_stop_frames

QUESTION = '设备是否已经取下、放在桌面、未接触身体、附近无人使用，并且可以随时按键关机？'


async def observation_signals():
    """Local audible markers emitted after application GATT session closure."""
    import winsound
    deadline = time.monotonic() + 2
    winsound.Beep(1100, 400)
    await asyncio.sleep(max(0, deadline - time.monotonic()))
    winsound.Beep(1500, 150)
    await asyncio.sleep(0.15)
    winsound.Beep(1500, 150)


async def bench_zero(address, confirmation, scanner=None, client_type=None,
                     clock=time.monotonic):
    result = {'status': 'confirmation_required', 'writes_attempted': 0,
              'device_state': 'unknown', 'physical_stop_verified': False,
              'connection_closed': False}
    if confirmation != '是':
        return result
    deadline = clock() + 30
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
                # The observed stop is one packet. No initialization or nonzero path.
                frame, = build_stop_frames()
                result['writes_attempted'] = 1
                await asyncio.wait_for(client.write_gatt_char(write, frame, response=True), 3)
                result['status'] = 'att_ack_only'
        result['connection_closed'] = True
    except Exception as exc:
        result['status'] = 'unknown' if result['writes_attempted'] else 'connection_error'
        result['error_type'] = type(exc).__name__
    return result


async def bench_vibration(address, confirmation, scanner=None, client_type=None,
                          clock=time.monotonic, sleep=asyncio.sleep):
    """Supervised bench pulse only. Nominal timing is not a firmware watchdog."""
    result = {'status': 'confirmation_required', 'nonzero_attempts': 0,
              'start_ack_received': False, 'stop_ack_received': False,
              'device_state': 'unknown', 'connection_closed': False}
    if confirmation != '震动1':
        return result
    if not nonzero_bench_allowed():
        return blocked_nonzero_result()
    authorization_deadline = clock() + 30
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
            elif clock() >= authorization_deadline:
                result['status'] = 'confirmation_expired'
            else:
                zero, = build_stop_frames()
                await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
                if clock() >= authorization_deadline:
                    result['status'] = 'confirmation_expired'
                else:
                    pulse = build_control_frame('vibration', 1, mode=1, max_intensity=1)
                    pulse_deadline = clock() + 0.25
                    result['nonzero_attempts'] = 1
                    try:
                        await asyncio.wait_for(client.write_gatt_char(write, pulse, response=True), 0.25)
                        result['start_ack_received'] = True
                        await sleep(max(0, pulse_deadline - clock()))
                    finally:
                        # Always attempt zero after an uncertain start, never replay nonzero.
                        zero, = build_stop_frames()
                        await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
                        result['stop_ack_received'] = True
                    result['status'] = 'att_acks_only'
        result['connection_closed'] = True
    except Exception as exc:
        result['status'] = 'unknown' if result['nonzero_attempts'] else 'preflight_error'
        result['error_type'] = type(exc).__name__
    return result


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    arguments = sys.argv[1:]
    if arguments not in ([], ['--worker'], ['pulse'], ['--pulse-worker'],
                         ['disconnect'], ['--disconnect-worker']):
        print(json.dumps({'status': 'invalid_arguments'}))
        return
    command = arguments[0] if arguments else 'zero'
    worker_modes = {'--worker': 'zero', '--pulse-worker': 'pulse',
                    '--disconnect-worker': 'disconnect'}
    mode = worker_modes.get(command, command)
    operations = {'zero': (bench_zero, '是', '--worker'),
                  'pulse': (bench_vibration, '震动1', '--pulse-worker'),
                  'disconnect': (bench_disconnect, '断连1', '--disconnect-worker')}
    operation, required_confirmation, worker_argument = operations[mode]
    worker_mode = command in worker_modes
    if not worker_mode and mode != 'zero' and not nonzero_bench_allowed():
        print(json.dumps(blocked_nonzero_result(), ensure_ascii=False, indent=2))
        return
    if worker_mode:
        try:
            authorization = json.load(sys.stdin)
            if not isinstance(authorization, dict) or authorization.get('confirmation') != required_confirmation:
                result = {'status': 'confirmation_required', 'device_state': 'unknown'}
            elif mode != 'zero' and not nonzero_bench_allowed():
                result = blocked_nonzero_result()
            else:
                options = {'on_disconnected': observation_signals} if mode == 'disconnect' else {}
                result = asyncio.run(operation(load_binding(), required_confirmation, **options))
        except (OSError, ValueError) as exc:
            result = {'status': 'preflight_error', 'device_state': 'unknown',
                      'error_type': type(exc).__name__}
    else:
        print(QUESTION)
        if mode == 'pulse':
            print('本次仅单路震动编码值1/100，名义0.25秒，随后归零。现场保持电源键可触及。')
        elif mode == 'disconnect':
            print('本次在最低震动值1启动后断开BLE，观察设备是否自行停止。')
            print('长音表示本程序GATT会话已关闭，从长音开始观察；短音开始即观察结束，请关机。')
            print('最多观察2秒。仍在动作时立即物理关机，观察结束后也请关机。')
        confirmation = input(f'输入“{required_confirmation}”授权本次测试，其他输入退出。\n').strip()
        if confirmation != required_confirmation:
            result = {'status': 'cancelled', 'device_state': 'unknown'}
        else:
            try:
                process = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()),
                     worker_argument],
                    input=json.dumps({'confirmation': confirmation}),
                    capture_output=True, encoding='utf-8', timeout=35, check=False)
                result = json.loads(process.stdout) if process.returncode == 0 else {'status': 'unknown'}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                result = {'status': 'unknown', 'device_state': 'unknown'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if worker_mode:
        return
    if result['status'] == 'unknown':
        print('执行结果未知。请现场关闭设备电源。')
    elif result['status'] == 'link_closed_observation_required':
        print('本次断链测试的提示音观察期已结束。请确认设备电源已经关闭。')


if __name__ == '__main__':
    main()
