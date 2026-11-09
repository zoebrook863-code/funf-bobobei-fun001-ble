"""Sentinel feasibility test: one stop frame, no nonzero command.

Reviewed phase-one sentinel feasibility procedure.

1. a primary child connects and holds the device;
2. the parent opens a second application session to the same device;
3. while both sessions are open the device must stop advertising, proving
   one shared physical link;
4. the parent hard-kills the primary child;
5. the backup session writes the already-observed stop frame exactly once
   and must receive an ATT write response;
6. the backup holds the link for a few seconds, then closes.

A successful run means a sentinel process could plausibly cover the
measured ~14 s crash window. It does not prove that the device physically
stopped: device_state stays unknown and physical_stop_verified stays false.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from ble_readonly import load_binding, verified_write_characteristic
from diagnostics_phase1 import DIAGNOSTICS, ON_SITE, ObserverProcess
from fun001_protocol import SERVICE_UUID, build_stop_frames

CONFIRMATION = '哨兵1'
DEFAULT_HOLD = 10


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


async def primary_worker(address, hold, *, clock=time.monotonic,
                         sleep=asyncio.sleep, scanner_type=None, client_type=None,
                         emit=emit):
    """Connect, discover GATT, hold. Never writes and never subscribes."""
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
        emit('gatt_checked', t=round(clock(), 6),
             write_characteristic=write is not None, writes_attempted=0)
    except Exception:
        emit('gatt_checked', t=round(clock(), 6),
             write_characteristic=False, writes_attempted=0)
    await sleep(hold)
    try:
        await client.disconnect()
    except Exception:
        pass
    emit('closed', t=round(clock(), 6), writes_attempted=0)


class PrimaryProcess:
    def __init__(self, *, python=sys.executable, hold=60, clock=time.monotonic):
        self.python = python
        self.hold = hold
        self.clock = clock
        self.process = None
        self.thread = None
        self.events = []
        self.lock = threading.Lock()

    def start(self):
        self.process = subprocess.Popen(
            [self.python, str(Path(__file__).resolve()), '--primary-worker',
             '--hold', str(self.hold)],
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

    def kill(self):
        killed = False
        if self.alive():
            self.process.kill()
            killed = True
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return killed

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


def sentinel_verdict(report):
    return bool(report.get('p2_connected')
                and report.get('p2_write_characteristic')
                and report.get('ads_during_dual_connection') == 0
                and report.get('p1_killed')
                and report.get('zero_ack_received')
                and report.get('ads_during_backup_hold_after_kill') == 0
                and report.get('backup_still_connected'))


async def sentinel_async(address, hold, observer, device, client_type=None,
                         primary=None, sleep=asyncio.sleep, backup_cached=True):
    report = {
        'status_scope': 'phase1_sentinel_feasibility',
        'report_version': '2026-11-08-cached-services',
        'writes_attempted': 0,
        'nonzero_attempts': 0,
        'device_state': 'unknown',
        'physical_stop_verified': False,
        'p1_connected': False,
        'p2_connected': False,
        'p2_write_characteristic': False,
        'p2_service_count': None,
        'p2_characteristic_count': None,
        'p2_has_fun001_service': False,
        'p2_cached_services': backup_cached,
        'p1_gatt_checked': False,
        'p1_killed': False,
        'zero_ack_received': False,
        'backup_still_connected': False,
        'sentinel_feasible': False,
    }
    if client_type is None:
        from bleak import BleakClient
        client_type = BleakClient
    primary = primary or PrimaryProcess(hold=max(60, hold + 30))
    backup = None
    try:
        observer.start()
        primary.start()
        connected = await wait_event(primary, 'connected', 30)
        if connected is None:
            report['status'] = 'p1_not_connected'
            return report
        report['p1_connected'] = True
        # The primary must finish its service discovery first: the backup
        # relies on the Windows GATT cache populated by that discovery.
        checked = await wait_event(primary, 'gatt_checked', 20)
        report['p1_gatt_checked'] = bool(checked and checked.get('write_characteristic'))
        if not report['p1_gatt_checked']:
            report['status'] = 'p1_gatt_not_ready'
            return report
        backup = client_type(device, pair=False, timeout=15,
                             winrt={'use_cached_services': backup_cached})
        try:
            await asyncio.wait_for(backup.connect(), timeout=20)
        except Exception as exc:
            report['status'] = 'p2_connect_failed'
            report['error_type'] = type(exc).__name__
            return report
        report['p2_connected'] = True
        await sleep(3)
        dual_start = connected['t'] + 1.0
        dual_end = time.monotonic()
        dual_ads = [event for event in observer.advertisements()
                    if dual_start < event['t'] < dual_end]
        report['ads_during_dual_connection'] = len(dual_ads)
        if dual_ads:
            report['status'] = 'shared_link_not_observed'
            return report

        report['p1_killed'] = primary.kill()
        kill_at = time.monotonic()
        await sleep(1)
        if not backup.is_connected:
            report['status'] = 'backup_link_lost_after_kill'
            return report
        # A second session may not be allowed to enumerate GATT while the
        # primary still owns the device. Discover only after the kill.
        try:
            services = backup.services
            write = verified_write_characteristic(services)
            report['p2_service_count'] = len(services.services)
            report['p2_characteristic_count'] = len(services.characteristics)
            report['p2_has_fun001_service'] = (
                services.get_service(SERVICE_UUID) is not None)
            try:
                report['p2_service_uuids'] = sorted(
                    service.uuid for service in services.services.values())
            except Exception:
                pass
        except Exception as exc:
            write = None
            report['p2_discovery_error_type'] = type(exc).__name__
        report['p2_write_characteristic'] = write is not None
        if write is None:
            report['status'] = 'p2_write_characteristic_missing_after_kill'
            return report

        frame, = build_stop_frames()
        report['writes_attempted'] = 1
        try:
            await asyncio.wait_for(
                backup.write_gatt_char(write, frame, response=True), timeout=3)
            report['zero_ack_received'] = True
        except Exception as exc:
            report['status'] = 'zero_write_unknown'
            report['error_type'] = type(exc).__name__
            return report

        await sleep(hold)
        hold_start = kill_at + 0.5
        hold_ads = [event for event in observer.advertisements()
                    if hold_start < event['t'] < time.monotonic()]
        report['ads_during_backup_hold_after_kill'] = len(hold_ads)
        report['backup_still_connected'] = bool(backup.is_connected)
        report['sentinel_feasible'] = sentinel_verdict(report)
        report['status'] = 'complete' if report['sentinel_feasible'] else 'backup_did_not_hold_link'
        return report
    finally:
        # Kill the primary first: if the run aborted before that point, a
        # lingering primary would keep the device connected and make the
        # backup-close resume measurement meaningless.
        primary.close()
        if backup is not None:
            try:
                closed_at = time.monotonic()
                await asyncio.wait_for(backup.disconnect(), timeout=5)
                resume = observer.wait_for_advertisement(closed_at, 90)
                report['backup_close_to_resume_ms'] = (
                    round((resume - closed_at) * 1000) if resume is not None else None)
                report['resume_after_backup_close'] = (
                    'observed' if resume is not None else '>90s')
            except Exception as exc:
                report['backup_close_error_type'] = type(exc).__name__
        observer.stop()


def run_command(arguments):
    print(ON_SITE)
    print('本测试会连接两次，并在主进程被强杀后写一次已取证的停止帧（零值）。')
    print('不会发送任何非零命令，不订阅通知。设备应全程静止。')
    try:
        confirmation = input(f'输入“{CONFIRMATION}”开始，其他输入退出。\n').strip()
    except EOFError:
        confirmation = ''
    if confirmation != CONFIRMATION:
        print(json.dumps({'status': 'cancelled'}, ensure_ascii=False, indent=2))
        return 1
    address = load_binding()

    async def run():
        from bleak import BleakClient, BleakScanner
        device = await BleakScanner.find_device_by_filter(
            lambda device, advertisement: device.address.upper() == address, timeout=10)
        if device is None:
            return {'status': 'bound_device_not_found'}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        observer = ObserverProcess(
            Path(arguments.raw_dir) / f'sentinel-{stamp}.jsonl')
        return await sentinel_async(address, arguments.hold, observer, device,
                                    client_type=BleakClient,
                                    backup_cached=not arguments.fresh_backup)

    try:
        report = asyncio.run(run())
    except KeyboardInterrupt:
        report = {'status': 'interrupted'}
    except RuntimeError as exc:
        report = {'status': 'observer_error', 'error_type': str(exc)}
    report = {'raw_log': 'private/diagnostics (not printed)', **report}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('sentinel_feasible') else 1


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('sentinel',))
    parser.add_argument('--primary-worker', action='store_true')
    parser.add_argument('--hold', type=float, default=DEFAULT_HOLD)
    parser.add_argument('--raw-dir', type=Path, default=DIAGNOSTICS)
    parser.add_argument('--fresh-backup', action='store_true',
                        help='force a fresh GATT discovery in the backup session')
    arguments = parser.parse_args()
    try:
        if arguments.primary_worker:
            asyncio.run(primary_worker(load_binding(), arguments.hold))
            return 0
        if arguments.action == 'sentinel' or arguments.action is None:
            return run_command(arguments)
        parser.error('choose sentinel')
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'preflight_error',
                          'error_type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
