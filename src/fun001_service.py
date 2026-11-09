"""Resident local FUN-001 control service.

The owner console enables a saved standing permission or creates a timed
grant. Standing sessions connect to the bound device through a zero barrier.
The loopback API and stdio MCP execute within the owner's saved permission.

Live control is experimental. The owner grants locally after reviewing the
known link-loss risk. Planned exits attempt the observed stop frame.
"""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import sys
import threading
import time

from ble_readonly import load_binding
from device_safety import live_nonzero_allowed
from fun001_core import (BleLink, ControlRejected, MAX_GRANT_SECONDS,
                         RealSafetyController, SCOPE)
from fun001_protocol import FUNCTIONS, MODE_NAMES
from local_runtime import LoopbackClient, LoopbackServer, RequestRejected
from standing_permit import CHANNELS, StandingPermit

RUNTIME = Path(__file__).resolve().parents[1] / 'private' / 'control'
DEFAULT_PORT = 18762
CREDENTIAL = 'FUN001_API_TOKEN'
ACTIONS = {
    'status': set(),
    'set_intensity': {'session_id', 'level', 'key', 'ttl', 'function', 'mode'},
    'set_state': {'session_id', 'state', 'key', 'ttl'},
    'heartbeat': {'session_id'},
    'stop': {'session_id'},
    'release_control': {'session_id'},
    'emergency_stop': set(),
    'client_closed': {'session_id'},
}
ON_SITE = ('设备是否已经取下、放在桌面、未接触身体、附近无人使用，'
           '并且电源键伸手可及？')
MAX_SUSTAINED_SECONDS = 300.0


class ControlState:
    def __init__(self, link, clock=None, nonzero_allowed=None, permit=None):
        self.link = link
        if nonzero_allowed is None:
            nonzero_allowed = live_nonzero_allowed
        arguments = {} if clock is None else {'clock': clock}
        self.controller = RealSafetyController(link, nonzero_allowed=nonzero_allowed,
                                               output_journal=permit.journal if permit else None,
                                               **arguments)
        self.permit = permit
        self.connecting = False
        self.connect_worker = None
        self.next_connect = 0
        self.owner_recovery = False
        self.lock = threading.RLock()
        self.session_id = None
        self.client_id = None
        self.closed = False
        self.clock = time.monotonic if clock is None else clock

    def tick(self):
        with self.lock:
            # Idle at zero: the session may wait for the next burst until the
            # grant deadline. A live nonzero command keeps the normal 2/3 s
            # heartbeat and lock deadlines.
            if (self.controller.token is not None
                    and self.controller.desired_level == 0
                    and self.controller.command_deadline is None):
                self.controller.heartbeat_deadline = self.controller.session_deadline
                self.controller.lock_deadline = self.controller.session_deadline
            had_session = self.session_id is not None
            self.controller.tick()
            if self.controller.token is None:
                self.session_id = None
                self.client_id = None
                if had_session:
                    self.next_connect = self.clock() + 5
            if self.permit is not None:
                if self.controller.unknown_latched and not self.owner_recovery:
                    self.permit.suspend()
                policy = self.permit.snapshot()
                if (policy['enabled'] and not policy['suspended_reason']
                        and not policy['output_pending'] and not self.closed
                        and self.session_id is None and not self.connecting
                        and self.clock() >= self.next_connect):
                    self.connecting = True
                    self.next_connect = self.clock() + 5
                    self.connect_worker = threading.Thread(
                        target=self._connect_standing, args=(policy,), daemon=True)
                    self.connect_worker.start()

    def _connect_standing(self, policy):
        try:
            self.controller.authorize(channels=policy['channels'],
                                      allow_unverified=True, persistent=True)
            with self.lock:
                current = self.permit.snapshot()
                if (self.closed or not current['enabled'] or current['suspended_reason']
                        or current['generation'] != policy['generation']):
                    self.controller.emergency_stop()
                else:
                    self.controller.heartbeat_deadline = float('inf')
                    self.controller.lock_deadline = float('inf')
                    self.session_id = secrets.token_hex(16)
                    self.client_id = None
        except (OSError, ControlRejected, RuntimeError):
            pass
        finally:
            with self.lock:
                self.connecting = False
                self.owner_recovery = False
                self.next_connect = self.clock() + 5

    def enable_standing(self):
        if self.permit is None:
            raise ControlRejected('Standing permission unavailable')
        with self.lock:
            if self.closed or self.connecting or self.session_id is not None:
                raise ControlRejected('Finish the current control session first')
            self.permit.enable()
            self.next_connect = 0
            self.owner_recovery = True
            self.tick()

    def revoke_standing(self):
        self.controller.request_emergency_stop()
        try:
            if self.permit is not None:
                self.permit.revoke()
        except OSError:
            raise ControlRejected('Standing revocation persistence failed') from None
        finally:
            self.stop_current()

    def stop_current(self):
        self.controller.request_emergency_stop()
        with self.lock:
            self.controller.emergency_stop()
            self.session_id = None
            self.client_id = None
            self.next_connect = self.clock() + 5

    def status(self):
        with self.lock:
            self.tick()
            status = self.controller.status()
            policy = self.permit.snapshot() if self.permit else None
            status.update({
                'scope': SCOPE,
                'session_id': self.session_id,
                'controller_claimed': self.client_id is not None,
                'authorization_active': self.session_id is not None,
                'cap': self.controller.cap,
                'standing_enabled': policy['enabled'] if policy else False,
                'standing_suspended': policy['suspended_reason'] if policy else None,
                'standing_channels': policy['channels'] if policy else {},
                'standing_connecting': self.connecting,
                'standing_retry_after': max(0, self.next_connect - self.clock())
                    if policy and policy['enabled'] and self.session_id is None
                    and not self.connecting and not policy['suspended_reason'] else 0,
            })
            if policy and policy['suspended_reason']:
                status['manual_power_off_required'] = True
            return status

    def authorize(self, *, function='vibration', modes=(1,), cap=1,
                  allow_unverified=False, channels=None):
        """Owner-console action; deliberately absent from HTTP and MCP."""
        with self.lock:
            if self.closed:
                raise ControlRejected('Control service closed')
            if self.permit is not None and self.permit.snapshot()['enabled']:
                raise ControlRejected('Revoke standing permission before a timed grant')
            self.tick()
            if self.session_id is not None or self.connecting:
                raise ControlRejected('Controller lock already held')
            self.controller.authorize(duration=MAX_GRANT_SECONDS,
                                      cap=cap, function=function, modes=modes,
                                      allow_unverified=allow_unverified, channels=channels)
            # The owner grant may wait at zero until claimed. Once a controller
            # acts, the ordinary 2-second heartbeat and 3-second lock apply.
            self.controller.heartbeat_deadline = self.controller.session_deadline
            self.controller.lock_deadline = self.controller.session_deadline
            self.session_id = secrets.token_hex(16)
            self.client_id = None

    def _check_session(self, arguments):
        self.tick()
        session = arguments.get('session_id')
        if (not isinstance(session, str) or self.session_id is None
                or not secrets.compare_digest(session, self.session_id)):
            raise ControlRejected('Current session required')

    def execute(self, action, arguments, client_id):
        # A validated emergency can signal an in-flight fragmented group before
        # waiting for the state lock. The HTTP runtime already authenticates it.
        if (action == 'emergency_stop' and arguments == {}
                and isinstance(client_id, str) and 1 <= len(client_id) <= 128):
            self.controller.request_emergency_stop()
        with self.lock:
            if self.closed:
                raise ControlRejected('Control service closed')
            if not isinstance(action, str) or action not in ACTIONS:
                raise ControlRejected('Unknown action')
            if not isinstance(arguments, dict) or arguments.keys() - ACTIONS[action]:
                raise ControlRejected('Invalid arguments')
            if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
                raise ControlRejected('Invalid controller')
            if action == 'status':
                return self.status()
            if action == 'emergency_stop':
                # Any authenticated local client may zero and revoke.
                self.revoke_standing()
                return self.status()
            self._check_session(arguments)
            if action == 'client_closed':
                if self.client_id == client_id:
                    self.controller.client_disconnected()
                    self.session_id = None
                    self.client_id = None
                    self.next_connect = self.clock() + 5
                return self.status()
            if self.client_id is not None and self.client_id != client_id:
                raise ControlRejected('Controller lock held')
            token = self.controller.token
            if action in ('set_intensity', 'set_state'):
                if action == 'set_state':
                    if not {'state', 'key'} <= arguments.keys():
                        raise ControlRejected('State and request key required')
                    result = self.controller.set_state(
                        token, state=arguments['state'], key=arguments['key'],
                        ttl=arguments.get('ttl', 1))
                else:
                    if not {'level', 'key'} <= arguments.keys():
                        raise ControlRejected('Level and request key required')
                    if 'function' in arguments and not isinstance(arguments['function'], str):
                        raise ControlRejected('Invalid function')
                    if 'mode' in arguments and type(arguments['mode']) is not int:
                        raise ControlRejected('Invalid mode')
                    result = self.controller.set_level(
                        token, level=arguments['level'], key=arguments['key'],
                        ttl=arguments.get('ttl', 1), function=arguments.get('function'),
                        mode=arguments.get('mode', 'constant'))
                if result.get('outcome') == 'blocked_by_disconnect_safety':
                    return {**result, 'scope': SCOPE,
                            'real_write_enabled': self.controller.nonzero_allowed()}
                if self.client_id is None and self.controller.token is not None:
                    self.controller.heartbeat(token)
                self.client_id = client_id
                return {**result, 'scope': SCOPE,
                        'real_write_enabled': self.controller.nonzero_allowed()}
            if action == 'heartbeat':
                self.controller.heartbeat(token)
                if self.client_id is None:
                    self.controller.lock_deadline = self.clock() + 3
                self.client_id = client_id
            else:
                if action == 'release_control' and self.permit is not None:
                    self.revoke_standing()
                else:
                    self.controller.stop(token)
                self.session_id = None
                self.client_id = None
                self.next_connect = self.clock() + 5
            return self.status()

    def close(self):
        with self.lock:
            self.closed = True
            self.controller.close()
            self.session_id = None
            self.client_id = None


class ControlServer(LoopbackServer):
    def __init__(self, *, state, directory=RUNTIME, port=DEFAULT_PORT):
        super().__init__(state=state, directory=directory, port=port, scope=SCOPE,
                         credential=CREDENTIAL,
                         lock_message='Control instance already running')


class ControlClient(LoopbackClient):
    def __init__(self, directory=RUNTIME):
        super().__init__(directory=directory, scope=SCOPE, credential=CREDENTIAL,
                         error_type=ControlRejected)
        self.session_id = None

    def request(self, action, arguments=None):
        result = super().request(action, arguments)
        if action in ('set_intensity', 'set_state', 'heartbeat'):
            self.session_id = (arguments or {}).get('session_id')
        return result

    def close(self):
        if self.session_id is not None:
            try:
                self.request('client_closed', {'session_id': self.session_id})
            except (OSError, ValueError, http.client.HTTPException):
                pass
            self.session_id = None


def sustained_level(state, level, seconds, *, mode=None, clock=time.monotonic,
                   sleep=time.sleep):
    if type(level) is not int or not 0 <= level <= 100:
        raise ControlRejected('Invalid intensity')
    if mode is not None and (type(mode) is not int or mode not in range(1, 5)):
        raise ControlRejected('Invalid mode')
    if type(seconds) not in (int, float) or not 1 <= seconds <= MAX_SUSTAINED_SECONDS:
        raise ControlRejected('Duration outside 1-300 seconds')
    selection = {} if mode is None else {'mode': mode}
    return _sustained_command(state, 'set_intensity', {'level': level, **selection},
                              seconds, clock=clock, sleep=sleep)


def sustained_state(state, channels, seconds, *, clock=time.monotonic, sleep=time.sleep):
    if not isinstance(channels, dict) or channels.keys() - FUNCTIONS.keys():
        raise ControlRejected('Invalid state channels')
    if type(seconds) not in (int, float) or not 1 <= seconds <= MAX_SUSTAINED_SECONDS:
        raise ControlRejected('Duration outside 1-300 seconds')
    return _sustained_command(state, 'set_state', {'state': channels}, seconds,
                              clock=clock, sleep=sleep)


def _sustained_command(state, action, arguments, seconds, *, clock, sleep):
    session = state.status().get('session_id')
    if not session:
        raise ControlRejected('Current session required')
    deadline = clock() + seconds
    result = {'outcome': 'cancelled'}
    zero = {'outcome': 'not_attempted'}
    try:
        while True:
            result = state.execute(action, {
                'session_id': session, **arguments,
                'key': secrets.token_hex(8), 'ttl': 1.0}, 'owner-console')
            if result.get('outcome') != 'gatt_ack':
                break
            state.execute('heartbeat', {'session_id': session}, 'owner-console')
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(0.5, remaining))
    finally:
        status = state.status()
        if status.get('authorization_active'):
            try:
                # Zero but keep the session: the owner may start another
                # burst within the same grant. stop/quit still end it.
                zero = state.execute('set_intensity', {
                    'session_id': session, 'level': 0,
                    'key': secrets.token_hex(8), 'ttl': 1.0}, 'owner-console')
            except ControlRejected:
                zero = {'outcome': 'rejected_or_unknown'}
    return {**state.status(), 'last_execution': result,
            'zero_outcome': zero.get('outcome', 'unknown')}


def print_status(status):
    status.pop('session_id', None)
    print(json.dumps(status, ensure_ascii=False))
    if status.get('outcome') == 'unknown':
        print('结果未知：设备可能仍在动作。请立即现场关闭设备电源。')


def parse_grant(command):
    parts = command.split()
    if parts and parts[0] == 'grant3':
        if len(parts) not in (4, 5):
            raise ControlRejected('格式：grant3 吸吮上限 震动上限 电流上限 [模式或all]')
        try:
            caps = [int(value) for value in parts[1:4]]
            modes = (1, 2, 3, 4) if len(parts) == 4 or parts[4] == 'all' else (int(parts[4]),)
        except ValueError:
            raise ControlRejected('上限和模式格式不对。') from None
        if any(not 0 <= cap <= 100 for cap in caps) or any(m not in range(1, 5) for m in modes):
            raise ControlRejected('模式范围1-4，上限范围0-100。')
        return {'channels': {name: {'cap': cap, 'modes': modes}
                             for name, cap in zip(FUNCTIONS, caps)}}
    if parts == ['grant']:
        return {'function': 'vibration', 'modes': (1,), 'cap': 1}
    if len(parts) != 4 or parts[0] != 'grant' or parts[1] not in FUNCTIONS:
        raise ControlRejected('格式：grant [vibration|suction|electric 模式1-4或all 上限0-100]')
    try:
        modes = (1, 2, 3, 4) if parts[2] == 'all' else (int(parts[2]),)
        cap = int(parts[3])
    except ValueError:
        raise ControlRejected('模式和上限必须是整数，模式也可用 all。') from None
    if any(mode not in range(1, 5) for mode in modes) or not 0 <= cap <= 100:
        raise ControlRejected('模式范围1-4，上限范围0-100。')
    return {'function': parts[1], 'modes': modes, 'cap': cap}


def owner_console():
    state = ControlState(BleLink(load_binding()), permit=StandingPermit())
    relay = ControlServer(state=state)
    relay.start()
    print('本机控制服务已启动，仅绑定 127.0.0.1。')
    print('风险提示：已观察到BLE断链后持续动作。停止依赖进程与链路，异常时需要现场物理关机。')
    print('首次验收请取下设备放在桌面，并保持电源键可立即触及。')
    print('permit 开启常态许可，revoke 永久撤销。连接后先写零值屏障。')
    print('普通 grant 使用最长20分钟的临时授权，常态许可使用已保存的固定范围。')
    print('grant3 吸吮上限 震动上限 电流上限 [模式或all]，state {三路JSON} [秒数]。')
    print('命令 grant [功能 模式或all 上限]、level 强度 [秒数] [模式]、functions、status、stop、quit。')
    try:
        while True:
            try:
                command = input('fun001> ').strip()
                if not command:
                    continue
                if command == 'quit':
                    break
                if command == 'permit':
                    current = state.status()
                    if current['standing_enabled'] and not current['standing_suspended']:
                        print('常态许可已经开启。')
                        continue
                    if current['manual_power_off_required']:
                        print('上次输出结果未知，请先关闭设备电源再重新开机。')
                        if input('输入“已关机”确认完成断电重启，其他输入取消。\n').strip() != '已关机':
                            continue
                    print('常态配置：' + json.dumps(CHANNELS, ensure_ascii=False))
                    print('许可会跨重启保存，设备可用时自动连接并归零，随后接受范围内的新指令。')
                    print(ON_SITE)
                    if input('输入“开启常态许可”确认本次设置，其他输入取消。\n').strip() != '开启常态许可':
                        continue
                    state.enable_standing()
                elif command == 'revoke':
                    state.revoke_standing()
                elif command == 'grant' or command.startswith(('grant ', 'grant3 ')):
                    profile = parse_grant(command)
                    unverified = ('channels' in profile or profile['function'] != 'vibration'
                                  or profile['modes'] != (1,) or profile['cap'] > 1)
                    if state.status().get('manual_power_off_required'):
                        print('上次结果未知，设备可能仍在动作。请先关闭设备电源，再重新开机。')
                        if input('输入“已关机”确认完成断电重启，其他输入取消。\n').strip() != '已关机':
                            print('已取消。')
                            continue
                    print(ON_SITE)
                    confirmation = input('输入“是”确认现场条件，其他输入取消。\n').strip()
                    if confirmation != '是':
                        print('已取消。')
                        continue
                    print('本次授权配置：' + json.dumps(profile, ensure_ascii=False))
                    if unverified:
                        print('此配置超出现有现场验收范围。请桌面先做最低值短时验证，异常时立即关机。')
                        if input('输入“接受本次未验收配置”继续，其他输入取消。\n').strip() != '接受本次未验收配置':
                            print('已取消。')
                            continue
                    print('正在连接绑定的设备并写入零值屏障，请保持设备可随时关机。')
                    state.authorize(**profile, allow_unverified=unverified)
                elif command.startswith('state '):
                    try:
                        channels, end = json.JSONDecoder().raw_decode(command[6:].strip())
                        duration = command[6:].strip()[end:].strip()
                        seconds = float(duration) if duration else 1.0
                    except ValueError:
                        print('格式：state {功能: {level:强度, mode:模式}} [秒数1-300]，JSON 键用双引号。')
                        continue
                    try:
                        sustained_state(state, channels, seconds)
                    except KeyboardInterrupt:
                        print('已提前停止本次三路输出。')
                elif command.startswith('level '):
                    parts = command.split()
                    if not 2 <= len(parts) <= 4:
                        print('格式：level 强度0-100 [秒数1-300] [模式1-4]。')
                        continue
                    try:
                        level = int(parts[1])
                        seconds = 1.0 if len(parts) == 2 else float(parts[2])
                        mode = None if len(parts) < 4 else int(parts[3])
                    except ValueError:
                        print('强度、秒数或模式格式不对。')
                        continue
                    print(f'请求值 {level}，持续 {seconds} 秒，实际值受本次上限约束，Ctrl+C 可提前停止。')
                    try:
                        sustained_level(state, level, seconds, mode=mode)
                    except KeyboardInterrupt:
                        print('已提前停止本次输出。')
                elif command == 'stop':
                    state.stop_current()
                elif command == 'functions':
                    print('功能 vibration（震动）、suction（吸吮）、electric（App 标为 Soda current）。')
                    print('模式编号1-4，强度编码0-100。单台实测限模式1/值1的吸吮、震动与一次组合，详见 SAFETY.md。')
                    for code, name in MODE_NAMES.items():
                        print(f'模式 {code}：{name}')
                elif command != 'status':
                    print('未知命令。')
                    continue
                print_status(state.status())
            except ControlRejected as exc:
                print(f'动作被拒绝：{exc}')
                print_status(state.status())
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        relay.close()
        print('本机控制服务已关闭。')


def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('serve', 'status'))
    arguments = parser.parse_args()
    try:
        if arguments.action == 'serve':
            owner_console()
            return 0
        result = ControlClient().request('status')
        result.pop('session_id')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, RequestRejected,
            http.client.HTTPException):
        print(json.dumps({'scope': SCOPE, 'status': 'control_service_unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
