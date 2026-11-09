"""Scan and discover the single privately bound device without app writes."""
import asyncio
import json
from pathlib import Path
import re

from fun001_protocol import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID

BINDING_FILE = Path(__file__).resolve().parent / 'private' / 'device' / 'binding.json'


def load_binding(path=BINDING_FILE):
    binding = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(binding, dict):
        raise ValueError('Invalid private binding')
    address = binding.get('address')
    if not isinstance(address, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', address):
        raise ValueError('Invalid private binding')
    return address.upper()


def verified_write_characteristic(services):
    service = services.get_service(SERVICE_UUID)
    if service is None:
        return None
    characteristics = {c.uuid.lower(): c for c in service.characteristics}
    write = characteristics.get(WRITE_UUID)
    notify = characteristics.get(NOTIFY_UUID)
    if (write is not None and notify is not None
            and 'write' in write.properties and 'notify' in notify.properties):
        return write
    return None


async def inspect_bound_device(address, scanner=None, client_type=None):
    result = {'status': 'unknown', 'device_state': 'unknown',
              'real_write_enabled': False, 'connection_closed': False}
    try:
        if scanner is None or client_type is None:
            from bleak import BleakClient, BleakScanner
            scanner = BleakScanner
            client_type = BleakClient
        device = await scanner.find_device_by_filter(
            lambda device, adv: device.address.upper() == address, timeout=10)
        if device is None:
            result['status'] = 'bound_device_not_found'
            return result
        async with client_type(device, pair=False, timeout=15,
                               winrt={'use_cached_services': False}) as client:
            service = client.services.get_service(SERVICE_UUID)
            if service is None:
                result['status'] = 'gatt_mismatch'
            else:
                write = verified_write_characteristic(client.services)
                result['status'] = 'readonly_gatt_verified' if write is not None else 'gatt_mismatch'
                result['service_uuid'] = SERVICE_UUID
                result['characteristics'] = [
                    {'uuid': c.uuid.lower(), 'properties': list(c.properties)}
                    for c in service.characteristics]
        result['connection_closed'] = True
    except Exception as exc:
        # Driver messages may contain addresses. Only emit the exception type.
        result['status'] = 'inspection_error'
        result['error_type'] = type(exc).__name__
    return result
