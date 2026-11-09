"""Owner-controlled persistent permission for the selected low-level profile."""
import copy
import json
from pathlib import Path
import threading

from local_runtime import secure_directory, write_private

DIRECTORY = Path(__file__).resolve().parent / 'private' / 'standing-control'
CHANNELS = {
    'suction': {'cap': 1, 'modes': [1]},
    'vibration': {'cap': 1, 'modes': [1]},
    'electric': {'cap': 0, 'modes': [1]},
}


class StandingPermit:
    def __init__(self, directory=DIRECTORY):
        self.directory = Path(directory)
        secure_directory(self.directory)
        self.path = self.directory / 'permission.json'
        self.lock = threading.RLock()
        self.generation = 0
        self.data = {'version': 1, 'enabled': False, 'output_pending': False,
                     'suspended_reason': None, 'channels': copy.deepcopy(CHANNELS)}
        if self.path.exists():
            if self.path.stat().st_size > 4096:
                raise ValueError('Invalid standing permission')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.keys() != self.data.keys()
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['enabled']) is not bool
                    or type(data['output_pending']) is not bool
                    or json.dumps(data['channels'], sort_keys=True) != json.dumps(CHANNELS, sort_keys=True)
                    or data['suspended_reason'] not in (None, 'unknown_execution',
                                                         'unclean_output')):
                raise ValueError('Invalid standing permission')
            self.data = data
            if data['output_pending']:
                self.data['suspended_reason'] = 'unclean_output'

    def snapshot(self):
        with self.lock:
            return {**copy.deepcopy(self.data), 'generation': self.generation}

    def _save(self, data):
        write_private(self.path, json.dumps(data, ensure_ascii=False))
        self.data = data

    def enable(self):
        with self.lock:
            self._save({**self.data, 'enabled': True, 'output_pending': False,
                        'suspended_reason': None})
            self.generation += 1

    def revoke(self):
        with self.lock:
            self.data = {**self.data, 'enabled': False}
            self.generation += 1
            self._save(self.data)

    def journal(self, pending, uncertain):
        with self.lock:
            data = {**self.data, 'output_pending': pending}
            if uncertain:
                data['suspended_reason'] = 'unknown_execution'
            if data != self.data:
                self._save(data)

    def suspend(self):
        with self.lock:
            if self.data['suspended_reason'] is None:
                self._save({**self.data, 'suspended_reason': 'unknown_execution'})
