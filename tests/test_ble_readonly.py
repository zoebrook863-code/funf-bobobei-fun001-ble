from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ble_readonly import inspect_bound_device, load_binding
from fun001_protocol import SERVICE_UUID, WRITE_UUID, NOTIFY_UUID


class ReadonlyDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_discovery_has_no_write_or_subscription(self):
        events = []
        device = SimpleNamespace(address='00:00:00:00:00:01')

        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                events.append('scan')
                return device if predicate(device, None) else None

        class Client:
            def __init__(self, found, **kwargs):
                self.services = self
                self.asserted_pair = kwargs['pair']
                events.append(('pair', self.asserted_pair))

            async def __aenter__(self):
                events.append('connect')
                return self

            async def __aexit__(self, *args):
                events.append('disconnect')

            def get_service(self, uuid):
                if uuid != SERVICE_UUID:
                    raise AssertionError('Unexpected service')
                return SimpleNamespace(characteristics=[
                    SimpleNamespace(uuid=WRITE_UUID, properties=['write']),
                    SimpleNamespace(uuid=NOTIFY_UUID, properties=['notify'])])

            async def write_gatt_char(self, *args, **kwargs):
                raise AssertionError('Read-only discovery attempted a write')

            async def start_notify(self, *args, **kwargs):
                raise AssertionError('Read-only discovery attempted a subscription')

        result = await inspect_bound_device(device.address, Scanner, Client)
        self.assertEqual(result['status'], 'readonly_gatt_verified')
        self.assertTrue(result['connection_closed'])
        self.assertFalse(result['real_write_enabled'])
        self.assertEqual(events, ['scan', ('pair', False), 'connect', 'disconnect'])
        self.assertNotIn(device.address, str(result))

    async def test_no_name_based_fallback(self):
        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                other = SimpleNamespace(address='00:00:00:00:00:02', name='SOSEXY')
                return other if predicate(other, None) else None
        result = await inspect_bound_device('00:00:00:00:00:01', Scanner, object)
        self.assertEqual(result['status'], 'bound_device_not_found')

    async def test_driver_errors_are_redacted(self):
        class Scanner:
            @staticmethod
            async def find_device_by_filter(predicate, timeout):
                raise OSError('sensitive-address-hidden')
        result = await inspect_bound_device('00:00:00:00:00:01', Scanner, object)
        self.assertEqual(result['error_type'], 'OSError')
        self.assertNotIn('sensitive-address-hidden', str(result))

    def test_binding_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binding.json'
            for content in ('[]', '{}', '{"address":"invalid"}', 'invalid json'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_binding(path)
