import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from setup_binding import save_binding
from ble_readonly import load_binding

ROOT = Path(__file__).resolve().parents[1]


class BindingSetupTests(unittest.TestCase):
    def test_runtime_paths_stay_under_project_private_directory(self):
        from ble_readonly import BINDING_FILE
        from fun001_service import RUNTIME
        from standing_permit import DIRECTORY
        self.assertEqual(BINDING_FILE, ROOT / 'private' / 'device' / 'binding.json')
        self.assertEqual(RUNTIME, ROOT / 'private' / 'control')
        self.assertEqual(DIRECTORY, ROOT / 'private' / 'standing-control')

    def test_binding_is_canonical_and_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device' / 'binding.json'
            save_binding(' aa:bb:cc:dd:ee:ff ', path)
            self.assertEqual(load_binding(path), 'AA:BB:CC:DD:EE:FF')

    def test_invalid_binding_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device' / 'binding.json'
            for value in (None, '', 'invalid', 'AA:BB:CC:DD:EE', 'AA:BB:CC:DD:EE:FF extra'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    save_binding(value, path)
            self.assertFalse(path.exists())

    def test_existing_binding_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binding.json'
            path.write_text('existing', encoding='utf-8')
            with self.assertRaises(ValueError):
                save_binding('AA:BB:CC:DD:EE:FF', path)
            self.assertEqual(path.read_text(encoding='utf-8'), 'existing')


@unittest.skipUnless(os.name == 'nt', 'Windows startup scripts')
class PortableStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='fun001 release test ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_vbs_check_uses_its_own_directory(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        script = scripts / 'launch-fun001-stack.vbs'
        shutil.copyfile(ROOT / 'scripts' / script.name, script)
        result = subprocess.run(['cscript.exe', '//Nologo', str(script), '/check'],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertIn(str(self.root / 'scripts' / 'start-fun001-component.ps1'), result.stdout)
        self.assertNotIn('control', result.stdout)

    def test_startup_directory_preparation_in_windows_powershell(self):
        script = (ROOT / 'scripts' / 'fun001-startup.ps1').read_text(encoding='utf-8')
        code = re.search(r"\$prepareDirectory = @'\n(.*?)\n'@", script, re.S).group(1)
        source = self.root / 'src'
        source.mkdir()
        shutil.copyfile(ROOT / 'src' / 'local_runtime.py', source / 'local_runtime.py')
        command = "$prepareDirectory = @'\n" + code + "\n'@\n"
        command += "& '" + sys.executable.replace("'", "''") + "' -B -c $prepareDirectory\nexit $LASTEXITCODE"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'private' / 'startup').is_dir())

    def check_tunnel(self, config):
        scripts = self.root / 'scripts'
        scripts.mkdir(exist_ok=True)
        script = scripts / 'start-fun001-tunnel.ps1'
        shutil.copyfile(ROOT / 'scripts' / script.name, script)
        private = self.root / 'private'
        private.mkdir(exist_ok=True)
        (private / 'tunnel.json').write_text(json.dumps(config), encoding='utf-8')
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(script), '-Check'],
                              capture_output=True, text=True, timeout=15)

    def test_installed_vbs_keeps_project_path(self):
        script = self.root / 'installed launcher.vbs'
        project = self.root / 'project with spaces'
        content = (ROOT / 'scripts' / 'launch-fun001-stack.vbs').read_text(encoding='utf-8')
        content = content.replace('root = ""', 'root = "' + str(project).replace('"', '""') + '"')
        script.write_text(content, encoding='utf-16')
        result = subprocess.run(['cscript.exe', '//Nologo', str(script), '/check'],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertIn(str(project / 'scripts' / 'start-fun001-component.ps1'), result.stdout)

    def test_tunnel_check_is_loopback_and_accepts_example(self):
        result = self.check_tunnel({'ssh_target': 'user@example.invalid', 'remote_port': 38473})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['forward'], '127.0.0.1:38473:127.0.0.1:8769')

    def test_tunnel_rejects_ssh_options(self):
        result = self.check_tunnel({'ssh_target': '-oProxyCommand=bad', 'remote_port': 38473})
        self.assertNotEqual(result.returncode, 0)

    def test_tunnel_rejects_port_types_and_extra_fields(self):
        for config in ({'ssh_target': 'example.invalid', 'remote_port': '38473'},
                       {'ssh_target': 'example.invalid', 'remote_port': 1},
                       {'ssh_target': 'example.invalid', 'remote_port': 38473, 'extra': True}):
            with self.subTest(config=config):
                self.assertNotEqual(self.check_tunnel(config).returncode, 0)


if __name__ == '__main__':
    unittest.main()
