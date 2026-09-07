"""Recovery rollback and actual portable-backup round trips using fictional data."""
from __future__ import annotations

import hashlib
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from control.app import auth, backup_transfer, config, main
from scripts import mdd_admin, mdd_archive


class ApiClient:
    """Exercise the real ASGI middleware without adding HTTP client dependencies."""
    def request(self, method, path, content=b"", headers=None):
        async def invoke():
            result = {"body": bytearray(), "headers": {}}
            received = False
            finished = asyncio.Event()
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": content, "more_body": False}
                await finished.wait()
                return {"type": "http.disconnect"}
            async def send(message):
                if message["type"] == "http.response.start":
                    result["status"] = message["status"]
                    result["headers"] = {key.decode(): value.decode() for key, value in message["headers"]}
                elif message["type"] == "http.response.body":
                    result["body"].extend(message.get("body", b""))
                    if not message.get("more_body", False):
                        finished.set()
            values = {"content-length": str(len(content)), **(headers or {})}
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                     "http_version": "1.1", "scheme": "https", "method": method,
                     "path": path, "raw_path": path.encode(), "query_string": b"",
                     "root_path": "", "headers": [(key.lower().encode(), value.encode()) for key, value in values.items()],
                     "client": ("192.0.2.1", 1234), "server": ("fixture.invalid", 443)}
            await main.app(scope, receive, send)
            body = bytes(result["body"])
            return SimpleNamespace(status_code=result["status"], headers=result["headers"],
                                   content=body, text=body.decode(errors="replace"), json=lambda: json.loads(body))
        return asyncio.run(invoke())

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "source"
        self.state = self.root / "state"
        self.backups = self.root / "backups"
        self.other = self.root / "other-host"
        for path in (self.data, self.state, self.backups, self.other):
            path.mkdir(mode=0o700)
        for target, name, value in (
            (config, "DATA_DIR", str(self.data)),
            (auth, "AUTH_PATH", str(self.data / "auth.json")),
            (auth, "SESSIONS_PATH", str(self.data / "sessions.json")),
            (auth, "_sessions", {}), (auth, "_failures", {}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {
            "MDD_BACKUP_DIR": str(self.backups), "MDD_STATE_DIR": str(self.state),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def archive(self):
        (self.data / "config.yaml").write_text("max_sim_lines: 13\n")
        with sqlite3.connect(self.data / "fixture.sqlite") as database:
            database.execute("CREATE TABLE sample (value TEXT)")
            database.execute("INSERT INTO sample VALUES ('migration-fixture')")
        path = self.backups / "fixture.tar.gz"
        mdd_archive.create_backup(self.data, path, version="1.9.1-vmware.3",
                                 source_commit="a" * 40, created_at="2026-09-08T00:00:00Z")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar = Path(str(path) + ".sha256")
        sidecar.write_text(digest + "  fixture.tar.gz\n")
        sidecar.chmod(0o600)
        return path

    def bundle(self):
        path = self.archive()
        output = self.root / "fixture.mddbackup"
        mdd_archive.export_bundle(path, output)
        return output


class AdministratorRecoveryTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        auth.setup("fixture-old-password", "fixture-admin")
        self.old_token, _csrf = auth.login("fixture-admin", "fixture-old-password", "192.0.2.1")
        self.states = {unit: True for unit in mdd_admin.UNITS}
        self.calls = []

    def service(self, action, unit):
        self.calls.append((action, unit))
        if action == "is-active":
            return self.states[unit]
        self.states[unit] = action == "start"
        return True

    def reset(self, **options):
        return mdd_admin.reset_admin(self.data, self.state, "fixture-new-password",
                                    auth._derive, service=self.service, **options)

    def test_reset_preserves_account_and_data_and_revokes_persistent_sessions(self):
        sentinel = self.data / "config.yaml"
        sentinel.write_text("max_sim_lines: 13\n")
        self.reset(health=lambda: None)
        auth._load_sessions()
        self.assertIsNone(auth.session(self.old_token))
        self.assertIsNone(auth.login("fixture-admin", "fixture-old-password", "192.0.2.1"))
        self.assertIsNotNone(auth.login("fixture-admin", "fixture-new-password", "192.0.2.1"))
        self.assertEqual(sentinel.read_text(), "max_sim_lines: 13\n")
        self.assertTrue(all(self.states.values()))
        for path in (self.data / "auth.json", self.data / "sessions.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.state.glob("admin-reset-*"))), 1)

    def test_health_failure_restores_original_authentication_and_sessions(self):
        originals = {name: (self.data / name).read_bytes() for name in ("auth.json", "sessions.json")}
        with self.assertRaisesRegex(RuntimeError, "fixture health failure"):
            self.reset(health=lambda: (_ for _ in ()).throw(RuntimeError("fixture health failure")))
        for name, content in originals.items():
            self.assertEqual((self.data / name).read_bytes(), content)
        self.assertTrue(all(self.states.values()))
        auth._load_sessions()
        self.assertIsNotNone(auth.session(self.old_token))

    def test_inactive_services_remain_inactive_and_no_health_probe_runs(self):
        self.states = {unit: False for unit in mdd_admin.UNITS}
        self.reset(health=lambda: self.fail("health should not run for an inactive Control"))
        self.assertFalse(any(self.states.values()))
        self.assertTrue(all(action == "is-active" for action, _unit in self.calls))

    def test_invalid_password_and_linked_auth_fail_before_service_changes(self):
        with self.assertRaises(ValueError):
            mdd_admin.reset_admin(self.data, self.state, "short", auth._derive,
                                 service=self.service)
        self.assertEqual(self.calls, [])
        account = self.data / "auth.json"
        account.rename(self.data / "original.json")
        account.symlink_to(self.data / "original.json")
        with self.assertRaises(OSError):
            self.reset(health=lambda: None)
        self.assertEqual(self.calls, [])

    def test_partial_write_failure_rolls_back_and_preserves_missing_session_file(self):
        (self.data / "sessions.json").unlink()
        original = (self.data / "auth.json").read_bytes()
        write = mdd_admin.atomic_write
        failed = False
        def fail_once(path, content):
            nonlocal failed
            if path == self.data / "auth.json" and not failed:
                failed = True
                raise OSError("fixture write failure")
            write(path, content)
        with patch.object(mdd_admin, "atomic_write", side_effect=fail_once), self.assertRaises(OSError):
            self.reset(health=lambda: None)
        self.assertEqual((self.data / "auth.json").read_bytes(), original)
        self.assertFalse((self.data / "sessions.json").exists())
        self.assertTrue(all(self.states.values()))

    def test_manager_validates_generation_before_invoking_recovery_without_password_arguments(self):
        from tests.test_vmware_install_contract import MDDCTL, shell_function
        function = shell_function(MDDCTL, "cmd_reset_admin") + "\n}\n"
        script = '''set -Eeuo pipefail
acquire_lock() { printf 'lock\\n'; }
validate_managed_checkout() { printf 'checkout\\n'; }
validate_active_generation() { printf 'generation\\n'; }
env() { printf 'helper'; printf ' <%s>' "$@"; printf '\\n'; }
die() { exit 7; }
DATA_DIR=/fixture/data STATE_DIR=/fixture/state INSTALL_DIR=/fixture/install
'''
        result = subprocess.run(["bash", "-c", script + function + "cmd_reset_admin --dry-run"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:3], ["lock", "checkout", "generation"])
        self.assertIn("/fixture/install/.venv/bin/python", result.stdout)
        self.assertIn("<--dry-run>", result.stdout)
        result = subprocess.run(["bash", "-c", script + function + "cmd_reset_admin --password invalid"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")


class PortableBackupTests(RecoveryFixture):
    def test_two_host_round_trip_preserves_sqlite_and_import_does_not_restore(self):
        bundle = self.bundle()
        sentinel = self.other / "active-config"
        sentinel.write_text("target-data")
        target_backups = self.other / "backups"
        target_backups.mkdir(mode=0o700)
        name = mdd_archive.import_bundle(bundle, target_backups)
        self.assertEqual(sentinel.read_text(), "target-data")
        imported = target_backups / name
        self.assertEqual(imported.read_bytes(), (self.backups / "fixture.tar.gz").read_bytes())
        stage = self.other / "restore-stage"
        manifest = mdd_archive.safe_extract(imported, stage)
        self.assertEqual(manifest["source_commit"], "a" * 40)
        with sqlite3.connect(stage / "data" / "fixture.sqlite") as database:
            self.assertEqual(database.execute("SELECT value FROM sample").fetchone(), ("migration-fixture",))
        self.assertEqual(imported.stat().st_mode & 0o777, 0o600)
        self.assertEqual(imported.stat().st_nlink, 1)
        self.assertFalse(list(target_backups.glob(".mdd-import-*")))

    def test_export_rejects_corrupt_checksum_without_publishing_and_never_overwrites(self):
        source = self.archive()
        source.write_bytes(b"damaged fixture")
        output = self.root / "export.mddbackup"
        with self.assertRaises(mdd_archive.ArchiveError):
            mdd_archive.export_bundle(source, output)
        self.assertFalse(output.exists())
        output.write_bytes(b"existing artifact")
        with self.assertRaises(FileExistsError):
            mdd_archive.export_bundle(source, output)
        self.assertEqual(output.read_bytes(), b"existing artifact")

    def test_import_rejects_duplicates_traversal_symlinks_and_bad_checksum(self):
        archive = self.archive().read_bytes()
        for variant in ("extra", "duplicate", "symlink", "digest"):
            with self.subTest(variant=variant):
                bundle = self.root / f"{variant}.mddbackup"
                with zipfile.ZipFile(bundle, "w") as output:
                    item = zipfile.ZipInfo("data.tar.gz")
                    if variant == "symlink":
                        item.external_attr = (0o120777 << 16)
                    output.writestr(item, archive)
                    digest = "0" * 64 if variant == "digest" else hashlib.sha256(archive).hexdigest()
                    output.writestr("data.tar.gz.sha256", digest + "  data.tar.gz\n")
                    if variant in {"extra", "duplicate"}:
                        output.writestr("../escape" if variant == "extra" else "data.tar.gz", b"extra")
                bundle.chmod(0o600)
                with self.assertRaises(mdd_archive.ArchiveError):
                    mdd_archive.import_bundle(bundle, self.other)
                self.assertEqual(list(self.other.iterdir()), [])

    def test_import_checks_inner_archive_paths_database_and_resource_limits(self):
        bundle = self.bundle()
        with patch.object(mdd_archive, "TRANSFER_DATA_MAX_BYTES", 1), self.assertRaises(mdd_archive.ArchiveError):
            mdd_archive.import_bundle(bundle, self.other)
        self.assertEqual(list(self.other.iterdir()), [])
        for variant in ("traversal", "sqlite"):
            source = self.root / f"{variant}.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                for name, content in (("manifest.json", b'{}'), ("data", None),
                                      ("data/../escape" if variant == "traversal" else "data/bad.sqlite", b"corrupt")):
                    item = tarfile.TarInfo(name)
                    item.mode = 0o600 if content is not None else 0o700
                    if content is None:
                        item.type = tarfile.DIRTYPE
                        archive.addfile(item)
                    else:
                        if name == "manifest.json":
                            content = json.dumps({"format": 1, "kind": "mdd-sim-gateway-data", "version": "fixture",
                                                  "source_commit": "a" * 40, "created_at": "fixture"}).encode()
                        item.size = len(content)
                        archive.addfile(item, io.BytesIO(content))
            source.chmod(0o600)
            checksum = Path(str(source) + ".sha256")
            checksum.write_text(hashlib.sha256(source.read_bytes()).hexdigest() + f"  {source.name}\n")
            checksum.chmod(0o600)
            package = self.root / f"{variant}.mddbackup"
            mdd_archive.export_bundle(source, package)
            with self.assertRaises(mdd_archive.ArchiveError):
                mdd_archive.import_bundle(package, self.other)
            self.assertEqual(list(self.other.iterdir()), [])


class TransferApiTests(RecoveryFixture):
    def test_download_disconnect_and_upload_disconnect_release_staging_and_transfer_lock(self):
        from starlette.requests import Request, ClientDisconnect
        self.archive()
        response = backup_transfer.export_backup("fixture.tar.gz")
        async def disconnected_send(_message):
            raise OSError("fixture connection closed")
        async def receive():
            return {"type": "http.disconnect"}
        with self.assertRaises(OSError):
            asyncio.run(response({"type": "http", "method": "GET", "headers": [],
                                  "asgi": {"version": "3.0", "spec_version": "2.4"}},
                                 receive, disconnected_send))
        self.assertFalse(backup_transfer._transfer_lock.locked())
        self.assertFalse(list(self.backups.glob(".mdd-transfer-*")))
        request = Request({"type": "http", "method": "POST", "headers": [
            (b"content-type", b"application/octet-stream")]}, receive)
        with self.assertRaises(ClientDisconnect):
            asyncio.run(backup_transfer.import_backup(request))
        self.assertFalse(backup_transfer._transfer_lock.locked())
        self.assertFalse(list(self.backups.glob(".mdd-transfer-*")))

    def test_unauthenticated_download_and_upload_are_denied(self):
        client = ApiClient()
        with patch.object(auth, "session", return_value=None):
            self.assertEqual(client.get("/api/system/backups/fixture.tar.gz/export").status_code, 401)
            self.assertEqual(client.post("/api/system/backups/import", content=b"fixture").status_code, 401)

    def test_authenticated_round_trip_requires_csrf_and_cleans_download_staging(self):
        self.archive()
        client = ApiClient()
        with patch.object(auth, "session", return_value={"csrf": "fixture-csrf"}):
            exported = client.get("/api/system/backups/fixture.tar.gz/export")
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.headers["cache-control"], "no-store")
            self.assertIn(".mddbackup", exported.headers["content-disposition"])
            self.assertFalse(list(self.backups.glob(".mdd-transfer-*")))
            self.assertFalse(backup_transfer._transfer_lock.locked())
            self.assertEqual(client.post("/api/system/backups/import", content=exported.content).status_code, 403)
            headers = {"X-MDD-CSRF-Token": "fixture-csrf", "Content-Type": "application/octet-stream"}
            imported = client.post("/api/system/backups/import", content=exported.content, headers=headers)
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertTrue((self.backups / imported.json()["backup_name"]).exists())
            self.assertFalse((self.data / "orchestrator" / "backup-operation-request.json").exists())
            with patch.object(backup_transfer, "MAX_BYTES", 4):
                rejected = client.post("/api/system/backups/import", content=exported.content, headers=headers)
            self.assertEqual(rejected.status_code, 413)
            rejected = client.post("/api/system/backups/import", content=b"invalid fixture", headers=headers)
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json(), {"detail": "backup.transfer.invalid"})
            self.assertFalse(list(self.backups.glob(".mdd-transfer-*")))
            self.assertFalse(backup_transfer._transfer_lock.locked())


if __name__ == "__main__":
    unittest.main()
