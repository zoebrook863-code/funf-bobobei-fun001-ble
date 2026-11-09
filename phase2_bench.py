"""Phase-2 planned-disconnect test: exactly one verified lowest-value pulse.

Owner-approved and owner-present only. Writes the zero barrier, one
vibration / mode 1 / value 1 pulse, waits one second, disconnects, and keeps
the process alive for a 10-second observation window. It never writes zero
after the pulse: the point is to observe the firmware after the physical
disconnect. The owner watches the device; if it is still moving at the cap,
the owner powers it off. The narrow device_safety gate is reverted after the
test.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from ble_readonly import load_binding, verified_write_characteristic
from device_safety import phase2_disconnect_allowed
from diagnostics_phase1 import DIAGNOSTICS, ON_SITE, ObserverProcess
from fun001_protocol import build_control_frame, build_stop_frames

CONFIRMATION = '第二阶段1'
HOLD = 1.0
OBSERVE = 10.0


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def beep(frequency, duration):
    try:
        import winsound
        winsound.Beep(frequency, duration)
    except Exception:
        pass


async def phase2_worker(address, *, hold=HOLD, observe=OBSERVE,
                        clock=time.monotonic, sleep=asyncio.sleep,
                        scanner_type=None, client_type=None, emit=emit,
                        allowed=phase2_disconnect_allowed):
    if not allowed():
        emit('error', error_type='phase2_not_approved',
             writes_attempted=0, nonzero_attempts=0)
        return
    if scanner_type is None or client_type is None:
        from bleak import BleakClient, BleakScanner
        scanner_type = scanner_type or BleakScanner
        client_type = client_type or BleakClient
    device = await scanner_type.find_device_by_filter(
        lambda device, advertisement: device.address.upper() == address, timeout=10)
    if device is None:
        emit('error', error_type='bound_device_not_found', t=round(clock(), 6))
        return
    client = client_type(device, pair=False, timeout=15,
                         winrt={'use_cached_services': False})
    try:
        await client.connect()
    except Exception as exc:
        emit('error', error_type=type(exc).__name__, t=round(clock(), 6))
        return
    emit('connected', t=round(clock(), 6))
    try:
        write = verified_write_characteristic(client.services)
    except Exception:
        write = None
    if write is None:
        emit('error', error_type='gatt_mismatch', t=round(clock(), 6))
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    zero, = build_stop_frames()
    try:
        await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
        emit('zero_barrier_ack', t=round(clock(), 6), writes_attempted=1)
    except Exception as exc:
        emit('error', error_type=type(exc).__name__, t=round(clock(), 6),
             writes_attempted=1, nonzero_attempts=0)
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    pulse = build_control_frame('vibration', 1, mode=1, max_intensity=1)
    try:
        await asyncio.wait_for(client.write_gatt_char(write, pulse, response=True), 3)
        emit('pulse_ack', t=round(clock(), 6), writes_attempted=2,
             nonzero_attempts=1)
    except Exception as exc:
        emit('error', error_type=type(exc).__name__, t=round(clock(), 6),
             writes_attempted=2, nonzero_attempts=1)
        try:
            await asyncio.wait_for(client.write_gatt_char(write, zero, response=True), 3)
            emit('recovery_zero_ack', t=round(clock(), 6))
        except Exception:
            emit('recovery_zero_unknown', t=round(clock(), 6))
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    await sleep(hold)
    emit('disconnecting', t=round(clock(), 6))
    try:
        await client.disconnect()
        emit('disconnected', t=round(clock(), 6), is_connected=client.is_connected)
    except Exception as exc:
        emit('error', error_type=type(exc).__name__, t=round(clock(), 6))
        return
    await sleep(observe)
    emit('worker_done', t=round(clock(), 6))


class WorkerProcess:
    def __init__(self, *, python=sys.executable, hold=HOLD, observe=OBSERVE):
        self.python = python
        self.hold = hold
        self.observe = observe
        self.process = None
        self.thread = None
        self.events = []
        self.lock = threading.Lock()

    def start(self):
        self.process = subprocess.Popen(
            [self.python, str(Path(__file__).resolve()), '--worker',
             '--hold', str(self.hold), '--observe', str(self.observe)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding='utf-8', errors='replace')

        def read():
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.events.append(event)

        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        return self

    def find(self, name):
        with self.lock:
            for event in self.events:
                if event.get('event') == name:
                    return event.copy()
        return None

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        if self.alive():
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2)


async def wait_event(process, name, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = process.find(name)
        if event is not None:
            return event
        if not process.alive():
            return None
        await asyncio.sleep(0.02)
    return None


async def phase2_async(address, observer, worker):
    report = {
        'status_scope': 'phase2_planned_disconnect',
        'tool_version': '2026-11-09-phase2-disconnect',
        'nonzero_attempts': 0,
        'writes_attempted': 0,
        'zero_barrier_ack': False,
        'pulse_ack': False,
        'disconnected': False,
        't_resume_ms': None,
        'observation_window_s': OBSERVE,
        'device_state': 'unknown',
        'physical_stop_verified': False,
    }
    observer.start()
    try:
        worker.start()
        pulse = await wait_event(worker, 'pulse_ack', 40)
        if pulse is None:
            error = worker.find('error')
            report['status'] = 'phase2_not_started'
            if error is not None:
                report['error_type'] = error.get('error_type')
                report['writes_attempted'] = error.get('writes_attempted', 0)
                report['nonzero_attempts'] = error.get('nonzero_attempts', 0)
            return report
        report['zero_barrier_ack'] = worker.find('zero_barrier_ack') is not None
        report['pulse_ack'] = True
        report['writes_attempted'] = 2
        report['nonzero_attempts'] = 1
        beep(1100, 300)
        disconnected = await wait_event(worker, 'disconnected', 10)
        if disconnected is None:
            report['status'] = 'disconnect_not_observed'
            error = worker.find('error')
            if error is not None:
                report['error_type'] = error.get('error_type')
            return report
        report['disconnected'] = True
        resume = observer.wait_for_advertisement(disconnected['t'], 90)
        if resume is None:
            report['status'] = 'link_release_not_observed'
            return report
        report['t_resume_ms'] = round((resume - disconnected['t']) * 1000)
        beep(1500, 150)
        done = await wait_event(worker, 'worker_done', 20)
        report['observation_completed'] = done is not None
        beep(900, 500)
        report['status'] = 'complete'
        return report
    finally:
        worker.close()
        observer.stop()


def run_command(arguments):
    print(ON_SITE)
    print('本次只写一次 vibration / mode 1 / 值 1，保持 1 秒，然后断开。')
    print('断开后不再写零帧，专门观察固件会不会自己停。观察窗口 10 秒。')
    print('会听到三声提示：开始、物理断链、观察结束。设备全程在桌面，不上身。')
    print('观察结束时如果设备仍在动作，立即关闭电源。')
    try:
        confirmation = input(f'输入“{CONFIRMATION}”开始，其他输入退出。\n').strip()
    except EOFError:
        confirmation = ''
    if confirmation != CONFIRMATION:
        print(json.dumps({'status': 'cancelled'}, ensure_ascii=False, indent=2))
        return 1
    address = load_binding()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    observer = ObserverProcess(
        Path(arguments.raw_dir) / f'phase2-{stamp}.jsonl')
    worker = WorkerProcess(hold=arguments.hold, observe=arguments.observe)
    try:
        report = asyncio.run(phase2_async(address, observer, worker))
    except KeyboardInterrupt:
        report = {'status': 'interrupted'}
    except RuntimeError as exc:
        report = {'status': 'observer_error', 'error_type': str(exc)}
    report = {'raw_log': 'private/diagnostics (not printed)', **report}
    if report.get('status') == 'complete':
        print('观察窗口结束。如果设备仍在动作，请立即关闭设备电源。')
        try:
            observation = input(
                '设备在断链后是自行停止，还是仍在动？输入 自行停止 / 还在动 / 不确定。\n').strip()
        except EOFError:
            observation = ''
        if observation == '自行停止':
            report['owner_report'] = 'stopped_after_disconnect'
            report['firmware_disconnect_stop'] = 'stopped_after_disconnect'
            report['physical_stop_verified'] = True
            try:
                timing = input(
                    '大约在物理断链后多久停止？输入 立即 / 1-3秒 / 3-10秒 / 不确定。\n').strip()
            except EOFError:
                timing = ''
            report['stop_timing_after_link_release'] = timing or 'unknown'
        elif observation == '还在动':
            print('请立即关闭设备电源。')
            report['owner_report'] = 'still_running_at_cap'
            report['firmware_disconnect_stop'] = 'still_running_at_cap'
            try:
                input('输入“已关机”确认。\n')
                report['manual_power_off_confirmed'] = True
            except EOFError:
                report['manual_power_off_confirmed'] = False
        else:
            report['owner_report'] = 'unknown'
            report['firmware_disconnect_stop'] = 'unknown'
            print('无法判定。请关闭设备电源。')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('status') == 'complete' else 1


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('phase2',))
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--hold', type=float, default=HOLD)
    parser.add_argument('--observe', type=float, default=OBSERVE)
    parser.add_argument('--raw-dir', type=Path, default=DIAGNOSTICS)
    arguments = parser.parse_args()
    try:
        if arguments.worker:
            asyncio.run(phase2_worker(load_binding(), hold=arguments.hold,
                                      observe=arguments.observe))
            return 0
        if arguments.action == 'phase2' or arguments.action is None:
            return run_command(arguments)
        parser.error('choose phase2')
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'preflight_error',
                          'error_type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
