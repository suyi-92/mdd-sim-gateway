"""Behavioral coverage for the authenticated local backup and restore control path."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from control.app import config, operations
from host import mdd_orchestrator

try:
    from control.app import main
except ImportError:  # source-only hosts need not have the complete Control venv
    main = None


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
WORKER_SPEC = importlib.util.spec_from_file_location(
    "mdd_backup_worker", ROOT / "scripts" / "mdd_backup_worker.py")
assert WORKER_SPEC and WORKER_SPEC.loader
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)


class BackupOperationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.state = self.root / "state"
        self.backups = self.root / "backups"
        for path in (self.data, self.state, self.backups):
            path.mkdir(mode=0o700)
        patcher = patch.object(config, "DATA_DIR", str(self.data))
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {
            "MDD_STATE_DIR": str(self.state),
            "MDD_BACKUP_DIR": str(self.backups),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write_pair(self, name: str = "mdd-data-20260903T000000Z.tar.gz") -> str:
        archive = self.backups / name
        archive.write_bytes(b"archive")
        archive.chmod(0o600)
        checksum = self.backups / f"{name}.sha256"
        checksum.write_text("0" * 64 + f"  {name}\n", encoding="ascii")
        checksum.chmod(0o600)
        return name


class BackupInventoryTests(BackupOperationFixture):
    def test_inventory_returns_only_complete_private_regular_pairs_without_host_paths(self):
        valid = self.write_pair()
        (self.backups / "incomplete.tar.gz").write_bytes(b"missing checksum")
        unsafe = self.write_pair("unsafe.tar.gz")
        (self.backups / unsafe).chmod(0o644)
        target = self.backups / "outside"
        target.write_bytes(b"outside")
        (self.backups / "linked.tar.gz").symlink_to(target)
        (self.backups / "linked.tar.gz.sha256").write_text("unused", encoding="ascii")

        items = operations.list_local_backups()

        self.assertEqual([item["name"] for item in items], [valid])
        self.assertEqual(set(items[0]), {"name", "size_bytes", "created_at", "kind"})
        self.assertNotIn(str(self.backups), json.dumps(items))

    def test_writable_managed_backup_directory_is_rejected(self):
        self.backups.chmod(0o770)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            operations.list_local_backups()

    def test_restore_request_rejects_traversal_missing_sidecar_and_open_permissions(self):
        with self.assertRaisesRegex(ValueError, "invalid backup name"):
            operations.request_backup_operation("restore", "../escape.tar.gz")
        archive = self.backups / "missing.tar.gz"
        archive.write_bytes(b"archive")
        archive.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "checksum"):
            operations.request_backup_operation("restore", archive.name)
        unsafe = self.write_pair("unsafe.tar.gz")
        (self.backups / unsafe).chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions"):
            operations.request_backup_operation("restore", unsafe)


class BackupRequestTests(BackupOperationFixture):
    def test_create_request_uses_a_generated_basename_and_private_state(self):
        with patch.object(operations.secrets, "token_hex", return_value="0123456789abcdef"):
            result = operations.request_backup_operation("create")

        request_path = self.data / "orchestrator" / "backup-operation-request.json"
        status_path = self.state / "backup-operation-status.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertRegex(request["backup_name"],
                         r"^mdd-data-\d{8}T\d{6}Z-0123456789abcdef\.tar\.gz$")
        self.assertEqual(result["operation_id"], "0123456789abcdef")
        self.assertEqual(request_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(status_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(operations.backup_operation_status()["state"], "requested")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            operations.request_backup_operation("create")

    def test_restore_request_contains_only_the_selected_safe_basename(self):
        name = self.write_pair()

        result = operations.request_backup_operation("restore", name)

        request = json.loads((self.data / "orchestrator" /
                              "backup-operation-request.json").read_text())
        self.assertEqual(request["action"], "restore")
        self.assertEqual(request["backup_name"], name)
        self.assertNotIn(str(self.backups), json.dumps(result))


@unittest.skipIf(main is None, "manager runtime dependencies are unavailable")
class BackupApiTests(BackupOperationFixture):
    def test_restore_requires_the_exact_confirmation_phrase_before_publication(self):
        with patch.object(operations, "request_backup_operation") as request:
            with self.assertRaises(HTTPException) as raised:
                main.api_system_backup_restore("safe.tar.gz", {"confirm": "yes"})
            self.assertEqual(raised.exception.status_code, 400)
            request.assert_not_called()

            request.return_value = {"ok": True}
            self.assertEqual(
                main.api_system_backup_restore("safe.tar.gz", {"confirm": "RESTORE"}),
                {"ok": True},
            )
            request.assert_called_once_with("restore", "safe.tar.gz")

    def test_inventory_api_exposes_closed_records_and_operation_status(self):
        with patch.object(operations, "list_local_backups", return_value=[{"name": "safe"}]), \
                patch.object(operations, "backup_operation_status",
                             return_value={"state": "idle"}):
            self.assertEqual(main.api_system_backups(), {
                "backups": [{"name": "safe"}], "operation": {"state": "idle"},
            })


class OrchestratorBackupLaunchTests(BackupOperationFixture):
    def request(self, **changes):
        value = {
            "operation_id": "0123456789abcdef",
            "action": "create",
            "backup_name": "mdd-data-20260903T000000Z-0123456789abcdef.tar.gz",
            "requested_at": 1,
        }
        value.update(changes)
        operations._write_private_json(
            self.data / "orchestrator" / "backup-operation-request.json", value)

    def test_valid_request_launches_exact_worker_arguments_without_a_shell(self):
        self.request()
        app = mdd_orchestrator.Orchestrator(
            self.data, ROOT, dry_run=False, state=self.state, backup=self.backups)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(mdd_orchestrator, "run", side_effect=[completed, completed]) as run:
            app.process_backup_operation_request()

        command = run.call_args_list[1].args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--property=Type=exec", command)
        self.assertIn(str(ROOT / "scripts" / "mdd_backup_worker.py"), command)
        self.assertIn(str(self.state), command)
        self.assertIn(str(self.backups), command)
        self.assertNotIn("sh", command)
        self.assertNotIn("-c", command)
        self.assertFalse((self.data / "orchestrator" /
                          "backup-operation-request.json").exists())
        status = json.loads((self.state / "backup-operation-status.json").read_text())
        self.assertEqual(status["state"], "launching")

    def test_extra_fields_and_path_shaped_names_fail_before_systemd_run(self):
        for changes in ({"extra": "value"}, {"backup_name": "../escape.tar.gz"}):
            self.request(**changes)
            app = mdd_orchestrator.Orchestrator(
                self.data, ROOT, dry_run=False, state=self.state, backup=self.backups)
            with patch.object(mdd_orchestrator, "run") as run:
                app.process_backup_operation_request()
            run.assert_not_called()
            status = json.loads((self.state / "backup-operation-status.json").read_text())
            self.assertEqual(status["error_code"], "backup.error.invalid_request")


class DetachedWorkerTests(BackupOperationFixture):
    operation_id = "0123456789abcdef"

    def prepare_status(self, action: str, name: str):
        operations._write_private_json(self.state / "backup-operation-status.json", {
            "operation_id": self.operation_id,
            "action": action,
            "backup_name": name,
            "state": "launching",
            "updated_at": 1,
        })

    def test_create_runs_only_mddctl_with_the_generated_output_and_publishes_success(self):
        name = "mdd-data-20260903T000000Z-0123456789abcdef.tar.gz"
        self.prepare_status("create", name)
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(worker.subprocess, "run", return_value=completed) as run:
            code = worker.run_operation(
                action="create", operation_id=self.operation_id, backup_name=name,
                state_dir=str(self.state), backup_dir=str(self.backups))

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            ["/usr/local/sbin/mddctl", "backup", "--output", str(self.backups / name)],
            check=False,
        )
        status = json.loads((self.state / "backup-operation-status.json").read_text())
        self.assertEqual(status["state"], "success")

    def test_restore_uses_only_a_verified_private_pair_and_preserves_failure(self):
        name = self.write_pair()
        self.prepare_status("restore", name)
        completed = subprocess.CompletedProcess([], 7)
        with patch.object(worker.subprocess, "run", return_value=completed) as run:
            code = worker.run_operation(
                action="restore", operation_id=self.operation_id, backup_name=name,
                state_dir=str(self.state), backup_dir=str(self.backups))

        self.assertEqual(code, 7)
        run.assert_called_once_with(
            ["/usr/local/sbin/mddctl", "restore", "--input", str(self.backups / name)],
            check=False,
        )
        status = json.loads((self.state / "backup-operation-status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error_code"], "backup.error.restore_failed")

    def test_missing_restore_pair_fails_without_invoking_mddctl(self):
        name = "missing.tar.gz"
        self.prepare_status("restore", name)
        with patch.object(worker.subprocess, "run") as run:
            code = worker.run_operation(
                action="restore", operation_id=self.operation_id, backup_name=name,
                state_dir=str(self.state), backup_dir=str(self.backups))
        self.assertEqual(code, 1)
        run.assert_not_called()
        status = json.loads((self.state / "backup-operation-status.json").read_text())
        self.assertEqual(status["error_code"], "backup.error.invalid_backup")


class BackupUiContractTests(unittest.TestCase):
    def test_settings_offers_local_create_and_confirmed_restore_without_host_path_input(self):
        view = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
        api = (ROOT / "webui/src/api.js").read_text(encoding="utf-8")
        css = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")

        for method in ("backups:", "backupOperation:", "createBackup:", "restoreBackup:"):
            self.assertIn(method, api)
        self.assertIn("window.prompt(t('Type RESTORE", view)
        self.assertIn("api.createBackup()", view)
        self.assertIn("api.restoreBackup(name)", view)
        self.assertNotIn('type="file"', view)
        self.assertIn(".u-backup-copy { display:grid; min-width:0", css)
        self.assertIn(".u-backup-row { align-items:stretch; flex-direction:column", css)

    @unittest.skipUnless(NODE, "Node.js is required for the backup row state test")
    def test_only_the_selected_archive_reports_restoring(self):
        script = r"""
import { activeBackupOperation } from './webui/src/backup-operation.js'
const names = ['first.tar.gz', 'second.tar.gz', 'third.tar.gz']
const local = activeBackupOperation(
  { action: 'restore', backupName: 'second.tar.gz' },
  { state: 'idle' },
)
const remote = activeBackupOperation(null, {
  state: 'running', action: 'restore', backup_name: 'third.tar.gz',
})
process.stdout.write(JSON.stringify({
  local: names.map(name => local.action === 'restore' && local.backupName === name),
  remote: names.map(name => remote.action === 'restore' && remote.backupName === name),
}))
"""
        completed = subprocess.run(
            [NODE, "--input-type=module", "--eval", script],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )
        self.assertEqual(json.loads(completed.stdout), {
            "local": [False, True, False],
            "remote": [False, False, True],
        })


if __name__ == "__main__":
    unittest.main()
