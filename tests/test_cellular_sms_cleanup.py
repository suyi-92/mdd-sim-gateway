"""Physical SMS copies are freed only after exact local preservation, across SIM switches."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

from control.app import cellular_sms, store
from tests.test_cellular_sms_policies import (
    DurableStoreCase, FakeModemManager, Ingest, LINE, MODEM, SIM, Result,
    scanner, sms_object, wap_push_sms,
)

OTHER = [{"id": "4", "iccid": "card-b"}]


def history_snapshot():
    with store._conn() as c:
        return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")]
                for t in ("messages", "message_imports", "message_identities")}


class HistoricalCleanupTests(DurableStoreCase):
    def old_card(self, *, detail=None):
        mm = FakeModemManager()
        path = mm.add(7, detail or sms_object())
        s = scanner(mm, topology_ttl=0, detail_ttl=0)
        s.poll(LINE, Ingest(), policy="keep")
        mm.iccid = "card-b"
        return mm, path, s

    def test_retained_old_card_copy_is_removed_without_reassigning_or_mutating_history(self):
        mm, path, s = self.old_card()
        before = history_snapshot()
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 0)

    def test_exact_original_owner_can_differ_from_legacy_fence_owner(self):
        mm, path, s = self.old_card()
        with store._conn() as c:
            c.execute("UPDATE cellular_sms_objects SET instance='legacy-wrong-line',quarantined=1")
        before = history_snapshot()
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)

    def test_missing_history_is_archived_with_unknown_owner_before_old_object_is_released(self):
        mm, path, s = self.old_card()
        store.clear_messages("3")
        before = history_snapshot()
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            row = c.execute("SELECT owner_instance,raw_json FROM modem_sms_archives").fetchone()
        self.assertEqual(row["owner_instance"], "")
        self.assertEqual(json.loads(row["raw_json"]), sms_object())

    def test_restored_original_history_proves_cleanup_without_an_extra_archive(self):
        mm, path, s = self.old_card()
        mm.iccid = "card-a"
        records = s.discover(LINE)
        store.clear_messages("3")
        self.assertEqual(len(store.reimport_cellular_messages("3", records)), 1)
        mm.iccid = "card-b"
        before = history_snapshot()
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)

    def test_ambiguous_historical_owner_is_not_guessed(self):
        mm, path, s = self.old_card()
        record = s._details[(MODEM, path)][1]
        store.add_imported_message("b" * 64, "different-old-card", "in", record["peer"],
                                   record["body"], record["ts"])
        self.assertIsNone(store.preserved_cellular_sms_owner(
            record["peer"], record["body"], record["ts"]))
        before = history_snapshot()
        s.poll(OTHER, Ingest())
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT owner_instance FROM modem_sms_archives").fetchone()[0], "")

    def test_ordinary_row_without_import_marker_is_not_a_historical_ownership_proof(self):
        ts = cellular_sms._timestamp(sms_object()["properties"]["timestamp"])
        store.add_message("old", "in", "+447700900123", "hello", ts=ts, transport="cellular")
        self.assertIsNone(store.preserved_cellular_sms_owner("+447700900123", "hello", ts))

    def test_history_read_or_archive_failure_preserves_modem_object(self):
        for target in ("preserved_cellular_sms_owner", "archive_cellular_sms_object"):
            with self.subTest(target=target):
                mm, path, s = self.old_card()
                if target == "archive_cellular_sms_object":
                    store.clear_messages("3")
                with patch.object(store, target, side_effect=sqlite3.OperationalError("locked")):
                    self.assertEqual(s.poll(OTHER, Ingest()), [])
                self.assertIn(path, mm.objects)
                self.assertEqual(mm.deletes(), [])

    def test_old_binary_mms_copy_uses_exact_original_durable_payload(self):
        mm, path, s = self.old_card(detail=wap_push_sms())
        with store._conn() as c:
            before = [tuple(r) for r in c.execute("SELECT * FROM binary_sms")]
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        with store._conn() as c:
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM binary_sms")], before)

    def test_old_local_submit_keeps_original_final_history(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(pdu_type="submit", state="sent", timestamp="--"))
        reservation = store.reserve_local_modem_sms(
            "3", "card-a", cellular_sms._content_hash("+447700900123", "hello"),
            "epoch-1", "+447700900123", "hello")
        store.bind_local_modem_sms(reservation, "epoch-1", MODEM, path)
        with store._conn() as c:
            c.execute("UPDATE messages SET status='sent'")
        s = scanner(mm, topology_ttl=0, detail_ttl=0)
        s.poll(LINE, Ingest(), policy="keep")
        mm.iccid = "card-b"
        before = history_snapshot()
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)

    def test_cleared_local_send_is_raw_preserved_without_recreating_its_history(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(pdu_type="submit", state="sent", timestamp="--"))
        reservation = store.reserve_local_modem_sms(
            "3", "card-a", cellular_sms._content_hash("+447700900123", "hello"),
            "epoch-1", "+447700900123", "hello")
        store.bind_local_modem_sms(reservation, "epoch-1", MODEM, path)
        store.clear_messages("3")
        before = history_snapshot()
        self.assertEqual(scanner(mm).poll(LINE, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 1)

    def test_local_pending_draft_is_not_archived_or_removed_even_if_history_was_cleared(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(pdu_type="submit", state="stored", timestamp="--"))
        reservation = store.reserve_local_modem_sms(
            "3", "card-a", cellular_sms._content_hash("+447700900123", "hello"),
            "epoch-1", "+447700900123", "hello")
        store.bind_local_modem_sms(reservation, "epoch-1", MODEM, path)
        store.clear_messages("3")
        self.assertEqual(scanner(mm).poll(LINE, Ingest()), [])
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)


class OpaqueArchiveTests(DurableStoreCase):
    def test_every_complete_non_displayable_class_is_archived_byte_for_byte(self):
        for pdu, state in (("status-report", "received"), ("--", "received"),
                           ("deliver", "received"), ("submit", "stored"),
                           ("submit", "sent"), ("submit", "failed")):
            with self.subTest(pdu=pdu, state=state):
                mm = FakeModemManager()
                detail = sms_object(text="--", data="--", pdu_type=pdu, state=state)
                detail["properties"]["delivery-report"] = {"status": "complete", "reference": 7}
                path = mm.add(7, detail)
                ingest = Ingest()
                s = scanner(mm)
                before = history_snapshot()
                self.assertEqual(s.poll(LINE, ingest), [])
                self.assertEqual(ingest.records, [])
                self.assertEqual(mm.deletes(), [path])
                self.assertEqual(history_snapshot(), before)
                with store._conn() as c:
                    rows = c.execute("SELECT raw_json,owner_instance FROM modem_sms_archives").fetchall()
                self.assertTrue(any(json.loads(r["raw_json"]) == detail and r["owner_instance"] == "3"
                                    for r in rows))

    def test_failed_archive_never_releases_object(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="--"))
        with patch.object(store, "archive_cellular_sms_object", side_effect=sqlite3.OperationalError("full")):
            self.assertEqual(scanner(mm).poll(LINE, Ingest()), [])
        self.assertIn(path, mm.objects)
        self.assertEqual(mm.deletes(), [])

    def test_normal_visible_text_does_not_bypass_ingestion_error_through_raw_archival(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        self.assertEqual(scanner(mm).poll(LINE, Ingest(error=sqlite3.OperationalError("locked"))), [])
        self.assertIn(path, mm.objects)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 0)

    def test_cleared_current_text_is_archived_and_freed_without_resurrecting_history(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        store.clear_messages("3")
        before = history_snapshot()
        self.assertEqual(s.poll(LINE, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            row = c.execute("SELECT raw_json,owner_instance FROM modem_sms_archives").fetchone()
        self.assertEqual(row["owner_instance"], "3")
        self.assertEqual(json.loads(row["raw_json"]), sms_object())

    def test_known_tombstone_cannot_bypass_a_new_ingestion_or_archive_error(self):
        for target in ("ingest", "archive"):
            with self.subTest(target=target):
                mm = FakeModemManager()
                path = mm.add(7, sms_object())
                s = scanner(mm)
                s.poll(LINE, Ingest(), policy="keep")
                store.clear_messages("3")
                if target == "ingest":
                    s.poll(LINE, Ingest(error=sqlite3.OperationalError("locked")))
                else:
                    with patch.object(store, "archive_cellular_sms_object",
                                      side_effect=sqlite3.OperationalError("full")):
                        s.poll(LINE, Ingest())
                self.assertIn(path, mm.objects)
                self.assertEqual(mm.deletes(), [])

    def test_receiving_or_sending_is_neither_archived_nor_removed_regardless_of_age(self):
        for state in ("receiving", "sending"):
            with self.subTest(state=state):
                mm = FakeModemManager()
                path = mm.add(7, sms_object(text="--", state=state))
                s = scanner(mm, wall_clock=lambda: 2_000_000_000)
                self.assertEqual(s.poll(LINE, Ingest()), [])
                self.assertEqual(mm.deletes(), [])
                self.assertIn(path, mm.objects)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 0)


class GenerationGuardTests(DurableStoreCase):
    def stored_record(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        return mm, path, s, s.discover(LINE)[0]

    def test_actual_card_change_blocks_delete(self):
        mm, path, s, record = self.stored_record()
        mm.iccid = "card-b"
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.deletes(), [])


    def test_cached_old_topology_cannot_import_new_card_message_into_old_history(self):
        mm = FakeModemManager()
        s = scanner(mm)
        all_lines = LINE + OTHER
        s.poll(all_lines, Ingest())
        mm.iccid = "card-b"
        mm.add(7, sms_object(text="new card message"))
        self.assertEqual(s.poll(all_lines, Ingest()), [])
        self.assertEqual(store.list_messages("3", "+447700900123"), [])
        # The object present when B is first confirmed has no proven original owner. It is
        # retained raw, while subsequent new paths can be attributed normally to B.
        self.assertEqual(s.poll(all_lines, Ingest()), [])
        mm.add(8, sms_object(text="message after confirmed card transition"))
        self.assertEqual([r["instance"] for r in s.poll(all_lines, Ingest())], ["4"])
        self.assertEqual(store.list_messages("3", "+447700900123"), [])
        self.assertEqual(len(store.list_messages("4", "+447700900123")), 1)

    def test_fence_begin_or_claim_failure_never_imports_or_archives_under_current_line(self):
        for function in ("begin_cellular_sms_fence", "claim_cellular_sms_object"):
            for result in (None, {}):
                with self.subTest(function=function, result=result):
                    mm = FakeModemManager()
                    path = mm.add(7, sms_object())
                    s = scanner(mm)
                    before = history_snapshot()
                    with patch.object(store, function, return_value=result):
                        self.assertEqual(s.poll(LINE, Ingest()), [])
                    self.assertEqual(history_snapshot(), before)
                    self.assertIn(path, mm.objects)
                    self.assertEqual(mm.deletes(), [])
            with self.subTest(function=function, failure="database locked"):
                mm = FakeModemManager()
                path = mm.add(7, sms_object())
                before = history_snapshot()
                with patch.object(store, function, side_effect=sqlite3.OperationalError("locked")):
                    self.assertEqual(scanner(mm).poll(LINE, Ingest()), [])
                self.assertEqual(history_snapshot(), before)
                self.assertIn(path, mm.objects)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 0)

    def test_first_baseline_quarantine_survives_scanner_restart(self):
        mm = FakeModemManager()
        mm.add(7, sms_object())
        ts = cellular_sms._timestamp(sms_object()["properties"]["timestamp"])
        store.add_message("3", "in", "+447700900123", "hello", ts=ts, transport="cellular")
        before = history_snapshot()
        self.assertEqual(scanner(mm).discover(LINE), [])
        self.assertEqual(scanner(mm).discover(LINE), [])
        self.assertEqual(history_snapshot(), before)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT quarantined FROM cellular_sms_objects").fetchone()[0], 1)

    def test_path_reuse_blocks_delete_of_unpreserved_replacement(self):
        mm, path, s, record = self.stored_record()
        mm.objects[path] = sms_object(text="new not stored")
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.deletes(), [])

    def test_missing_or_failed_path_listing_is_not_permission_to_delete(self):
        for listing in ({}, {"modem.messaging.sms": []}, {"modem.messaging.sms": ["invalid"]}):
            mm, path, s, record = self.stored_record()
            def runner(args, **kwargs):
                if "--messaging-list-sms" in args:
                    return Result(json.dumps(listing))
                return mm(args, **kwargs)
            s.runner = runner
            self.assertFalse(s.delete_preserved(record))
            self.assertEqual(mm.deletes(), [])

    def test_changed_unique_owner_blocks_delete_even_with_same_numeric_object(self):
        mm, path, s, record = self.stored_record()
        mm.owner = ":1.99"
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.deletes(), [])

    def test_restart_after_final_check_cannot_redirect_delete_to_replacement_daemon(self):
        mm, path, s, record = self.stored_record()
        def runner(args, **kwargs):
            if len(args) > 6 and args[6] == "Delete":
                mm.owner = ":1.99"
                mm.objects[path] = sms_object(text="replacement daemon message")
            return mm(args, **kwargs)
        s.runner = runner
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.objects[path]["content"]["text"], "replacement daemon message")
        self.assertEqual(mm.deletes(), [path])

    def test_raw_snapshot_change_is_not_covered_by_previous_archive(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="--"))
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        record = s.discover(LINE, include_local_cleanup=True)[0]
        mm.objects[path]["properties"]["delivery-report"] = {"status": "new"}
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.deletes(), [])

class ManagerTransactionLockTests(DurableStoreCase):
    def test_exclusive_manager_transaction_allows_archival_but_defers_device_delete(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="--", pdu_type="status-report"))
        lock_path = Path(store.DATA_DIR) / "manager.lock"
        s = scanner(mm, cleanup_lock_path=str(lock_path))
        with lock_path.open("w") as manager_lock:
            fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(s.poll(LINE, Ingest()), [])
            self.assertIn(path, mm.objects)
            self.assertEqual(mm.deletes(), [])
            with store._conn() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM modem_sms_archives").fetchone()[0], 1)
        # A subsequent normal poll automatically resumes cleanup after update health succeeds.
        self.assertEqual(s.poll(LINE, Ingest()), [])
        self.assertEqual(mm.deletes(), [path])
        self.assertNotIn(path, mm.objects)

    def test_shared_guard_remains_held_during_durable_recheck_and_delete(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        lock_path = Path(store.DATA_DIR) / "manager.lock"
        s = scanner(mm, cleanup_lock_path=str(lock_path))
        s.poll(LINE, Ingest(), policy="keep")
        record = s.discover(LINE)[0]
        checked = []

        def assert_exclusive_is_blocked(stage):
            with lock_path.open("a") as manager_lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            checked.append(stage)

        original_preserved = s._preserved
        def preserved(item):
            assert_exclusive_is_blocked("durable proof")
            return original_preserved(item)
        def runner(args, **kwargs):
            if len(args) > 6 and args[6] == "Delete":
                assert_exclusive_is_blocked("Delete")
            return mm(args, **kwargs)
        s.runner = runner
        with patch.object(s, "_preserved", side_effect=preserved):
            self.assertTrue(s.delete_preserved(record))
        self.assertEqual(checked, ["durable proof", "Delete"])
        self.assertEqual(mm.deletes(), [path])
        with lock_path.open("a") as manager_lock:
            fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lock_open_or_validation_errors_defer_cleanup(self):
        base = Path(store.DATA_DIR)
        target = base / "target"
        target.write_text("unchanged")
        symlink = base / "symlink.lock"
        symlink.symlink_to(target)
        directory = base / "directory.lock"
        directory.mkdir()
        fifo = base / "fifo.lock"
        os.mkfifo(fifo)
        for lock_path in (symlink, directory, fifo, base / "absent-directory" / "manager.lock"):
            with self.subTest(lock_path=lock_path.name):
                mm = FakeModemManager()
                path = mm.add(7, sms_object())
                s = scanner(mm, cleanup_lock_path=str(lock_path))
                s.poll(LINE, Ingest())
                self.assertEqual(mm.deletes(), [])
                self.assertIn(path, mm.objects)
        self.assertEqual(target.read_text(), "unchanged")

    def test_new_lock_is_private_and_wrong_owner_is_rejected(self):
        lock_path = Path(store.DATA_DIR) / "manager.lock"
        with cellular_sms._sms_cleanup_transaction_guard(str(lock_path)) as permitted:
            self.assertTrue(permitted)
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        with patch.object(cellular_sms.os, "geteuid", return_value=os.geteuid() + 1):
            with cellular_sms._sms_cleanup_transaction_guard(str(lock_path)) as permitted:
                self.assertFalse(permitted)


class BaselinePersistenceTests(DurableStoreCase):
    def prepare(self, detail=None):
        mm = FakeModemManager()
        path = mm.add(7, detail or sms_object(text="retained previous card fixture"))
        initial = cellular_sms._timestamp(sms_object()["properties"]["timestamp"]) + 3600
        store.add_message("3", "in", "+447700900111", "current line preexisting history",
                          transport="cellular", ts=initial - 7200)
        return mm, path, initial

    def assert_unknown_archive_without_history_changes(self, mm, path, s, lines, before):
        self.assertEqual(s.poll(lines, Ingest()), [])
        self.assertEqual(history_snapshot(), before)
        self.assertEqual(mm.deletes(), [path])
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT owner_instance FROM modem_sms_archives").fetchone()[0], "")

    def test_claim_failure_does_not_consume_baseline_on_retry_or_store_scanner_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                # Each branch uses its own complete fixture database.
                self.setUp()
                mm, path, initial = self.prepare()
                s = scanner(mm, wall_clock=lambda: initial)
                before = history_snapshot()
                with patch.object(store, "claim_cellular_sms_object",
                                  side_effect=sqlite3.OperationalError("locked")):
                    self.assertEqual(s.poll(LINE, Ingest()), [])
                self.assertIn(path, mm.objects)
                with store._conn() as c:
                    cutoff = c.execute("SELECT baseline_cutoff FROM cellular_sms_modems").fetchone()[0]
                if restart:
                    store.init()
                    s = scanner(mm, wall_clock=lambda: initial + 3600)
                self.assert_unknown_archive_without_history_changes(mm, path, s, LINE, before)
                with store._conn() as c:
                    self.assertEqual(c.execute("SELECT baseline_cutoff FROM cellular_sms_modems").fetchone()[0], cutoff)

    def test_failed_listing_or_detail_is_not_permission_to_import_an_old_object_on_retry(self):
        for fail_flag in ("--messaging-list-sms", "-s"):
            with self.subTest(fail_flag=fail_flag):
                self.setUp()
                mm, path, initial = self.prepare()
                def runner(args, **kwargs):
                    if fail_flag in args:
                        return Result(returncode=1)
                    return mm(args, **kwargs)
                s = scanner(mm, wall_clock=lambda: initial)
                s.runner = runner
                before = history_snapshot()
                self.assertEqual(s.poll(LINE, Ingest()), [])
                self.assertIn(path, mm.objects)
                store.init()
                s = scanner(mm, wall_clock=lambda: initial + 3600)
                self.assert_unknown_archive_without_history_changes(mm, path, s, LINE, before)

    def test_baseline_receiving_object_stays_unknown_after_it_completes_and_scanner_restarts(self):
        mm, path, initial = self.prepare(sms_object(text="--", state="receiving"))
        s = scanner(mm, wall_clock=lambda: initial)
        before = history_snapshot()
        self.assertEqual(s.poll(LINE, Ingest()), [])
        mm.objects[path] = sms_object(text="completed retained previous card fixture")
        store.init()
        s = scanner(mm, wall_clock=lambda: initial + 3600)
        self.assert_unknown_archive_without_history_changes(mm, path, s, LINE, before)

    def test_restart_does_not_advance_cutoff_over_later_current_card_message(self):
        mm, path, initial = self.prepare()
        s = scanner(mm, wall_clock=lambda: initial)
        s.poll(LINE, Ingest())
        timestamp = datetime.fromtimestamp(initial + 60, timezone.utc).isoformat()
        new_path = mm.add(8, sms_object(text="later current card message", timestamp=timestamp))
        store.init()
        s = scanner(mm, wall_clock=lambda: initial + 3600)
        self.assertEqual([r["body"] for r in s.poll(LINE, Ingest())], ["later current card message"])
        self.assertIn(new_path, mm.deletes())

    def test_claim_failure_then_card_switch_quarantines_unknown_existing_paths_durably(self):
        for timestamp in ("--", "2099-01-01T00:00:00+00:00"):
            with self.subTest(timestamp=timestamp):
                self.setUp()
                mm, path, initial = self.prepare(sms_object(text="unknown original card", timestamp=timestamp))
                s = scanner(mm, wall_clock=lambda: initial)
                with patch.object(store, "claim_cellular_sms_object",
                                  side_effect=sqlite3.OperationalError("locked")):
                    self.assertEqual(s.poll(LINE, Ingest()), [])
                before = history_snapshot()
                mm.iccid = "card-b"
                s = scanner(mm, wall_clock=lambda: initial + 10)
                # A read failure after detecting B must retain the snapshot placeholder.
                def unreadable(args, **kwargs):
                    return Result(returncode=1) if "-s" in args else mm(args, **kwargs)
                s.runner = unreadable
                self.assertEqual(s.poll(OTHER, Ingest()), [])
                with store._conn() as c:
                    row = c.execute("SELECT signature,quarantined FROM cellular_sms_objects").fetchone()
                    self.assertEqual(tuple(row), ("", 1))
                store.init()
                s = scanner(mm, wall_clock=lambda: initial + 3600)
                self.assert_unknown_archive_without_history_changes(mm, path, s, OTHER, before)

    def test_failed_switch_listing_keeps_old_generation_until_snapshot_can_be_quarantined(self):
        mm, path, initial = self.prepare(sms_object(text="--", state="receiving", timestamp="--"))
        s = scanner(mm, wall_clock=lambda: initial)
        self.assertEqual(s.poll(LINE, Ingest()), [])
        before = history_snapshot()
        mm.iccid = "card-b"
        s = scanner(mm, wall_clock=lambda: initial + 10)
        def no_listing(args, **kwargs):
            return Result(returncode=1) if "--messaging-list-sms" in args else mm(args, **kwargs)
        s.runner = no_listing
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT instance FROM cellular_sms_modems").fetchone()[0], "3")
        s = scanner(mm, wall_clock=lambda: initial + 20)
        self.assertEqual(s.poll(OTHER, Ingest()), [])
        # The receiving object belongs to the switch snapshot, even with no timestamp.
        mm.objects[path] = sms_object(text="late complete unknown card", timestamp="--")
        store.init()
        s = scanner(mm, wall_clock=lambda: initial + 3600)
        self.assert_unknown_archive_without_history_changes(mm, path, s, OTHER, before)

    def test_old_signature_ledger_cannot_claim_reused_path_as_new_card_after_failed_read(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="old object before modemmanager restart"))
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        mm.owner = ":1.99"
        mm.objects[path] = sms_object(text="card A object reusing old path",
                                      timestamp="2099-01-01T00:00:00+00:00")
        with patch.object(store, "claim_cellular_sms_object",
                          side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(s.poll(LINE, Ingest()), [])
        before = history_snapshot()
        mm.iccid = "card-b"
        s = scanner(mm)
        self.assert_unknown_archive_without_history_changes(mm, path, s, OTHER, before)
        self.assertEqual(store.list_messages("4", "+447700900123"), [])
