"""Secondary AT rule changes stay within activation and have usable rollback material."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from scripts import mdd_udev as rules

ROOT = Path(__file__).resolve().parents[1]

class RuleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.path = self.root / rules.RULE_NAME
        self.backup = self.root / 'restore.json'
        self.run = Mock(return_value=SimpleNamespace(returncode=0))

    def test_install_reapply_idempotence_and_uninstall(self):
        rules.replace(self.path, rules.RULE, self.run)
        self.assertEqual(self.path.read_bytes(), rules.RULE)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertIn('ID_MM_PORT_TYPE_AT_SECONDARY', self.path.read_text())
        self.assertNotIn('bInterfaceNumber', self.path.read_text())
        self.assertIn(['systemctl', 'restart', 'ModemManager.service'], [c.args[0] for c in self.run.call_args_list])
        self.run.reset_mock(); rules.replace(self.path, rules.RULE, self.run)
        self.run.assert_not_called()
        rules.replace(self.path, None, self.run); self.assertFalse(self.path.exists())

    def test_snapshot_and_restore_existing_and_absent_rule(self):
        for old in (None, b'# prior administrator rule\n'):
            with self.subTest(present=old is not None):
                self.backup.unlink(missing_ok=True)
                if old: self.path.write_bytes(old); self.path.chmod(0o644)
                rules.snapshot(self.path, self.backup)
                self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
                rules.replace(self.path, rules.RULE, self.run)
                rules.restore(self.path, self.backup, self.run)
                self.assertEqual(rules.read_rule(self.path), old)
                self.path.unlink(missing_ok=True)

    def test_apply_failure_restores_previous_content_before_returning(self):
        self.path.write_bytes(b'# previous\n'); self.path.chmod(0o644)
        self.run.side_effect = [subprocess.CalledProcessError(1, 'udevadm')] + [SimpleNamespace(returncode=0)] * 8
        with self.assertRaises(subprocess.CalledProcessError):
            rules.replace(self.path, rules.RULE, self.run)
        self.assertEqual(self.path.read_bytes(), b'# previous\n')

    def test_restore_refuses_changed_backup_or_concurrent_admin_edit(self):
        rules.snapshot(self.path, self.backup); rules.replace(self.path, rules.RULE, self.run)
        value=json.loads(self.backup.read_text()); value['sha256']='0'*64
        self.backup.write_text(json.dumps(value))
        with self.assertRaises(ValueError): rules.restore(self.path, self.backup, self.run)
        self.backup.unlink(); rules.snapshot(self.path, self.backup)
        self.path.write_bytes(b'# concurrent edit\n')
        with self.assertRaises(ValueError): rules.restore(self.path, self.backup, self.run)
        self.assertEqual(self.path.read_bytes(), b'# concurrent edit\n')

    def test_full_size_rule_retains_mode_and_rejects_unsafe_metadata(self):
        self.path.write_bytes(b'#' * 65536); self.path.chmod(0o600)
        rules.snapshot(self.path, self.backup)
        rules.replace(self.path, rules.RULE, self.run)
        rules.restore(self.path, self.backup, self.run)
        self.assertEqual(self.path.read_bytes(), b'#' * 65536)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o666)
        with self.assertRaises(ValueError): rules.read_rule(self.path)

    def test_inactive_mm_is_not_started(self):
        self.run.return_value.returncode=3
        rules.replace(self.path, rules.RULE, self.run)
        self.assertFalse(any(c.args[0][:2]==['systemctl','restart'] for c in self.run.call_args_list))

    def test_symlink_rule_is_refused(self):
        target=self.root/'outside'; target.write_bytes(b'untouched'); self.path.symlink_to(target)
        with self.assertRaises(OSError): rules.replace(self.path,rules.RULE,self.run)
        self.assertEqual(target.read_bytes(),b'untouched'); self.run.assert_not_called()

    def test_prepare_cannot_apply_rules_and_activation_requires_new_manager(self):
        source=(ROOT/'install.sh').read_text()
        prepare=source[source.index('prepare_build() {'):source.index('write_managed_state() {')]
        self.assertNotIn('mdd_udev.py',prepare)
        activate=source[source.index('activate_build() {'):source.index('activate_build() {')+7000]
        self.assertLess(activate.index('MDD_UPDATE_MANAGER_ABI'),activate.index('mdd_udev.py'))
        manager=(ROOT/'scripts/mddctl').read_text()
        self.assertIn('"$UPDATE_RULE_HELPER" restore',manager)
        self.assertIn('"$INSTALL_DIR/scripts/mdd_udev.py" remove',manager)
