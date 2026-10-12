"""Native candidate preparation fails before publishing READY on bad Python/codecs."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / 'install.sh').read_text()

class SetupVenvTests(unittest.TestCase):
    def run_prepare(self, fail):
        start = INSTALL.index('  info "building Control virtual environment"')
        end = INSTALL.index('  build_lpac ', start)
        block = INSTALL[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'venv/bin').mkdir(parents=True)
            (root / 'venv/bin/pip').write_text('#!/bin/sh\necho "$*" >> "$LOG"\ncase "$*" in *"$FAIL"*) exit 7;; esac\n')
            (root / 'venv/bin/python').write_text('#!/bin/sh\necho codec >> "$LOG"\n[ "$FAIL" != codec ]\n')
            for name in ('pip', 'python'):
                (root / 'venv/bin' / name).chmod(0o700)
            script = 'set -e\ninfo() { :; }\npython3() { :; }\n' + block + '\ntouch "$temp/READY"\n'
            done = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                env={**os.environ, 'temp': str(root), 'source_dir': str(ROOT),
                     'FAIL': fail, 'LOG': str(root/'calls')})
            return done.returncode, (root/'READY').exists(), (root/'calls').read_text()

    def test_valid_dependencies_and_decoders_allow_candidate_progress(self):
        code, ready, calls = self.run_prepare('never-fail')
        self.assertEqual(code, 0)
        self.assertTrue(ready)
        self.assertIn('check', calls)
        self.assertIn('codec', calls)

    def test_dependency_install_check_or_decode_failure_prevents_ready(self):
        for failure in ('install', 'check', 'codec'):
            with self.subTest(failure=failure):
                code, ready, _ = self.run_prepare(failure)
                self.assertNotEqual(code, 0)
                self.assertFalse(ready)

    def test_relocated_and_reused_generations_repeat_actual_codec_check(self):
        self.assertEqual(INSTALL.count('"$root/venv/bin/python" "$source/tools/check-mms-codecs.py"'), 1)
        moved = INSTALL.index('relocate_venv "$build_root/venv"')
        proof = INSTALL.index('"$build_root/venv/bin/python" "$source_dir/tools/check-mms-codecs.py"', moved)
        ready = INSTALL.index('touch "$build_root/READY"', moved)
        self.assertLess(proof, ready)
