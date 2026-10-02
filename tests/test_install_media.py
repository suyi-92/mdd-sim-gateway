"""The VMware media entry point uses the managed native venv and local Docker."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGER = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")


class NativeMediaCliTests(unittest.TestCase):
    def test_media_runs_with_native_data_and_does_not_inherit_remote_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "control").mkdir()
            (root / ".venv/bin").mkdir(parents=True)
            python = root / ".venv/bin/python"
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$MDD_DATA" '
                              '"$DOCKER_HOST" "${DOCKER_CONTEXT:-unset}" "$@"\n')
            python.chmod(0o755)
            function = re.search(r"^media_cli\(\) \{\n.*?^\}\n", MANAGER,
                                 re.M | re.S).group(0)
            done = subprocess.run(["bash", "-c", function + '\nmedia_cli relay --port 8478'],
                                  capture_output=True, text=True,
                                  env={**os.environ, "INSTALL_DIR": str(root),
                                       "DATA_DIR": str(root / "data"),
                                       "ENGINE_STABLE_IMAGE": "mdd-sim-gateway/engine:latest",
                                       "DOCKER_HOST": "tcp://remote:2375",
                                       "DOCKER_CONTEXT": "remote"})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.splitlines(), [str(root / "control"),
                             str(root / "data"), "unix:///var/run/docker.sock", "unset",
                             "-m", "app.media", "relay", "--port", "8478"])

    def test_media_command_validates_the_active_generation_before_running(self):
        body = re.search(r"^cmd_media\(\) \{\n.*?^\}\n", MANAGER, re.M | re.S).group(0)
        self.assertLess(body.index("validate_active_generation"), body.index('media_cli "$sub"'))


if __name__ == "__main__":
    unittest.main()
