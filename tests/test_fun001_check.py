from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fun001_check


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, action, arguments=None):
        self.calls.append((action, arguments or {}))
        return {'scope': 'real_local_ble', 'authorization_active': True,
                'session_id': 'secret', 'outcome': 'gatt_ack',
                'link_connected': True}


class CheckHelperTests(unittest.TestCase):
    def test_status_is_redacted_and_stop_uses_the_session(self):
        client = FakeClient()
        status = fun001_check.call(client, 'status')
        self.assertEqual(status['session_id'], 'secret')
        public = fun001_check.public(status)
        self.assertNotIn('session_id', public)
        self.assertEqual(public['outcome'], 'gatt_ack')
        result = fun001_check.call(client, 'stop', {'session_id': 'secret'})
        self.assertEqual(result['outcome'], 'gatt_ack')
        self.assertEqual(client.calls[-1], ('stop', {'session_id': 'secret'}))


if __name__ == '__main__':
    unittest.main()
