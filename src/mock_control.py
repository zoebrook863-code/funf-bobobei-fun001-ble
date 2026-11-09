"""Offline safety model with a semantic mock transport, never real BLE.

Call tick() when advancing the simulated clock. This is not a live watchdog.
The 0..100 range and default cap of 1 are synthetic test values.
"""
from collections import deque
import secrets
import threading
import time


class ControlRejected(ValueError):
    pass


class UnknownExecution(RuntimeError):
    pass


class MockTransport:
    def __init__(self):
        self.connected = True
        self.level = 0
        self.writes = []
        self.fail_next_nonzero = False

    def write(self, level):
        if not self.connected:
            raise UnknownExecution('Mock link unavailable')
        self.writes.append(level)
        self.level = level
        if level and self.fail_next_nonzero:
            self.fail_next_nonzero = False
            raise UnknownExecution('Mock acknowledgement lost')


class MockSafetyController:
    DEVICE = 'mock-fun001'

    def __init__(self, transport, clock=time.monotonic):
        if type(transport) is not MockTransport:
            raise TypeError('Only the semantic mock transport is supported')
        self.transport = transport
        self.clock = clock
        self._lock = threading.RLock()
        self.token = None
        self.session_deadline = 0
        self.lock_deadline = 0
        self.heartbeat_deadline = 0
        self.command_deadline = None
        self.cap = 1
        self.desired_level = 0
        self.result = 'mock_zero'
        self.reason = 'initial'
        self.results = {}
        self.recent_commands = deque()

    def _invalidate(self, reason):
        # Revoke before attempting the zero write, including uncertain outcomes.
        self.token = None
        self.desired_level = 0
        self.command_deadline = None
        self.reason = reason
        try:
            self.transport.write(0)
            self.result = 'mock_zero'
        except UnknownExecution:
            self.result = 'unknown'

    def tick(self):
        with self._lock:
            if self.token is None:
                return
            deadlines = [(self.session_deadline, 'session_expired'),
                         (self.lock_deadline, 'lock_expired'),
                         (self.heartbeat_deadline, 'heartbeat_timeout')]
            if self.command_deadline is not None:
                deadlines.append((self.command_deadline, 'command_ttl'))
            deadline, reason = min(deadlines)
            if self.clock() >= deadline:
                self._invalidate(reason)

    def _check_token(self, token):
        self.tick()
        if not isinstance(token, str) or self.token is None:
            raise ControlRejected('Active mock authorization required')
        if not secrets.compare_digest(token.encode('utf-8'), self.token.encode('ascii')):
            raise ControlRejected('Active mock authorization required')

    @staticmethod
    def _duration(value, maximum):
        if type(value) not in (int, float) or not 0 < value <= maximum:
            raise ControlRejected('Duration outside mock limits')

    def authorize(self, *, duration=30, cap=1):
        """Test-owner action. No CLI or MCP tool exposes authorization."""
        with self._lock:
            self.tick()
            if self.token is not None:
                raise ControlRejected('Mock controller lock already held')
            self._duration(duration, 30)
            if type(cap) is not int or not 1 <= cap <= 100:
                raise ControlRejected('Invalid mock owner cap')
            self._invalidate('new_authorization_zero_barrier')
            if self.result == 'unknown':
                raise ControlRejected('Zero barrier unconfirmed')
            now = self.clock()
            self.cap = cap
            self.session_deadline = now + duration
            self.lock_deadline = now + 3
            self.heartbeat_deadline = now + 2
            self.results.clear()
            self.recent_commands.clear()
            self.token = secrets.token_urlsafe(24)
            return self.token

    def heartbeat(self, token):
        with self._lock:
            self._check_token(token)
            self.heartbeat_deadline = self.clock() + 2

    def set_level(self, token, *, device=DEVICE, level, key, mode='constant', ttl=1):
        with self._lock:
            self._check_token(token)
            if device != self.DEVICE:
                raise ControlRejected('Unbound mock device')
            if mode != 'constant':
                raise ControlRejected('Unknown mock mode')
            if type(level) is not int or not 0 <= level <= 100:
                raise ControlRejected('Invalid mock intensity')
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ControlRejected('Request key required')
            self._duration(ttl, 1)
            payload = (device, level, mode, ttl)
            if key in self.results:
                previous_payload, result = self.results[key]
                if previous_payload != payload:
                    raise ControlRejected('Request key reused with different input')
                return result.copy()
            now = self.clock()
            while self.recent_commands and self.recent_commands[0] <= now - 1:
                self.recent_commands.popleft()
            if len(self.recent_commands) >= 5:
                raise ControlRejected('Mock command rate exceeded')
            self.recent_commands.append(now)
            applied = min(level, self.cap)
            try:
                self.transport.write(applied)
                self.desired_level = applied
                self.command_deadline = now + ttl if applied else None
                self.lock_deadline = now + 3
                self.result = 'mock_ack'
                result = {'outcome': 'mock_ack', 'level': applied}
            except UnknownExecution:
                self._invalidate('unknown_execution')
                result = {'outcome': 'unknown', 'level': None}
            self.results[key] = (payload, result.copy())
            return result

    def stop(self, token):
        with self._lock:
            self._check_token(token)
            self._invalidate('stop_and_release')

    def emergency_stop(self):
        with self._lock:
            self._invalidate('local_emergency')

    def network_disconnected(self):
        with self._lock:
            self._invalidate('network_disconnected')

    def ble_disconnected(self):
        with self._lock:
            self.transport.connected = False
            self._invalidate('ble_disconnected')

    def reconnect(self):
        with self._lock:
            self.transport.connected = True
            self._invalidate('reconnect_zero_barrier')
