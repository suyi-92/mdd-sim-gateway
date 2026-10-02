"""A native build must prove its MMS binary dependencies work before activation."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")


class ControlImportsTests(unittest.TestCase):
    def probe(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python"
            python.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "$PROBE_LOG"\nexit {result}\n')
            python.chmod(0o755)
            function = re.search(r"^verify_control_imports\(\) \{\n.*?^\}\n", INSTALL,
                                 re.M | re.S).group(0)
            log = Path(tmp) / "probe.log"
            done = subprocess.run(["bash", "-c", 'warn() { echo "$*" >&2; };\n' + function
                                   + '\nverify_control_imports "$PROBE_PYTHON"'],
                                  capture_output=True, text=True,
                                  env={**os.environ, "PROBE_PYTHON": str(python),
                                       "PROBE_LOG": str(log)})
            return done, log.read_text()

    def test_usable_native_libraries_pass(self):
        done, command = self.probe(0)
        self.assertEqual(done.returncode, 0, done.stderr)
        for module in ("PIL.Image", "pi_heif", "websockets"):
            self.assertIn(module, command)

    def test_installed_but_unimportable_library_refuses_activation(self):
        done, _ = self.probe(1)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("activation refused", done.stderr)

    def test_both_new_and_cached_builds_are_probed(self):
        self.assertIn('verify_control_imports "$temp/venv/bin/python" || die', INSTALL)
        self.assertIn('verify_control_imports "$root/venv/bin/python" || return 1', INSTALL)


if __name__ == "__main__":
    unittest.main()
