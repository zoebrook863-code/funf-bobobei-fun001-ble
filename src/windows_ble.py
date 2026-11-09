"""Windows Bluetooth environment probe. Reads adapter capabilities only."""
import asyncio
import json
import logging
from pathlib import Path
import subprocess
import sys


async def check_environment(adapter_type=None):
    result = {'backend': 'winrt', 'adapter_available': None,
              'low_energy_supported': None, 'central_role_supported': None,
              'real_write_enabled': False}
    try:
        if adapter_type is None:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            adapter_type = BluetoothAdapter
        adapter = await asyncio.wait_for(adapter_type.get_default_async(), timeout=8)
        result['adapter_available'] = adapter is not None
        if adapter is not None:
            result['low_energy_supported'] = adapter.is_low_energy_supported
            result['central_role_supported'] = adapter.is_central_role_supported
        return result
    except (ImportError, OSError, RuntimeError, TimeoutError) as exc:
        result['error_type'] = type(exc).__name__
        return result


def run_worker(action, timeout):
    """Bound native WinRT calls that can block before an asyncio timeout."""
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), action],
            capture_output=True, encoding='utf-8', timeout=timeout, check=False)
        if process.returncode == 0:
            return json.loads(process.stdout)
        error_type = 'ProbeProcessFailed'
    except subprocess.TimeoutExpired:
        error_type = 'ProbeProcessTimeout'
    except (ValueError, OSError):
        error_type = 'ProbeProcessUnavailable'
    return {'backend': 'winrt', 'adapter_available': None,
            'low_energy_supported': None, 'central_role_supported': None,
            'real_write_enabled': False, 'error_type': error_type}


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    if sys.argv[1:] == ['--probe-worker']:
        result = asyncio.run(check_environment())
    elif sys.argv[1:] == ['--inspect-worker']:
        from ble_readonly import inspect_bound_device, load_binding
        try:
            result = asyncio.run(inspect_bound_device(load_binding()))
        except (OSError, ValueError) as exc:
            result = {'status': 'private_binding_unavailable',
                      'real_write_enabled': False, 'error_type': type(exc).__name__}
    elif sys.argv[1:] == ['inspect']:
        result = run_worker('--inspect-worker', 35)
    else:
        result = run_worker('--probe-worker', 12)
    print(json.dumps(result, indent=2))
