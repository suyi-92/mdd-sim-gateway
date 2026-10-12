"""MMS attachments are part of every history backup, and a restored backup reads them back."""
from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config, mms_staging, store
from scripts import mdd_archive
import json


class _Store:
    def __init__(self, root: Path):
        self.root = root
        self.db = root / "mdd-sim-gateway.sqlite"
        self.patch = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(self.db),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))

    def __enter__(self):
        self.patch.start()
        store.init()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


def add_mms(image: bytes = b"\xff\xd8\xff-picture") -> int:
    rec = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                    subject="", body="hi", transaction_id=f"T{len(image)}")
    store.save_mms_content(rec["id"], [
        {"content_type": "text/plain", "data": b"hi", "charset": "utf-8", "text": "hi"},
        {"content_type": "image/jpeg", "data": image, "name": "photo.jpg"}])
    return rec["id"]


def read_back(root: Path, message_id: int) -> list[bytes]:
    with _Store(root):
        return [p["data"] for p in store.mms_parts_with_data(message_id)]


class MigrationBackupTests(unittest.TestCase):
    def test_the_pre_migration_backup_restores_whole_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live) as ctx:
                mid = add_mms()
                with sqlite3.connect(ctx.db) as db:     # pretend one schema step is pending
                    db.execute(f"PRAGMA user_version={len(store._MIGRATIONS) - 1}")
                store.init()
                backups = sorted(Path(store.backup_dir()).glob("*.sqlite"))
                self.assertEqual(len(backups), 1)
                attachments = backups[0].with_suffix(".mms")
                self.assertTrue(attachments.is_dir())
                self.assertFalse(list(Path(store.backup_dir()).glob("*.partial")))

            restored = Path(temp, "restored")
            restored.mkdir()
            shutil.copy2(backups[0], restored / "mdd-sim-gateway.sqlite")
            shutil.copytree(attachments, restored / "mms")
            self.assertEqual(read_back(restored, mid), [b"hi", b"\xff\xd8\xff-picture"])

    def test_a_part_already_missing_does_not_block_the_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            with _Store(Path(temp)) as ctx:
                mid = add_mms()
                for path in (Path(store.mms_dir()) / str(mid)).iterdir():
                    path.unlink()
                with sqlite3.connect(ctx.db) as db:
                    db.execute(f"PRAGMA user_version={len(store._MIGRATIONS) - 1}")
                store.init()
                self.assertEqual(store.schema_version(), len(store._MIGRATIONS))


class LocalBackupTests(unittest.TestCase):
    def backup(self, live, target):
        return mdd_archive.create_backup(live, target, version="1.13.2-vmware.1",
            source_commit="a" * 40, created_at="2026-10-12T00:00:00Z")

    def test_full_managed_backup_restores_history_and_all_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            live, archive, restored = Path(temp, "live"), Path(temp, "backup.tar.gz"), Path(temp, "restored")
            with _Store(live):
                mid = add_mms()
                self.backup(live, archive)
            mdd_archive.safe_extract(archive, restored)
            self.assertEqual(read_back(restored / "data", mid), [b"hi", b"\xff\xd8\xff-picture"])

    def test_existing_attachment_omitted_during_pack_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            live, archive = Path(temp, "live"), Path(temp, "backup.tar.gz")
            with _Store(live):
                mid = add_mms()
                original = mdd_archive._data_entries
                passes = [0]
                def omit(root):
                    passes[0] += 1
                    for path, metadata in original(root):
                        if passes[0] == 3 and path.name == "part-1.jpg":
                            continue
                        yield path, metadata
                missing = next((live / "mms" / str(mid)).iterdir())
                def omit_actual(root):
                    passes[0] += 1
                    for path, metadata in original(root):
                        if passes[0] == 3 and path == missing: continue
                        yield path, metadata
                with patch.object(mdd_archive, "_data_entries", side_effect=omit_actual):
                    with self.assertRaisesRegex(mdd_archive.ArchiveError, "omitted"):
                        self.backup(live, archive)

    def test_existing_missing_attachment_is_reported_and_restores_as_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            live, archive, restored = Path(temp, "live"), Path(temp, "backup.tar.gz"), Path(temp, "restored")
            with _Store(live):
                mid = add_mms()
                next((live / "mms" / str(mid)).iterdir()).unlink()
                self.backup(live, archive)
            with tarfile.open(archive) as packed:
                manifest = json.load(packed.extractfile("manifest.json"))
            self.assertEqual(len(manifest["mms"]["missing_parts"]), 1)
            mdd_archive.safe_extract(archive, restored)

    def test_tampered_attachment_is_refused_on_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            live, archive = Path(temp, "live"), Path(temp, "backup.tar.gz")
            with _Store(live):
                add_mms(); self.backup(live, archive)
            broken = Path(temp, "broken.tar.gz")
            import io
            with tarfile.open(archive) as source, tarfile.open(broken, "w:gz") as target:
                for member in source:
                    content = source.extractfile(member) if member.isfile() else None
                    if member.name.startswith("data/mms/") and content:
                        data = content.read(); content = io.BytesIO(b"x" * len(data))
                    target.addfile(member, content)
            with self.assertRaisesRegex(mdd_archive.ArchiveError, "attachment digests"):
                mdd_archive.safe_extract(broken, Path(temp, "restored"))

if __name__ == "__main__":
    unittest.main()
