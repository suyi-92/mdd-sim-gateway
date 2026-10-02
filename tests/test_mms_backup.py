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
from tests.test_mdd_archive import mdd_archive, COMMIT, CREATED_AT


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
    """mddctl stops writers, then archives the complete quiesced VMware data tree."""

    def backup(self, live, archive):
        return mdd_archive.create_backup(live, archive, version="1.13.1-vmware.1",
                                         source_commit=COMMIT, created_at=CREATED_AT)

    def test_transient_updates_are_left_out_of_the_native_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live):
                update = live / "update"
                update.mkdir()
                (update / "temporary-download").write_bytes(b"not-runtime-state")
                archive_path = Path(temp, "backup.tar.gz")
                self.backup(live, archive_path)
            with tarfile.open(archive_path) as archive:
                self.assertFalse(any(n.startswith("data/update") for n in archive.getnames()))

    def test_full_native_backup_preserves_history_and_attachments_but_omits_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live), patch.object(config, "DATA_DIR", str(live)):
                (live / "config.yaml").write_text("settings: {}\ninstances: {}\n")
                mid = add_mms()
                stray = Path(store.mms_dir()) / str(mid) / "unreferenced.jpg"
                stray.write_bytes(b"unreferenced-but-preserved-native-data")
                draft = mms_staging.stage("1", "draft.jpg", "image/jpeg", b"draft")
                archive_path = Path(temp, "backup.tar.gz")
                self.backup(live, archive_path)
            with tarfile.open(archive_path) as archive:
                names = archive.getnames()
            restored = Path(temp, "restored")
            mdd_archive.safe_extract(archive_path, restored)
            self.assertIn("data/config.yaml", names)
            self.assertIn(f"data/mms/{mid}/unreferenced.jpg", names)
            self.assertFalse(any(draft["id"] in n for n in names))
            self.assertEqual(read_back(restored / "data", mid), [b"hi", b"\xff\xd8\xff-picture"])

    def test_attachment_disappearing_during_the_native_archive_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live):
                mid = add_mms()
                attachment = next((Path(store.mms_dir()) / str(mid)).iterdir())
                real_open = mdd_archive.os.open

                def disappear(path, flags, *args, **kwargs):
                    if Path(path) == attachment:
                        attachment.unlink()
                    return real_open(path, flags, *args, **kwargs)

                with patch.object(mdd_archive.os, "open", side_effect=disappear):
                    with self.assertRaises(FileNotFoundError):
                        self.backup(live, Path(temp, "backup.tar.gz"))

    def test_an_already_missing_attachment_does_not_invent_a_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live):
                mid = add_mms()
                attachment = next((Path(store.mms_dir()) / str(mid)).iterdir())
                relative = attachment.relative_to(live)
                attachment.unlink()
                archive_path = Path(temp, "backup.tar.gz")
                self.backup(live, archive_path)
            restored = Path(temp, "restored")
            mdd_archive.safe_extract(archive_path, restored)
            self.assertFalse((restored / "data" / relative).exists())
            self.assertTrue((restored / "data/mdd-sim-gateway.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
