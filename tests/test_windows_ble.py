from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from windows_ble import check_environment


class WindowsProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_available_adapter_capabilities_without_identifier(self):
        class FakeAdapter:
            @staticmethod
            async def get_default_async():
                return SimpleNamespace(is_low_energy_supported=True,
                                       is_central_role_supported=True)
        result = await check_environment(FakeAdapter)
        self.assertTrue(result['adapter_available'])
        self.assertTrue(result['low_energy_supported'])
        self.assertFalse(result['real_write_enabled'])
        self.assertNotIn('address', result)

    async def test_absent_adapter_is_distinct_from_unknown(self):
        class FakeAdapter:
            @staticmethod
            async def get_default_async():
                return None
        result = await check_environment(FakeAdapter)
        self.assertFalse(result['adapter_available'])
        self.assertIsNone(result['low_energy_supported'])

    async def test_error_does_not_expose_message_or_identifiers(self):
        class FakeAdapter:
            @staticmethod
            async def get_default_async():
                raise PermissionError('sensitive-address-hidden')
        result = await check_environment(FakeAdapter)
        self.assertIsNone(result['adapter_available'])
        self.assertEqual(result['error_type'], 'PermissionError')
        self.assertNotIn('sensitive-address-hidden', str(result))
