from control.app import store
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import cellular_sms, store

MODEM = "/org/freedesktop/ModemManager1/Modem/0"
SIM = "/org/freedesktop/ModemManager1/SIM/0"
LINE = [{"id": "3", "iccid": "card-a"}]


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def sms_object(number="+447700900123", text="hello", *, pdu_type="deliver", state="received",
               timestamp="2026-09-12T09:00:00+08:00", storage="me", data="--"):
    return {"content": {"number": number, "text": text, "data": data},
            "properties": {"pdu-type": pdu_type, "state": state, "timestamp": timestamp,
                           "storage": storage}}


def wap_push_sms() -> dict:
    """A carrier MMS notification as ModemManager reports it: no text, binary WSP payload."""
    payload = b"\x23\x06\x24" + b"application/vnd.wap.mms-message" + b"\x00"
    return sms_object("+447700900999", "--", data=" ".join(f"{b:02X}" for b in payload))


class FakeModemManager:
    """Just enough of mmcli/busctl: one modem, one SIM, a mutable set of SMS objects."""

    def __init__(self, iccid="card-a", objects=None, *, cpms=None, delete_ok=True):
        self.iccid = iccid
        self.objects = dict(objects or {})
        self.cpms = cpms
        self.delete_ok = delete_ok
        self.calls = []
        self.next_id = 100
        self.send_hook = None

    def path(self, n):
        return f"/org/freedesktop/ModemManager1/SMS/{n}"

    def add(self, n, detail):
        self.objects[self.path(n)] = detail
        return self.path(n)

    def deletes(self):
        return [c[3].split("=", 1)[1] for c in self.calls
                if len(c) == 4 and c[3].startswith("--messaging-delete-sms=")]

    def __call__(self, args, **kwargs):
        self.calls.append(tuple(args))
        assert isinstance(args, list) and "shell" not in kwargs
        if args == ["mmcli", "-L"]:
            return Result(MODEM)
        if args == ["mmcli", "-m", MODEM, "--output-json"]:
            return Result(json.dumps({"modem": {"generic": {"sim": SIM}}}))
        if args == ["mmcli", "-i", SIM, "--output-json"]:
            return Result(json.dumps({"sim": {"properties": {"iccid": self.iccid}}}))
        if args == ["mmcli", "-m", MODEM, "--messaging-list-sms", "--output-json"]:
            return Result(json.dumps({"modem.messaging.sms": sorted(self.objects)}))
        if args == ["mmcli", "-m", MODEM, "--messaging-status", "--output-json"]:
            return Result("{}")
        if args == ["mmcli", "-m", MODEM, "--command=AT+CPMS?"]:
            if self.cpms is None:
                return Result(returncode=1, stderr="command not allowed")
            used, total = self.cpms
            return Result(f"response: '+CPMS: \"ME\",{used},{total},\"ME\",{used},{total}'")
        if len(args) == 4 and args[:3] == ["mmcli", "-m", MODEM] and \
                args[3].startswith("--messaging-delete-sms="):
            path = args[3].split("=", 1)[1]
            if not self.delete_ok or path not in self.objects:
                return Result(returncode=1)
            del self.objects[path]
            if self.cpms:
                self.cpms = (self.cpms[0] - 1, self.cpms[1])
            return Result()
        if args[:3] == ["busctl", "--system", "call"] and args[6] == "Create":
            self.next_id += 1
            path = self.add(self.next_id, sms_object(args[11], args[14], pdu_type="submit",
                                                     state="stored", timestamp="--"))
            return Result(f'o "{path}"\n')
        if len(args) == 5 and args[1] == "-s" and args[3] == "--send":
            if self.send_hook:
                return self.send_hook(args, kwargs)
            self.objects[args[2]]["properties"]["state"] = "sent"
            return Result("{}")
        if len(args) == 4 and args[1] == "-s" and args[3] == "--output-json":
            if args[2] in self.objects:
                return Result(json.dumps({"sms": self.objects[args[2]]}))
            return Result(returncode=1)
        return Result(returncode=1)


class MemoryTracker:
    def __init__(self, *, begin_error=None, bind=True, own=False):
        self.begin_error = begin_error
        self.bind = bind
        self.own = own
        self.calls = []

    def begin_local_modem_sms(self, instance, recipient, body):
        self.calls.append(("begin", instance, recipient, body))
        if self.begin_error:
            raise self.begin_error
        return 9

    def bind_local_modem_sms(self, message_id, modem_path, sms_path):
        self.calls.append(("bind", message_id, modem_path, sms_path))
        return self.bind

    def owns_local_modem_sms(self, instance, modem_path, sms_path, peer, body):
        self.calls.append(("owns", instance, sms_path, peer, body))
        return self.own


def scanner(mm, **kwargs):
    kwargs.setdefault("epoch_getter", lambda: "epoch-1")
    kwargs.setdefault("local_sms_tracker", store)
    return cellular_sms.Scanner(mm, **kwargs)


class Ingest:
    def __init__(self, error=None):
        self.records = []
        self.error = error

    def __call__(self, record):
        if self.error:
            raise self.error
        self.records.append(record)
        if record.get("data"):
            return store.add_binary_sms(record["instance"], record["peer"], transport="cellular",
                                        ts=record["ts"], body_hex=record["data"].hex())
        return store.add_imported_message(record["fingerprint"], record["instance"],
            record["direction"], record["peer"], record["body"], record["ts"])


class DurableStoreCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        p = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(root / "history.sqlite"),
                           PREVIOUS_DB_PATH=str(root / "old.sqlite"))
        p.start(); self.addCleanup(p.stop); store.init()

class DiscoverTests(DurableStoreCase):
    def test_received_sms_is_mapped_to_instance_by_case_insensitive_iccid(self):
        mm = FakeModemManager("CARD-A", cpms=(1, 20))
        mm.add(7, sms_object(text="hello"))
        rows = cellular_sms.discover(LINE, runner=mm)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], "3")
        self.assertEqual(rows[0]["peer"], "+447700900123")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["ts"], 1789174800)
        self.assertEqual(mm.deletes(), [], "a diagnostic read never deletes")

    def test_unknown_sim_is_ignored(self):
        mm = FakeModemManager("card-z")
        mm.add(7, sms_object())
        self.assertEqual(cellular_sms.discover(LINE, runner=mm), [])

    def test_scanner_caches_topology_and_details_but_keeps_listing_live(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        now = [10.0]
        s = scanner(mm, clock=lambda: now[0])
        first = s.discover(LINE)
        now[0] += 5
        second = s.discover(LINE)
        self.assertEqual(first, second)
        self.assertEqual(mm.calls.count(("mmcli", "-L")), 1)
        self.assertEqual(mm.calls.count(("mmcli", "-s", path, "--output-json")), 1)
        self.assertEqual(mm.calls.count(
            ("mmcli", "-m", MODEM, "--messaging-list-sms", "--output-json")), 2)

    def test_topology_is_refreshed_after_ttl(self):
        mm = FakeModemManager()
        now = [10.0]
        s = scanner(mm, topology_ttl=60, clock=lambda: now[0])
        s.discover(LINE)
        now[0] = 71.0
        s.discover(LINE)
        self.assertEqual(mm.calls.count(("mmcli", "-L")), 2)

    def test_incomplete_multipart_sms_is_never_imported_as_its_placeholder(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="--", state="receiving"))
        now = [10.0]
        s = scanner(mm, clock=lambda: now[0])
        self.assertEqual(s.discover(LINE), [])
        # A ModemManager build that reports no state must not import the placeholder either.
        mm.objects[path]["properties"].pop("state")
        now[0] += 1
        self.assertEqual(s.discover(LINE), [])
        mm.objects[path] = sms_object(text="part one and part two")
        now[0] += 100
        self.assertEqual([r["body"] for r in s.discover(LINE)], ["part one and part two"])

    def test_sms_binary_payload_decoding_tolerates_mmcli_renderings(self):
        self.assertEqual(cellular_sms._sms_data("23 06 24"), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data("230624"), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data([35, 6, 36]), b"\x23\x06\x24")
        self.assertEqual(cellular_sms._sms_data("--"), b"")
        self.assertEqual(cellular_sms._sms_data(None), b"")
        self.assertEqual(cellular_sms._sms_data("23 06 2"), b"")


class StoragePolicyTests(DurableStoreCase):
    def test_policy_resolution_prefers_settings_then_environment(self):
        env = {cellular_sms.STORAGE_POLICY_ENV: "when-full"}
        self.assertEqual(cellular_sms.storage_policy({}, env), "when_full")
        self.assertEqual(cellular_sms.storage_policy({"cellular_sms_storage": "keep"}, env),
                         "keep")
        self.assertEqual(cellular_sms.storage_policy({"cellular_sms_storage": "bogus"}, {}),
                         "delete")
        self.assertEqual(cellular_sms.storage_limit({cellular_sms.STORAGE_LIMIT_ENV: "2"}),
                         cellular_sms._DEFAULT_STORAGE_LIMIT)
        self.assertEqual(cellular_sms.storage_limit({cellular_sms.STORAGE_LIMIT_ENV: "50"}), 50)

    def test_delete_policy_removes_object_only_after_it_is_stored(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        failing = Ingest(error=RuntimeError("database is locked"))
        s = scanner(mm)
        self.assertEqual(s.poll(LINE, failing, policy="delete"), [])
        self.assertEqual(mm.deletes(), [], "an object whose message is not stored stays")

        ingest = Ingest()
        stored = s.poll(LINE, ingest, policy="delete")
        self.assertEqual([r["body"] for r in stored], ["hello"])
        self.assertEqual(mm.deletes(), [path])
        self.assertEqual(mm.objects, {})

    def test_duplicate_is_still_deleted(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object())
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        self.assertEqual(s.poll(LINE, lambda record: None, policy="delete"), [])
        self.assertEqual(mm.deletes(), [path])

    def test_keep_policy_never_deletes_and_ingests_once(self):
        mm = FakeModemManager()
        mm.add(7, sms_object())
        ingest = Ingest()
        now = [0.0]
        s = scanner(mm, clock=lambda: now[0])
        for _ in range(3):
            s.poll(LINE, ingest, policy="keep")
            now[0] += 100
        self.assertEqual(len(ingest.records), 1)
        self.assertEqual(mm.deletes(), [])

    def test_delete_verifies_the_path_still_holds_the_stored_message(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="first"))
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="keep")
        # ModemManager restarted under the same D-Bus name and reused the number.
        mm.objects[path] = sms_object(text="second, not imported yet")
        record = s.discover(LINE)[0]
        self.assertFalse(s.delete_preserved(record))
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)

    def test_when_full_deletes_oldest_imported_objects_from_modem_capacity(self):
        mm = FakeModemManager(cpms=(21, 23))
        paths = [mm.add(n, sms_object(text=f"m{n}", timestamp=f"2026-09-0{n}T09:00:00+08:00"))
                 for n in range(1, 6)]
        s = scanner(mm)
        s.poll(LINE, Ingest(), policy="when_full")
        # 21 used of 23 while 3 slots stay free: one deletion brings it to 20.
        self.assertEqual(mm.deletes(), [paths[0]])

    def test_when_full_without_command_channel_uses_the_configured_limit(self):
        mm = FakeModemManager(cpms=None)
        paths = [mm.add(n, sms_object(text=f"m{n}", timestamp=f"2026-09-0{n}T09:00:00+08:00"))
                 for n in range(1, 8)]
        s = scanner(mm, environ={cellular_sms.STORAGE_LIMIT_ENV: "8"})
        s.poll(LINE, Ingest(), policy="when_full")
        self.assertEqual(mm.deletes(), paths[:2])

    def test_undeletable_object_stops_retrying_after_its_attempt_budget(self):
        mm = FakeModemManager(delete_ok=False)
        mm.add(7, sms_object())
        now = [0.0]
        s = scanner(mm, clock=lambda: now[0])
        for _ in range(6):
            s.poll(LINE, Ingest(), policy="delete")
            now[0] += 5
        self.assertEqual(len(mm.deletes()), 3)  # attempts at 0, 5, 15; bounded exponential backoff


class LegacyFingerprintTests(DurableStoreCase):
    def test_inbound_record_carries_the_1_9_marker_fingerprint(self):
        import hashlib
        mm = FakeModemManager("CARD-A")
        path = mm.add(7, sms_object(text="hello", timestamp="2026-09-12T09:00:00+08:00"))
        record = scanner(mm).discover(LINE)[0]
        expected = hashlib.sha256("\0".join(
            ("card-a", path, "in", "+447700900123", "hello", "2026-09-12T09:00:00+08:00")
        ).encode()).hexdigest()
        self.assertEqual(record["legacy_fingerprint"], expected)


class BinaryObjectTests(DurableStoreCase):
    def test_binary_payload_is_handed_to_ingest_and_then_removed(self):
        mm = FakeModemManager()
        path = mm.add(7, wap_push_sms())
        ingest = Ingest()
        stored = scanner(mm).poll(LINE, ingest)
        self.assertEqual(len(stored), 1)
        self.assertEqual(ingest.records[0]["body"], "")
        self.assertTrue(ingest.records[0]["data"].startswith(b"\x23\x06\x24"))
        self.assertEqual(mm.deletes(), [path])

    def test_object_with_neither_text_nor_payload_is_left_alone(self):
        mm = FakeModemManager()
        mm.add(7, sms_object(text="--", data="--"))
        ingest = Ingest()
        self.assertEqual(scanner(mm).poll(LINE, ingest), [])
        self.assertEqual(ingest.records, [])
        self.assertEqual(mm.deletes(), [])
