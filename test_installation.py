"""Timer installation portability without touching real user services."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent

class TestTimerInstallation(unittest.TestCase):
    def test_installer_uses_checkout_and_interpreter_with_spaces_and_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'a project 100%'
            root.mkdir()
            shutil.copy(ROOT / 'install-timer.sh', root / 'install-timer.sh')
            shutil.copytree(ROOT / 'systemd', root / 'systemd')
            # The installer validates the selected interpreter; use the real one.
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            (root / '.venv').symlink_to(Path(sys.prefix), target_is_directory=True)
            for name in ['systemctl', 'loginctl']:
                script = bin_dir / name
                script.write_text('#!/bin/bash\necho Linger=yes\nexit 0\n')
                script.chmod(0o700)
            config = Path(tmp) / 'config'
            env = os.environ.copy()
            env.update(PATH=str(bin_dir) + ':' + env['PATH'], XDG_CONFIG_HOME=str(config))
            result = subprocess.run(['bash', 'install-timer.sh'], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            service = (config / 'systemd/user/todays-brief.service').read_text()
            self.assertIn(str(root).replace('%', '%%'), service)
            self.assertIn(str(root / '.venv/bin').replace('%', '%%'), service)
            self.assertNotIn('@PROJECT_ROOT@', service)
            self.assertNotIn('Documents/News', service)
            self.assertIn('EnvironmentFile=-%h/.config/todays-brief/env', service)

if __name__ == '__main__':
    unittest.main()
