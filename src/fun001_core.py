"""Real local FUN-001 safety core over one Windows BLE link.

The core mirrors mock_control.py semantics but on a real transport:

* one resident process owns one application GATT session;
* every planned exit path writes the observed stop frame and requires an
  ATT acknowledgement before the link is closed;
* results stay layered: gatt_ack is not device confirmation, and an
  uncertain outcome is never replayed;
* idle sleep is held during timed grants or active standing output;
* the owner selects a session function, modes and intensity cap;
* configurations beyond the verified envelope require explicit owner opt-in.

Bleak's ``is_connected`` only reflects the application session. Nothing
here claims a physical radio disconnect; see STOP_GUARANTEE_DIAGNOSIS.
"""
import asyncio
import concurrent.futures
import copy
import ctypes
import gc
import os
import secrets
import threading
import time
from collections import deque

from ble_readonly import verified_write_characteristic
from device_safety import NONZERO_BLOCK_REASON, nonzero_bench_allowed
from fun001_protocol import (FUNCTIONS, MODE_NAMES, build_control_frame,
                             build_state_frames, build_stop_frames)

VERIFIED_ENVELOPE = {'function': 'vibration', 'mode': 1, 'max_intensity': 1}
MAX_GRANT_SECONDS = 1200
SCOPE = 'real_local_ble'
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class ControlRejected(ValueError):
    pass


class UnknownExecution(RuntimeError):
    pass


class LinkUnavailable(RuntimeError):
    pass


def _set_thread_execution_state(flags):
    if os.name != 'nt':
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        pass


class SleepGuard:
    """Keep the calling thread's idle-sleep request set while authorized.

    SetThreadExecutionState is per-thread, so the state is tracked per thread:
    the watchdog thread holds the request while the grant is active and clears
    it on its own thread when the grant ends. This does not change system
    settings and cannot block lid close or a manual sleep request.
    """

    def __init__(self, setter=_set_thread_execution_state):
        self._setter = setter
        self._local = threading.local()

    def engage(self):
        if getattr(self._local, 'active', False):
            return
        self._setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._local.active = True

    def disengage(self):
        if not getattr(self._local, 'active', False):
            return
        self._setter(ES_CONTINUOUS)
        self._local.active = False


class BleLink:
    """Owns one Bleak client on a dedicated asyncio loop thread."""

    def __init__(self, address, *, find_timeout=10, connect_timeout=15,
                 write_timeout=3, disconnect_timeout=5):
        self.address = address
        self.find_timeout = find_timeout
        self.connect_timeout = connect_timeout
        self.write_timeout = write_timeout
        self.disconnect_timeout = disconnect_timeout
        self._lock = threading.RLock()
        self._loop = None
        self._thread = None
        self._client = None
        self._write = None
        self._closed = False
        self._link_lost = False
        self._connect_generation = 0

    @property
    def connected(self):
        return self._client is not None

    def take_link_lost(self):
        lost, self._link_lost = self._link_lost, False
        return lost

    def _on_disconnect(self, client):
        if client is not self._client:
            # A planned close clears _client before calling disconnect().
            return
        self._client = None
        self._write = None
        self._link_lost = True

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _start(self):
        if self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _loop_call(self, coroutine, timeout):
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise LinkUnavailable('link_not_started')
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    async def _connect(self):
        from bleak import BleakClient, BleakScanner
        generation = self._connect_generation
        device = await BleakScanner.find_device_by_filter(
            lambda device, advertisement: device.address.upper() == self.address,
            timeout=self.find_timeout)
        if device is None:
            raise LinkUnavailable('bound_device_not_found')
        client = BleakClient(device, pair=False, timeout=self.connect_timeout,
                             disconnected_callback=self._on_disconnect,
                             winrt={'use_cached_services': False})
        try:
            await client.connect()
            write = verified_write_characteristic(client.services)
            if write is None:
                raise LinkUnavailable('gatt_mismatch')
            if not client.is_connected:
                raise LinkUnavailable('connect_lost')
            if generation != self._connect_generation or self._closed:
                raise LinkUnavailable('connect_cancelled')
        except LinkUnavailable:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise LinkUnavailable(type(exc).__name__) from None
        self._client = client
        self._write = write

    def ensure_connected(self):
        with self._lock:
            if self._closed:
                raise LinkUnavailable('link_closed')
            if self._client is not None:
                return
            self._start()
            try:
                self._loop_call(self._connect(),
                                self.find_timeout + self.connect_timeout + 10)
            except concurrent.futures.TimeoutError:
                self.disconnect()
                raise LinkUnavailable('connect_timeout') from None

    def write(self, frame):
        with self._lock:
            client, write = self._client, self._write
            if client is None or write is None:
                raise LinkUnavailable('link_not_open')
            try:
                self._loop_call(self._write_frame(client, write, frame),
                                self.write_timeout)
            except concurrent.futures.TimeoutError:
                raise UnknownExecution('gatt_write_timeout') from None
            except LinkUnavailable:
                raise
            except Exception as exc:
                raise UnknownExecution(type(exc).__name__) from None

    @staticmethod
    async def _write_frame(client, write, frame):
        # response=True requires an ATT write response before returning.
        await client.write_gatt_char(write, frame, response=True)

    async def _close_client(self, client):
        try:
            await client.disconnect()
        except Exception:
            pass

    def disconnect(self):
        with self._lock:
            self._connect_generation += 1
            client, write = self._client, self._write
            self._client = None
            self._write = None
            self._link_lost = False
            if client is None:
                gc.collect()
                return
            try:
                self._loop_call(self._close_client(client), self.disconnect_timeout)
            except Exception:
                pass
            del write, client
            gc.collect()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.disconnect()
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=3)
        if loop is not None and not loop.is_running():
            loop.close()


class RealSafetyController:
    DEVICE = 'fun001-fun-001'

    def __init__(self, link, clock=time.monotonic, sleep_guard=None,
                 nonzero_allowed=None, output_journal=None):
        self.link = link
        self.clock = clock
        self.sleep_guard = sleep_guard or SleepGuard()
        self._nonzero_allowed = nonzero_allowed or nonzero_bench_allowed
        self.output_journal = output_journal
        self.persistent_authorization = False
        self._lock = threading.RLock()
        self.token = None
        self._stop_requested = threading.Event()
        self.session_deadline = 0
        self.lock_deadline = 0
        self.heartbeat_deadline = 0
        self.command_deadline = None
        self.cap = VERIFIED_ENVELOPE['max_intensity']
        self.function = VERIFIED_ENVELOPE['function']
        self.modes = (VERIFIED_ENVELOPE['mode'],)
        self.active_mode = self.modes[0]
        self.unverified_configuration = False
        self.channel_profiles = {'vibration': {'cap': 1, 'modes': (1,)}}
        self.combined_control_authorized = False
        self.desired_state = {name: {'level': 0, 'mode': 1} for name in FUNCTIONS}
        self.desired_level = 0
        self.result = 'idle'
        self.reason = 'initial'
        self.results = {}
        self.recent_commands = deque()
        self.unknown_latched = False
        self.closed = False

    def nonzero_allowed(self):
        return bool(self._nonzero_allowed())

    def status(self):
        self.tick()
        connected = bool(self.link.connected)
        allowed = self.nonzero_allowed()
        outcome = 'unknown' if self.unknown_latched else self.result
        return {'scope': SCOPE,
                'real_write_enabled': allowed,
                'nonzero_block_reason': None if allowed else NONZERO_BLOCK_REASON,
                'verified_envelope': dict(VERIFIED_ENVELOPE),
                'mode_catalog': [{'mode': code, 'name': name}
                                 for code, name in MODE_NAMES.items()],
                'authorized_envelope': {'function': self.function,
                                        'modes': list(self.modes),
                                        'max_intensity': self.cap},
                'active_mode': self.active_mode,
                'unverified_configuration': self.unverified_configuration,
                'authorized_channels': copy.deepcopy(self.channel_profiles),
                'combined_control_authorized': self.combined_control_authorized,
                'desired_state': copy.deepcopy(self.desired_state),
                'link_connected': connected,
                'application_gatt_session': 'open' if connected else 'closed',
                'physical_radio_disconnect_verified': False,
                'physical_stop_verified': False,
                'device_confirmed': False,
                'device_state': 'unknown',
                'unknown_latched': self.unknown_latched,
                'manual_power_off_required': outcome == 'unknown',
                'desired_level': self.desired_level,
                'outcome': outcome, 'reason': self.reason,
                'authorization_active': self.token is not None,
                'authorization_mode': ('standing' if self.persistent_authorization else 'timed')
                                      if self.token is not None else 'none',
                'cap': self.cap}

    @staticmethod
    def _duration(value, maximum):
        if type(value) not in (int, float) or not 0 < value <= maximum:
            raise ControlRejected('Duration outside local limits')

    def _sleep(self, active):
        if active:
            self.sleep_guard.engage()
        else:
            self.sleep_guard.disengage()

    def _write_stop(self, *, reset=False):
        self.link.write(build_stop_frames()[0])
        self._journal_output(False, reset=reset)

    def _journal_output(self, pending, *, reset=False):
        if self.output_journal is not None:
            try:
                self.output_journal(pending, self.unknown_latched and not reset)
            except OSError:
                raise UnknownExecution('output_journal_unavailable') from None

    def _clear_desired(self):
        self.desired_level = 0
        self.desired_state = {name: {'level': 0, 'mode': 1} for name in FUNCTIONS}

    def _stop_and_release(self, reason, *, write=True):
        # Revoke first: a late nonzero command cannot pass the token check.
        was_nonzero = self.desired_level > 0
        self.token = None
        self.persistent_authorization = False
        self._clear_desired()
        self.command_deadline = None
        self.reason = reason
        self._sleep(False)
        if reason == 'ble_disconnected' and was_nonzero:
            # The link vanished while a nonzero value may have been applied.
            self.unknown_latched = True
        if write and self.link.connected:
            try:
                self._write_stop()
                self.result = 'gatt_ack'
            except (UnknownExecution, LinkUnavailable):
                self.result = 'unknown'
                self.unknown_latched = True
        else:
            # No independent evidence that the device stopped.
            self.result = 'unknown'
            if was_nonzero:
                self.unknown_latched = True
        try:
            self.link.disconnect()
        except Exception:
            pass

    def _zero_barrier(self):
        try:
            self.link.ensure_connected()
            self._write_stop(reset=True)
        except (UnknownExecution, LinkUnavailable, OSError) as exc:
            self.result = 'unknown'
            # Our own link labels or an exception type, never driver text.
            detail = str(exc) if isinstance(exc, LinkUnavailable) else type(exc).__name__
            self.reason = 'zero_barrier_failed:' + (detail or 'unknown')
            try:
                self.link.disconnect()
            except Exception:
                pass
            raise ControlRejected('Zero barrier unconfirmed') from None
        self.result = 'gatt_ack'
        self._clear_desired()

    def tick(self):
        if not self._lock.acquire(timeout=0.05):
            return
        try:
            if self.link.take_link_lost():
                self._stop_and_release('ble_disconnected', write=False)
                return
            if self.token is None:
                self._sleep(False)
                return
            self._sleep(self.desired_level > 0 or not self.persistent_authorization)
            deadlines = [(self.session_deadline, 'session_expired'),
                         (self.lock_deadline, 'lock_expired'),
                         (self.heartbeat_deadline, 'heartbeat_timeout')]
            if self.command_deadline is not None:
                deadlines.append((self.command_deadline, 'command_ttl'))
            deadline, reason = min(deadlines, key=lambda item: item[0])
            if self.clock() >= deadline:
                self._stop_and_release(reason)
        finally:
            self._lock.release()

    def _check_token(self, token):
        self.tick()
        if not isinstance(token, str) or self.token is None:
            raise ControlRejected('Active local authorization required')
        if not secrets.compare_digest(token.encode('utf-8'), self.token.encode('ascii')):
            raise ControlRejected('Active local authorization required')

    def authorize(self, *, duration=30, cap=VERIFIED_ENVELOPE['max_intensity'],
                  function='vibration', modes=(1,), allow_unverified=False, channels=None,
                  persistent=False):
        """Owner-console action; no HTTP or MCP tool exposes authorization."""
        with self._lock:
            if self.closed:
                raise ControlRejected('Control service closed')
            self.tick()
            if self.token is not None:
                raise ControlRejected('Controller lock already held')
            self._duration(duration, MAX_GRANT_SECONDS)
            if type(persistent) is not bool:
                raise ControlRejected('Invalid authorization policy')
            combined = channels is not None
            if not combined:
                if not isinstance(function, str) or function not in FUNCTIONS:
                    raise ControlRejected('Unknown function')
                channels = {function: {'cap': cap, 'modes': modes}}
            elif function != 'vibration' or modes != (1,) or cap != 1:
                raise ControlRejected('Use either single or channel authorization')
            if (not isinstance(channels, dict) or not channels
                    or channels.keys() - FUNCTIONS.keys()):
                raise ControlRejected('Invalid channel authorization')
            profiles = {}
            for name, profile in channels.items():
                if not isinstance(profile, dict) or set(profile) != {'cap', 'modes'}:
                    raise ControlRejected('Invalid channel profile')
                limit, allowed_modes = profile['cap'], profile['modes']
                if type(limit) is not int or not 0 <= limit <= 100:
                    raise ControlRejected('Invalid owner cap')
                if not isinstance(allowed_modes, (tuple, list)):
                    raise ControlRejected('Invalid authorized modes')
                allowed_modes = tuple(allowed_modes)
                if (not allowed_modes or any(type(m) is not int or m not in range(1, 5)
                                             for m in allowed_modes)
                        or len(set(allowed_modes)) != len(allowed_modes)):
                    raise ControlRejected('Invalid authorized modes')
                profiles[name] = {'cap': limit, 'modes': allowed_modes}
            unverified = combined or any(name != 'vibration' or p['modes'] != (1,)
                                         or p['cap'] > 1 for name, p in profiles.items())
            if type(allow_unverified) is not bool or (unverified and not allow_unverified):
                raise ControlRejected('Owner confirmation required for unverified configuration')
            self._stop_requested.clear()
            self._zero_barrier()
            if self._stop_requested.is_set():
                self._stop_and_release('emergency_during_authorization')
                raise ControlRejected('Authorization interrupted by emergency stop')
            # A confirmed owner grant plus a successful zero barrier is the
            # only operator-approved reset of a latched unknown.
            self.unknown_latched = False
            now = self.clock()
            self.function = 'vibration' if 'vibration' in profiles else next(iter(profiles))
            self.channel_profiles = profiles
            self.combined_control_authorized = combined
            self.cap = profiles[self.function]['cap']
            self.modes = profiles[self.function]['modes']
            self.active_mode = self.modes[0]
            self.unverified_configuration = unverified
            self.persistent_authorization = persistent
            self.session_deadline = float('inf') if persistent else now + duration
            self.lock_deadline = now + 3
            self.heartbeat_deadline = now + 2
            self.results.clear()
            self.recent_commands.clear()
            self.token = secrets.token_urlsafe(24)
            self.reason = 'authorization_active'
            self._sleep(not persistent)
            return self.token

    def heartbeat(self, token):
        with self._lock:
            self._check_token(token)
            self.heartbeat_deadline = self.clock() + 2

    def set_level(self, token, *, device=DEVICE, level, key, mode='constant', ttl=1,
                  function=None):
        with self._lock:
            clear_all = function is None and level == 0
            function = self.function if function is None else function
            if not isinstance(function, str) or function not in self.channel_profiles:
                raise ControlRejected('Function outside owner authorization')
            if mode == 'constant':
                mode = self.channel_profiles[function]['modes'][0]
            return self._command(token, {function: {'level': level, 'mode': mode}},
                                 key=key, ttl=ttl, device=device, single=function,
                                 clear_all=clear_all)

    def set_state(self, token, *, state, key, ttl=1, device=DEVICE):
        with self._lock:
            return self._command(token, state, key=key, ttl=ttl, device=device)

    def _command(self, token, state, *, key, ttl, device, single=None, clear_all=False):
        # Both public callers hold _lock throughout validation and every fragment.
        self._check_token(token)
        if self.closed:
            raise ControlRejected('Control service closed')
        if device != self.DEVICE:
            raise ControlRejected('Unbound device')
        if not isinstance(state, dict) or state.keys() - FUNCTIONS.keys():
            raise ControlRejected('Invalid state channels')
        requested = {}
        for name, value in state.items():
            if not isinstance(value, dict) or 'level' not in value or value.keys() - {'level', 'mode'}:
                raise ControlRejected('Invalid channel state')
            profile = self.channel_profiles.get(name, {'cap': 0, 'modes': (1,)})
            level = value['level']
            mode = value.get('mode', profile['modes'][0])
            if type(level) is not int or not 0 <= level <= 100:
                raise ControlRejected('Invalid intensity')
            if type(mode) is not int or mode not in profile['modes']:
                raise ControlRejected('Mode outside owner authorization')
            if level and name not in self.channel_profiles:
                raise ControlRejected('Function outside owner authorization')
            requested[name] = {'level': level, 'mode': mode}
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ControlRejected('Request key required')
        self._duration(ttl, 1)
        payload = (device, single, clear_all, tuple((name, value['level'], value['mode'])
                                        for name, value in sorted(requested.items())), ttl)
        if key in self.results:
            previous_payload, result = self.results[key]
            if previous_payload != payload:
                raise ControlRejected('Request key reused with different input')
            return copy.deepcopy(result)
        target = (copy.deepcopy(self.desired_state) if single and not clear_all else
                  {name: {'level': 0, 'mode': self.channel_profiles.get(
                      name, {'modes': (1,)})['modes'][0]} for name in FUNCTIONS})
        for name, value in requested.items():
            target[name] = {'level': min(value['level'], self.channel_profiles.get(
                name, {'cap': 0})['cap']), 'mode': value['mode']}
        nonzero = any(value['level'] for value in target.values())
        if nonzero and single is None and not self.combined_control_authorized:
            raise ControlRejected('Combined control requires an owner channel grant')
        now = self.clock()
        while self.recent_commands and self.recent_commands[0] <= now - 1:
            self.recent_commands.popleft()
        if len(self.recent_commands) >= 5:
            raise ControlRejected('Command rate exceeded')
        self.recent_commands.append(now)
        if nonzero and not self.nonzero_allowed():
            result = {'outcome': 'blocked_by_disconnect_safety', 'level': None,
                      'state': None, 'real_write_enabled': False}
        elif not self.link.connected:
            self._stop_and_release('link_unavailable', write=False)
            result = {'outcome': 'unknown', 'level': None, 'state': None}
        else:
            try:
                changed_mode = any(old['level'] and target[name]['level']
                                   and old['mode'] != target[name]['mode']
                                   for name, old in self.desired_state.items())
                if changed_mode:
                    self._write_stop()
                    self._clear_desired()
                if not nonzero:
                    frames = build_stop_frames()
                elif single and target[single]['level'] and not any(
                        value['level'] for name, value in target.items() if name != single):
                    frames = [build_control_frame(single, target[single]['level'],
                                                  target[single]['mode'],
                                                  max_intensity=self.channel_profiles[single]['cap'])]
                else:
                    frames = build_state_frames(
                        {name: value['level'] for name, value in target.items()},
                        {name: value['mode'] for name, value in target.items()},
                        max_intensity=100)
                if nonzero:
                    self._journal_output(True)
                for frame in frames:
                    if nonzero and (self._stop_requested.is_set() or self.clock() >= now + ttl):
                        raise UnknownExecution('state_deadline_expired')
                    self.link.write(frame)
                if nonzero and (self._stop_requested.is_set() or self.clock() >= now + ttl):
                    raise UnknownExecution('state_deadline_expired')
                if not nonzero:
                    self._journal_output(False)
                self.desired_state = target
                self.desired_level = max(value['level'] for value in target.values())
                self.active_mode = target[self.function]['mode']
                self.command_deadline = now + ttl if nonzero else None
                self.lock_deadline = now + 3
                self.result = 'gatt_ack'
                result = {'outcome': 'gatt_ack', 'state': copy.deepcopy(target),
                          'requested_state': copy.deepcopy(requested),
                          'clamped_channels': [name for name, value in requested.items()
                                               if value['level'] != target[name]['level']]}
                if single:
                    result.update({'level': target[single]['level'],
                                   'requested_level': requested[single]['level'],
                                   'clamped': requested[single]['level'] != target[single]['level'],
                                   'function': single, 'mode': target[single]['mode']})
            except (UnknownExecution, LinkUnavailable):
                self.unknown_latched = True
                self._stop_and_release('unknown_execution')
                result = {'outcome': 'unknown', 'level': None, 'state': None}
        self.results[key] = (payload, copy.deepcopy(result))
        return result

    def stop(self, token):
        with self._lock:
            self._check_token(token)
            self._stop_and_release('stop_and_release')

    def request_emergency_stop(self):
        self._stop_requested.set()

    def emergency_stop(self):
        self.request_emergency_stop()
        with self._lock:
            self._stop_and_release('local_emergency')

    def client_disconnected(self):
        with self._lock:
            self._stop_and_release('client_disconnected')

    def link_lost(self):
        with self._lock:
            self._stop_and_release('ble_disconnected', write=False)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stop_and_release('service_closed')
            try:
                self.link.close()
            except Exception:
                pass
