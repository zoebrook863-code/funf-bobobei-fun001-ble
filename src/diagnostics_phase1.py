"""Phase-one disconnect forensics: passive observation and close timing.

Two read-only tools and one parent orchestrator for the reviewed
phase-one diagnostic procedure.

* observe worker   - passive advertisements only, never connects.
* close worker     - connects, discovers GATT, writes nothing, holds, then
                     closes by one of five methods (A-E).
* measure          - parent orchestration on one monotonic clock, emitting
                     only sanitized results. Raw logs stay in private/.

The parent refuses to run without the on-site confirmation phrase. Nothing
here unlocks real nonzero writes; device_safety.py is not touched.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

from ble_readonly import load_binding, verified_write_characteristic
from local_runtime import secure_directory

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS = ROOT / 'private' / 'diagnostics'
CONFIRMATION = '断连测量'
MODES = ('A', 'B', 'C', 'D', 'E')
MODE_LABELS = {
    'A': '关闭会话，丢弃全部引用并回收，进程再存活 60 秒',
    'B': '关闭会话，保留特征和客户端引用，进程再存活 60 秒',
    'C': '关闭会话后进程立即退出',
    'D': '不关闭会话，进程正常退出',
    'E': '连接保持期间由父进程强制终止',
}
ON_SITE = ('设备已取下、放在桌面、未接触身体、附近无人使用、电源键伸手可及，'
           '手机 FUNF 已强行停止且其他 BLE 工具均未运行？')


def rssi_bucket(rssi):
    if type(rssi) not in (int, float):
        return 'unknown'
    if rssi >= -60:
        return 'near'
    if rssi >= -75:
        return 'mid'
    if rssi >= -90:
        return 'far'
    return 'edge'


def _emit_event(event, **fields):
    payload = {'event': event, **fields}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


class RawAdvertisementLog:
    """Private JSONL log; the only place the bound address may appear."""

    def __init__(self, path, secure=secure_directory):
        self.path = Path(path)
        secure(self.path.parent)
        self.file = self.path.open('a', encoding='utf-8')

    def write(self, record):
        self.file.write(json.dumps(record, ensure_ascii=False) + '\n')
        self.file.flush()

    def close(self):
        self.file.close()


async def observe_worker(address, seconds, raw_path, *, clock=time.monotonic,
                         scanner_type=None, emit=_emit_event, secure=secure_directory):
    """Passive only: a detection callback, never a connect or a write."""
    if scanner_type is None:
        from bleak import BleakScanner
        scanner_type = BleakScanner
    raw = RawAdvertisementLog(raw_path, secure)
    last = None
    events = 0

    def detection(device, advertisement):
        nonlocal last, events
        if device.address.upper() != address:
            return
        now = clock()
        rssi = getattr(advertisement, 'rssi', None)
        interval = None if last is None else round((now - last) * 1000, 1)
        last = now
        events += 1
        raw.write({'t': now, 'address': device.address, 'rssi': rssi,
                   'interval_ms': interval})
        emit('advertisement', t=round(now, 6), rssi_bucket=rssi_bucket(rssi),
             interval_ms=interval)

    scanner = scanner_type(detection_callback=detection)
    try:
        await scanner.start()
    except Exception as exc:
        emit('error', error_type=type(exc).__name__)
        raw.close()
        return
    emit('observer_started', t=round(clock(), 6))
    try:
        if seconds:
            await asyncio.sleep(seconds)
        else:
            await asyncio.Event().wait()
    finally:
        try:
            await scanner.stop()
        finally:
            raw.close()
            emit('observer_stopped', t=round(clock(), 6), advertisements=events)


async def close_worker(address, mode, hold, *, clock=time.monotonic,
                       sleep=asyncio.sleep, scanner_type=None, client_type=None,
                       emit=_emit_event, exit_process=os._exit, release_sleep=60):
    """Connect and close by one of the five methods. No GATT writes at all."""
    if mode not in MODES:
        emit('error', error_type='invalid_mode')
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
    emit('connected', t=round(clock(), 6), mode=mode)
    write = None
    try:
        write = verified_write_characteristic(client.services)
    except Exception:
        write = None
    emit('gatt_checked', t=round(clock(), 6), write_characteristic=write is not None,
         writes_attempted=0)
    await sleep(hold)
    if mode == 'D':
        emit('closed', t=round(clock(), 6), via='process_exit_without_disconnect',
             writes_attempted=0)
        exit_process(0)
        return
    try:
        await client.disconnect()
        emit('closed', t=round(clock(), 6), application_gatt_session_closed=True,
             is_connected=client.is_connected, writes_attempted=0)
    except Exception as exc:
        emit('error', error_type=type(exc).__name__, t=round(clock(), 6),
             writes_attempted=0)
        return
    if mode == 'A':
        del write, device, client
        gc.collect()
        emit('references_released', t=round(clock(), 6))
        await sleep(release_sleep)
    elif mode == 'B':
        await sleep(release_sleep)
    else:
        exit_process(0)
        return
    emit('worker_done', t=round(clock(), 6))


class WorkerResult:
    def __init__(self, mode, connected_at, closed_at, exit_at, gatt_ok,
                 killed, error_type):
        self.mode = mode
        self.connected_at = connected_at
        self.closed_at = closed_at
        self.exit_at = exit_at
        self.gatt_ok = gatt_ok
        self.killed = killed
        self.error_type = error_type


class WorkerRunner:
    """Run one close worker in a child process so exit timing is real."""

    def __init__(self, address, *, python=sys.executable, clock=time.monotonic,
                 sleep=time.sleep):
        self.address = address
        self.python = python
        self.clock = clock
        self.sleep = sleep

    def run(self, mode, hold, kill_after=None):
        process = subprocess.Popen(
            [self.python, str(Path(__file__).resolve()), '--close-worker', mode,
             '--hold', str(hold)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding='utf-8', errors='replace')
        events = []
        lock = threading.Lock()

        def read():
            for line in process.stdout:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        killed = False
        if kill_after is not None:
            deadline = self.clock() + 35
            while self.clock() < deadline and process.poll() is None:
                with lock:
                    if any(event.get('event') == 'connected' for event in events):
                        break
                self.sleep(0.02)
            self.sleep(kill_after)
            if process.poll() is None:
                process.kill()
                killed = True
        try:
            process.wait(timeout=hold + 90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            killed = True
        exit_at = self.clock()
        reader.join(timeout=2)
        with lock:
            captured = list(events)

        def timestamp(name):
            found = [event.get('t') for event in captured if event.get('event') == name]
            return found[0] if found else None

        error = next((event.get('error_type') for event in captured
                      if event.get('event') == 'error'), None)
        gatt = next((event.get('write_characteristic') for event in captured
                     if event.get('event') == 'gatt_checked'), None)
        return WorkerResult(mode, timestamp('connected'), timestamp('closed'),
                            exit_at, bool(gatt), killed, error)


class ObserverProcess:
    """Parent-side handle on the passive observer child process."""

    def __init__(self, raw_path, *, seconds=0, python=sys.executable,
                 clock=time.monotonic, sleep=time.sleep):
        self.raw_path = Path(raw_path)
        self.seconds = seconds
        self.python = python
        self.clock = clock
        self.sleep = sleep
        self.process = None
        self.thread = None
        self.events = []
        self.lock = threading.Lock()

    def start(self):
        self.process = subprocess.Popen(
            [self.python, str(Path(__file__).resolve()), '--observe-worker',
             '--raw', str(self.raw_path), '--seconds', str(self.seconds)],
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
        deadline = self.clock() + 15
        while self.clock() < deadline:
            with self.lock:
                started = any(event.get('event') == 'observer_started'
                              for event in self.events)
                failed = next((event for event in self.events
                               if event.get('event') == 'error'), None)
            if started:
                return self
            if failed is not None:
                self.stop()
                raise RuntimeError('Observer failed: ' + str(failed.get('error_type')))
            if self.process.poll() is not None:
                raise RuntimeError('Observer exited before startup')
            self.sleep(0.02)
        self.stop()
        raise RuntimeError('Observer startup timed out')

    def advertisements(self):
        with self.lock:
            return [event.copy() for event in self.events
                    if event.get('event') == 'advertisement']

    def wait_for_advertisement(self, after, timeout):
        deadline = self.clock() + timeout
        while True:
            found = [event['t'] for event in self.advertisements()
                     if isinstance(event.get('t'), (int, float)) and event['t'] > after]
            if found:
                return min(found)
            if self.clock() >= deadline:
                return None
            if self.process is not None and self.process.poll() is not None:
                return None
            self.sleep(0.02)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.thread is not None:
            self.thread.join(timeout=2)


def observation_summary(events, seconds):
    advertisements = [event for event in events
                      if event.get('event') == 'advertisement']
    intervals = [event['interval_ms'] for event in advertisements
                 if isinstance(event.get('interval_ms'), (int, float))]
    buckets = {}
    for event in advertisements:
        bucket = event.get('rssi_bucket', 'unknown')
        buckets[bucket] = buckets.get(bucket, 0) + 1
    maximum = max(intervals) if intervals else None
    return {'status': 'observed' if advertisements else 'no_advertising',
            'observed_seconds': seconds,
            'advertisement_count': len(advertisements),
            'median_interval_ms': round(statistics.median(intervals), 1)
            if intervals else None,
            'max_interval_ms': maximum,
            'rssi_buckets': buckets,
            'continuous': len(advertisements) >= 2
            and (maximum is None or maximum <= 5000)}


class MeasurementSession:
    def __init__(self, *, observer, runner, clock=time.monotonic, sleep=time.sleep,
                 settle=10, observation_cap=90, hold=20):
        self.observer = observer
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.settle = settle
        self.observation_cap = observation_cap
        self.hold = hold

    def _baseline(self, seconds):
        start = self.clock()
        self.sleep(seconds)
        end = self.clock()
        events = [event for event in self.observer.advertisements()
                  if isinstance(event.get('t'), (int, float))
                  and start <= event['t'] <= end]
        intervals = [event['interval_ms'] for event in events
                     if isinstance(event.get('interval_ms'), (int, float))]
        result = {'advertisement_count': len(events),
                  'median_interval_ms': round(statistics.median(intervals), 1)
                  if intervals else None,
                  'max_interval_ms': max(intervals) if intervals else None}
        result['continuous'] = len(events) >= 2 and (
            result['max_interval_ms'] is None or result['max_interval_ms'] <= 5000)
        return result

    def _calibrate(self):
        result = self.runner.run('C', self.hold)
        if result.error_type is not None or result.connected_at is None:
            return {'status': 'calibration_error',
                    'error_type': result.error_type or 'not_connected'}
        if result.closed_at is None:
            return {'status': 'calibration_error', 'error_type': 'close_not_observed'}
        during = [event for event in self.observer.advertisements()
                  if result.connected_at < event['t'] < result.closed_at]
        return {'status': 'calibration_ok' if not during else 'calibration_failed',
                'advertisements_while_connected': len(during)}

    def _run_once(self, mode, repeat):
        record = {'mode': mode, 'repeat': repeat, 'resume': 'not_observed'}
        if self.observer.wait_for_advertisement(self.clock() - 5, 10) is None:
            record['error_type'] = 'device_not_advertising'
            return record
        result = self.runner.run(mode, self.hold, kill_after=2 if mode == 'E' else None)
        record.update({
            'connected': result.connected_at is not None,
            'write_characteristic': result.gatt_ok,
            'writes_attempted': 0,
            'killed': result.killed,
        })
        if result.error_type is not None or result.connected_at is None:
            record['error_type'] = result.error_type or 'not_connected'
            return record
        reference = result.exit_at if mode == 'E' else result.closed_at
        if reference is None:
            record['error_type'] = 'close_not_observed'
            return record
        if result.closed_at is not None:
            record['close_to_exit_ms'] = round(
                (result.exit_at - result.closed_at) * 1000)
        resume_at = self.observer.wait_for_advertisement(reference,
                                                         self.observation_cap)
        if resume_at is None:
            record['resume'] = f'>{self.observation_cap}s'
            return record
        record['resume'] = 'observed'
        record['reference'] = 'exit' if mode == 'E' else 'close'
        record['reference_to_resume_ms'] = round((resume_at - reference) * 1000)
        record['exit_to_resume_ms'] = round((resume_at - result.exit_at) * 1000)
        return record

    def run(self, *, modes=MODES, repeats=3, baseline_seconds=60):
        self.observer.start()
        try:
            baseline = self._baseline(baseline_seconds)
            if not baseline['continuous']:
                return {'status': 'no_advertising', 'baseline': baseline}
            calibration = self._calibrate()
            if calibration['status'] != 'calibration_ok':
                return {'status': 'calibration_failed', 'baseline': baseline,
                        'calibration': calibration}
            runs = []
            for mode in modes:
                for repeat in range(1, repeats + 1):
                    runs.append(self._run_once(mode, repeat))
                    self.sleep(self.settle)
            return {'status': 'complete', 'baseline': baseline,
                    'calibration': calibration, 'runs': runs,
                    'summary': summarize(runs)}
        finally:
            self.observer.stop()


def summarize(runs):
    summary = {}
    for mode in MODES:
        rows = [run for run in runs if run['mode'] == mode
                and run['resume'] == 'observed'
                and isinstance(run.get('reference_to_resume_ms'), int)]
        values = sorted(row['reference_to_resume_ms'] for row in rows)
        summary[mode] = {'observed': len(values),
                         'min_ms': values[0] if values else None,
                         'median_ms': round(statistics.median(values)) if values else None,
                         'max_ms': values[-1] if values else None}
    observed = {mode: summary[mode]['median_ms'] for mode in MODES
                if summary[mode]['median_ms'] is not None}
    hint = 'insufficient_data'
    if observed:
        if all(value <= 1000 for value in observed.values()):
            hint = 'all_within_1s'
        elif all(value > 2000 for value in observed.values()):
            hint = 'all_slow_over_2s'
        elif ('A' in observed and 'C' in observed and 'B' in observed
              and max(observed['A'], observed['C']) <= 2000
              and observed['B'] > max(observed['A'], observed['C']) + 1000):
            hint = 'reference_release_delays_disconnect'
        else:
            hint = 'mixed'
    return {'per_mode': summary, 'reading_hint': hint}


def measure_command(arguments):
    print(ON_SITE)
    print('本流程只连接和关闭，不写任何 GATT 特征，不订阅通知。')
    for mode in MODES:
        print(f'  {mode}: {MODE_LABELS[mode]}')
    print('每种关闭方式 3 次，单次观察上限 90 秒；设备有任何动作请立即物理关机。')
    try:
        confirmation = input(f'输入“{CONFIRMATION}”开始，其他输入退出。\n').strip()
    except EOFError:
        confirmation = ''
    if confirmation != CONFIRMATION:
        print(json.dumps({'status': 'cancelled'}, ensure_ascii=False, indent=2))
        return 1
    address = load_binding()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    raw_path = Path(arguments.raw_dir) / f'observer-{stamp}.jsonl'
    observer = ObserverProcess(raw_path)
    runner = WorkerRunner(address)
    session = MeasurementSession(observer=observer, runner=runner,
                                 hold=arguments.hold)
    modes = tuple(arguments.modes)
    try:
        report = session.run(modes=modes, repeats=arguments.repeats,
                             baseline_seconds=arguments.baseline)
    except RuntimeError as exc:
        report = {'status': 'observer_error', 'error_type': str(exc)}
    except KeyboardInterrupt:
        report = {'status': 'interrupted'}
    report = {'status_scope': 'phase1_read_only_disconnect_timing',
              'raw_log': 'private/diagnostics (not printed)',
              **report}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('status') == 'complete' else 1


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('observe', 'measure'))
    parser.add_argument('--observe-worker', action='store_true')
    parser.add_argument('--close-worker', choices=MODES)
    parser.add_argument('--raw', type=Path)
    parser.add_argument('--raw-dir', type=Path, default=DIAGNOSTICS)
    parser.add_argument('--seconds', type=float, default=0)
    parser.add_argument('--hold', type=float, default=20)
    parser.add_argument('--baseline', type=float, default=60)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--modes', default=''.join(MODES))
    arguments = parser.parse_args()
    try:
        if arguments.observe_worker:
            path = arguments.raw or arguments.raw_dir / 'observer-unknown.jsonl'
            asyncio.run(observe_worker(load_binding(), arguments.seconds, path))
            return 0
        if arguments.close_worker:
            asyncio.run(close_worker(load_binding(), arguments.close_worker,
                                     arguments.hold))
            return 0
        if arguments.action == 'observe':
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            seconds = arguments.seconds or 60
            observer = ObserverProcess(
                arguments.raw_dir / f'observer-manual-{stamp}.jsonl',
                seconds=seconds)
            try:
                observer.start()
                try:
                    observer.process.wait()
                except KeyboardInterrupt:
                    observer.stop()
            except RuntimeError as exc:
                print(json.dumps({'status': 'observer_error',
                                  'error_type': str(exc)}, ensure_ascii=False))
                return 1
            report = {'scope': 'passive_broadcast_only_never_connected',
                      'raw_log': 'private/diagnostics (not printed)',
                      **observation_summary(observer.advertisements(), seconds)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['status'] == 'observed' else 1
        if arguments.action == 'measure':
            modes = tuple(character.upper() for character in arguments.modes)
            if not modes or any(mode not in MODES for mode in modes):
                parser.error('--modes must be a subset of ABCDE')
            if arguments.repeats < 1 or arguments.repeats > 5:
                parser.error('--repeats must be between 1 and 5')
            arguments.modes = modes
            return measure_command(arguments)
        parser.error('choose observe or measure')
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'preflight_error',
                          'error_type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
