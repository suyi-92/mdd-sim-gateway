"""Executable safety tests for mddctl data archives."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("mdd_archive", ROOT / "scripts/mdd_archive.py")
assert SPEC and SPEC.loader
mdd_archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mdd_archive)

COMMIT = "1" * 40
CREATED_AT = "2026-09-01T00:00:00Z"


def manifest_bytes(**changes) -> bytes:
    value = {
        "format": 1,
        "kind": "mdd-sim-gateway-data",
        "version": "1.7.0-vmware.1",
        "source_commit": COMMIT,
        "created_at": CREATED_AT,
    }
    value.update(changes)
    return (json.dumps(value) + "\n").encode()


def add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(value)
    member.mode = 0o600
    archive.addfile(member, io.BytesIO(value))


def base_archive(path: Path, extra_members=(), *, manifest: bytes | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        add_bytes(archive, "manifest.json", manifest if manifest is not None else manifest_bytes())
        data = tarfile.TarInfo("data")
        data.type = tarfile.DIRTYPE
        data.mode = 0o700
        archive.addfile(data)
        for member, payload in extra_members:
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)


class SQLiteSafetyTests(unittest.TestCase):
    def test_wal_is_checkpointed_and_database_is_integrity_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "runtime.sqlite"
            connection = sqlite3.connect(database)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('ok')")
            connection.commit()
            wal = database.with_name(f"{database.name}-wal")
            self.assertTrue(wal.exists())

            checked = mdd_archive.sqlite_check_tree(root)

            self.assertEqual(checked, ["runtime.sqlite"])
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone(), ("ok",))
            self.assertEqual(wal.stat().st_size, 0)
            connection.close()

    def test_corrupt_sqlite_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary, "broken.db")
            database.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(mdd_archive.ArchiveError, "SQLite validation failed"):
                mdd_archive.sqlite_check_tree(database.parent)


class ArchiveRoundTripTests(unittest.TestCase):
    def test_create_and_extract_round_trip_excludes_runtime_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "source"
            data.mkdir()
            (data / "config.json").write_text('{"enabled": true}\n', encoding="utf-8")
            cache = data / "cache"
            cache.mkdir()
            (cache / "ignored").write_text("do not archive", encoding="utf-8")
            connection = sqlite3.connect(data / "state.db")
            connection.execute("CREATE TABLE state(value INTEGER)")
            connection.execute("INSERT INTO state VALUES (13)")
            connection.commit()
            connection.close()
            archive = root / "backup.tar.gz"

            checked = mdd_archive.create_backup(
                data, archive, version="1.7.0-vmware.1",
                source_commit=COMMIT, created_at=CREATED_AT)
            destination = root / "restore"
            restored_manifest = mdd_archive.safe_extract(archive, destination)

            self.assertEqual(checked, ["state.db"])
            self.assertEqual(restored_manifest["source_commit"], COMMIT)
            self.assertEqual((destination / "data/config.json").read_text(encoding="utf-8"),
                             '{"enabled": true}\n')
            self.assertFalse((destination / "data/cache").exists())
            restored = sqlite3.connect(destination / "data/state.db")
            self.assertEqual(restored.execute("SELECT value FROM state").fetchone(), (13,))
            restored.close()

    @unittest.skipIf(os.name == "nt", "Windows test accounts cannot reliably create symlinks")
    def test_backup_rejects_symbolic_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "target").write_text("secret", encoding="utf-8")
            (data / "link").symlink_to(data / "target")
            with self.assertRaisesRegex(mdd_archive.ArchiveError, "symbolic links"):
                mdd_archive.create_backup(
                    data, root / "backup.tar.gz", version="1.7.0-vmware.1",
                    source_commit=COMMIT, created_at=CREATED_AT)


class ArchiveRejectionTests(unittest.TestCase):
    def assert_member_rejected(self, member: tarfile.TarInfo, payload: bytes | None = b"x") -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.tar.gz"
            if payload is not None:
                member.size = len(payload)
            base_archive(archive, [(member, payload)])
            with self.assertRaises(mdd_archive.ArchiveError):
                mdd_archive.safe_extract(archive, root / "restore")

    def test_parent_path_is_rejected(self):
        self.assert_member_rejected(tarfile.TarInfo("data/../../escape"))

    def test_absolute_path_is_rejected(self):
        self.assert_member_rejected(tarfile.TarInfo("/absolute"))

    def test_symbolic_link_is_rejected(self):
        member = tarfile.TarInfo("data/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/shadow"
        self.assert_member_rejected(member, None)

    def test_hard_link_is_rejected(self):
        member = tarfile.TarInfo("data/hardlink")
        member.type = tarfile.LNKTYPE
        member.linkname = "data/file"
        self.assert_member_rejected(member, None)

    def test_device_node_is_rejected(self):
        member = tarfile.TarInfo("data/device")
        member.type = tarfile.CHRTYPE
        member.devmajor = 1
        member.devminor = 3
        self.assert_member_rejected(member, None)

    def test_invalid_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad-manifest.tar.gz"
            base_archive(archive, manifest=manifest_bytes(kind="untrusted"))
            with self.assertRaisesRegex(mdd_archive.ArchiveError, "unsupported backup manifest"):
                mdd_archive.safe_extract(archive, root / "restore")

    def test_corrupt_sqlite_in_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad-sqlite.tar.gz"
            database = tarfile.TarInfo("data/corrupt.db")
            payload = b"not sqlite"
            database.size = len(payload)
            base_archive(archive, [(database, payload)])
            with self.assertRaisesRegex(mdd_archive.ArchiveError, "SQLite validation failed"):
                mdd_archive.safe_extract(archive, root / "restore")


if __name__ == "__main__":
    unittest.main()
