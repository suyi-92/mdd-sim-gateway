"""Protocol compatibility, private diagnostic evidence and bounded observation."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from control.app import config, stability
from engine import stability_log
from scripts import mdd_stability_report
from tests.test_rekey_policy import FakeTunnel, SOURCE


class RekeyCompatibilityTests(unittest.TestCase):
    def test_live_request_builder_omits_only_ke_and_dh_in_compatibility_mode(self):
        names = {"state_ue_rekey_child", "_begin_create_child_request", "_child_sa_list_with_dh",
                 "_child_sa_list_with_pfs", "create_CREATE_CHILD_SA_CHILD", "create_CREATE_CHILD_SA_CHILD_pfs"}
        picked = [node for node in ast.walk(ast.parse(SOURCE.read_text()))
                  if isinstance(node, ast.FunctionDef) and node.name in names]
        namespace = {"SA": 33, "KE": 34, "NINR": 40, "N": 41, "TSI": 44, "TSR": 45,
                     "NONE": 0, "ESP": 3, "CREATE_CHILD_SA": 36, "D_H": 4, "REKEY_SA": 16393,
                     "MODP_2048_bit": 14, "stability_event": Mock(), "swu_log": Mock()}
        exec(compile(ast.Module(body=picked, type_ignores=[]), str(SOURCE), "exec"), namespace)
        for mode in ("pfs", "inherited"):
            payload_types = []
            tunnel = types.SimpleNamespace(
                _child_rekey_mode=mode, message_id_request=9, sa_list_negotiated_child=[[[3, 4], [1, 12, 128], [3, 2], [5, 0]]],
                iana_diffie_hellman={14: "group14"}, ike_spi_initiator=b"init", ike_spi_responder=b"resp",
                spi_init_child=b"old!", dh_create_private_key_and_public_bytes=Mock(),
                encode_header=lambda *args: b"header", encode_payload_type_sa=lambda sa: b"proposal",
                encode_payload_type_ninr=lambda *args: b"fresh-nonce", encode_payload_type_n=lambda *args: b"rekey-old",
                encode_payload_type_ke=lambda: b"fresh-ke", encode_payload_type_tsi=lambda: b"tsi",
                encode_payload_type_tsr=lambda: b"tsr", set_ike_packet_length=lambda b: b,
                encode_payload_type_sk=lambda b: b, send_data=Mock())
            def payload(next_type, flags, body):
                payload_types.append(next_type)
                return body
            tunnel.encode_generic_payload_header = payload
            for name in names:
                setattr(tunnel, name, types.MethodType(namespace[name], tunnel))
            tunnel.state_ue_rekey_child()
            packet = tunnel.send_data.call_args.args[0]
            self.assertEqual(34 in payload_types, mode == "pfs")
            self.assertEqual(b"fresh-ke" in packet, mode == "pfs")
            self.assertIn(b"fresh-nonce", packet)
            self.assertIn(b"rekey-old", packet)
            self.assertEqual(tunnel._create_child_request_id, 10)
            self.assertEqual(tunnel._rekey_packet, packet)
            self.assertEqual(tunnel.dh_create_private_key_and_public_bytes.call_count, int(mode == "pfs"))
            self.assertEqual([t for t in tunnel.sa_list_create_child_sa_child[0] if t[0] != 4],
                             tunnel.sa_list_negotiated_child[0])

    def test_no_ke_response_is_rejected_before_using_stale_dh_or_installing_keys(self):
        tree = ast.parse(SOURCE.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "state_epdg_create_sa_response")
        namespace = {"SA": 33, "ESP": 3, "IKE": 1, "KE": 34, "NINR": 40, "N": 41,
                     "INVALID_SYNTAX": 7, "stability_event": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
        tunnel = types.SimpleNamespace(
            _rekey_outstanding=True, _rekey_request_mode="inherited",
            decoded_payload=[[0, [[33, [1, 3, b"test"]], [34, [14, b"peer-key"]]]]],
            dh_calculate_shared_key=Mock(side_effect=AssertionError("stale DH used")))
        namespace[node.name](tunnel)
        self.assertTrue(tunnel._rekey_failed)
        self.assertEqual(tunnel._rekey_last_notify, 7)
        tunnel.dh_calculate_shared_key.assert_not_called()

    def fire(self):
        tunnel = FakeTunnel()
        tunnel._child_sa_time = time.monotonic() - tunnel.child_rekey_period - 1
        tunnel._rekey_tick()
        return tunnel

    def reject(self, tunnel, code):
        tunnel._rekey_outstanding = False
        tunnel._rekey_failed = True
        tunnel._rekey_last_notify = code
        tunnel._rekey_tick()

    def test_authenticated_no_proposal_gets_one_new_no_ke_request(self):
        tunnel = self.fire()
        original_mid = tunnel.message_id_request
        self.reject(tunnel, 14)
        self.assertEqual(tunnel._child_rekey_mode, "inherited")
        self.assertTrue(tunnel._rekey_compat_attempted)
        self.assertEqual(tunnel.message_id_request, original_mid)
        self.assertEqual(tunnel.teardowns, [])
        tunnel._rekey_retry_at -= 2
        tunnel._rekey_tick()
        self.assertEqual(tunnel.sent, [b"rekey-request", b"no-ke-request"])
        self.assertEqual(tunnel.message_id_request, original_mid + 1)
        tunnel._rekey_sent_at -= tunnel.rekey_response_timeout
        tunnel._rekey_tick()
        self.assertEqual(tunnel.sent[-2:], [b"no-ke-request", b"no-ke-request"])
        self.assertEqual(tunnel.message_id_request, original_mid + 1)

    def test_rejected_compatibility_keeps_old_sa_without_an_unbounded_mode_loop(self):
        tunnel = self.fire()
        self.reject(tunnel, 14)
        tunnel._rekey_retry_at -= 2
        tunnel._rekey_tick()
        self.reject(tunnel, 14)
        self.assertEqual(tunnel._child_rekey_mode, "pfs")
        self.assertEqual(tunnel.teardowns, [])
        tunnel._rekey_retry_at -= tunnel.rekey_retry_interval + 1
        tunnel._rekey_tick()
        self.reject(tunnel, 14)
        self.assertEqual(tunnel._child_rekey_mode, "pfs")

    def test_temporary_failure_never_changes_the_security_mode(self):
        tunnel = self.fire()
        self.reject(tunnel, 43)
        self.assertEqual(tunnel._child_rekey_mode, "pfs")
        self.assertFalse(tunnel._rekey_compat_attempted)
        self.assertEqual(tunnel.teardowns, [])

    def test_silence_never_triggers_a_different_request_on_the_same_ike_sa(self):
        tunnel = self.fire()
        for _ in range(3):
            tunnel._rekey_sent_at -= tunnel.rekey_response_timeout
            tunnel._rekey_tick()
        self.assertEqual(tunnel.sent, [b"rekey-request"] * 3)
        self.assertFalse(tunnel._rekey_compat_attempted)
        self.assertEqual(tunnel.teardowns, ["rekey_timeout"])

    def test_no_ke_proposal_preserves_negotiated_cipher_integrity_and_esn(self):
        tree = ast.parse(SOURCE.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_child_sa_list_with_dh")
        namespace = {"D_H": 4}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
        original = [[[3, 4], [1, 12, 128], [3, 2], [5, 0], [4, 14]]]
        tunnel = types.SimpleNamespace(sa_list_negotiated_child=copy.deepcopy(original))
        result = namespace[node.name](tunnel, None)
        self.assertEqual(result, [[[3, 4], [1, 12, 128], [3, 2], [5, 0]]])
        self.assertEqual(tunnel.sa_list_negotiated_child, original)


class StabilityLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_closed_schema_drops_secrets_raw_messages_and_invalid_values(self):
        self.assertTrue(stability_log.record(self.root, "ike", "child_rekey_rejected",
            mode="pfs", notify_code=14, message_id=3, iccid="fixture-card",
            password="fixture-password", nonce="fixture-nonce", peer="private.invalid",
            message="fixture-message", protocol="private.invalid", status_code=10**15))
        raw = (self.root / "stability-ike.jsonl").read_text()
        record = json.loads(raw)
        self.assertEqual(record["notify_code"], 14)
        for text in ("fixture", "private.invalid", "password", "nonce", '"peer"', '"protocol"', '"status_code"'):
            self.assertNotIn(text, raw)
        self.assertEqual((self.root / "stability-ike.jsonl").stat().st_mode & 0o777, 0o600)

    def test_rotation_bounds_size_and_count(self):
        with patch.object(stability_log, "MAX_BYTES", 450), patch.object(stability_log, "ARCHIVES", 3):
            for number in range(40):
                self.assertTrue(stability_log.record(self.root, "ike", "child_rekey_retry", attempt=number))
        files = list(self.root.glob("stability-ike*.jsonl"))
        self.assertLessEqual(len(files), 4)
        self.assertTrue(all(p.stat().st_size <= 450 for p in files))
        self.assertEqual(json.loads((self.root / "stability-ike.jsonl").read_text().splitlines()[-1])["attempt"], 39)

    def test_expired_archives_are_removed_but_unrelated_files_are_kept(self):
        old = self.root / "stability-control.1.jsonl"
        old.write_text("old");old.chmod(0o600)
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        other = self.root / "keep.jsonl";other.write_text("keep")
        self.assertTrue(stability_log.record(self.root, "control", "health_sample", state="OK"))
        self.assertFalse(old.exists())
        self.assertEqual(other.read_text(), "keep")

    def test_symlink_is_rejected_without_touching_its_target(self):
        target = self.root / "private";target.write_text("keep")
        (self.root / "stability-ike.jsonl").symlink_to(target)
        self.assertFalse(stability_log.record(self.root, "ike", "session_started"))
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(stability_log.record(self.root / "missing", "ike", "session_started"))
        self.assertFalse((self.root / "missing").exists())

    def test_transport_event_and_health_samples_remain_private_and_bounded(self):
        logs = self.root / "instances/1/logs";logs.mkdir(parents=True)
        with patch.object(config, "DATA_DIR", str(self.root)), patch.object(stability, "_samples", {}):
            stability.ami_event("1", {"Event": "MDDTransportState", "State": "disconnected",
                                      "Protocol": "TCP", "StatusCode": "70016", "DirectionCode": "1",
                                      "CallerIDNum": "fixture-number", "Secret": "fixture-secret"})
            for stamp in (1000, 1010, 1310):
                with patch.object(stability.time, "monotonic", return_value=stamp):
                    stability.sample({"id": "1", "mcc": "001", "mnc": "01", "proxy_country": "gb"},
                                     {"state": "OK", "reason_code": "ok", "detail": {"registration": "Registered"}},
                                     {"running": True})
        records = [json.loads(line) for line in (logs / "stability-control.jsonl").read_text().splitlines()]
        self.assertEqual([r["event"] for r in records], ["ims_transport", "health_changed", "health_sample"])
        self.assertEqual(records[0]["status_code"], 70016)
        self.assertNotIn("fixture", json.dumps(records))

    def test_client_diagnostics_reject_extra_fields_before_any_write(self):
        item = {"scope": "devices", "outcome": "timeout", "status_code": 0,
                "elapsed_ms": 15000, "client_epoch": 1000, "sequence": 1}
        with patch.object(config, "DATA_DIR", str(self.root)):
            with self.assertRaises(ValueError):
                stability.client_events({"events": [{**item, "password": "fixture-secret"}]})
            self.assertFalse((self.root / "logs").exists())
            self.assertEqual(stability.client_events({"events": [item]}), 1)
        record = json.loads((self.root / "logs/stability-control.jsonl").read_text())
        self.assertEqual(record["scope"], "devices")


class TransportPatchTests(unittest.TestCase):
    def test_patch_is_idempotent_and_emits_no_addresses_or_identities(self):
        source = '#include "asterisk/module.h"\nvoid callback(void) {\n\tstruct ao2_container *transports;\n\n\t/* We only care about reliable transports */\n}\n'
        script = Path(__file__).resolve().parents[1] / "engine/patches/asterisk/transport_stability_events.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "res/res_pjsip/pjsip_transport_management.c"
            path.parent.mkdir(parents=True);path.write_text(source)
            env = {**os.environ, "AST_SRC": directory}
            subprocess.run([sys.executable, str(script)], env=env, check=True, capture_output=True)
            first = path.read_text()
            subprocess.run([sys.executable, str(script)], env=env, check=True, capture_output=True)
            self.assertEqual(first, path.read_text())
            self.assertIn('"MDDTransportState"', first)
            self.assertIn('info->status', first)
            for secret in ('remote_name', 'local_name', 'registration_name', 'transport->obj_name'):
                self.assertNotIn(secret, first)
            path.write_text('unexpected source')
            result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(), 'unexpected source')


class StabilityReportTests(unittest.TestCase):
    def test_summary_selects_safe_fields_and_ignores_old_unsafe_or_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "instances/1/logs"
            logs.mkdir(parents=True)
            for _ in range(2):
                stability_log.record(logs, "ike", "child_rekey_rejected", notify_code=14)
            stability_log.record(logs, "control", "ims_transport",
                                 transport_state="disconnected", status_code=70016)
            secret = logs / "private.log"
            secret.write_text("fixture-secret")
            (logs / "stability-ike.1.jsonl").symlink_to(secret)
            old = logs / "stability-ike.2.jsonl"
            old.write_text(json.dumps({"schema": 1, "ts": time.time()-90000,
                                       "event": "session_started"}) + "\n")
            old.chmod(0o600)
            unknown = logs / "stability-unknown.jsonl"
            unknown.write_text("fixture-secret")
            before = {p.name: p.lstat().st_mtime_ns for p in logs.iterdir()}
            result = mdd_stability_report.summarize(root, 24)
            self.assertEqual(result["lines"][0]["records"], 3)
            self.assertEqual(result["lines"][0]["child_rekey_rejections"], {"14": 2})
            self.assertEqual(result["lines"][0]["transport_disconnect_status"], {"70016": 1})
            self.assertNotIn("fixture", json.dumps(result))
            self.assertEqual(before, {p.name: p.lstat().st_mtime_ns for p in logs.iterdir()})
            for hours in (0, 169):
                with self.assertRaises(ValueError):
                    mdd_stability_report.summarize(root, hours)

    def test_support_bundle_includes_exact_stability_files(self):
        from io import BytesIO
        import zipfile
        from control.app import operations
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(config, "DATA_DIR", directory), \
                patch.object(config, "get_settings", return_value={}):
            logs = Path(directory) / "instances/1/logs"
            logs.mkdir(parents=True)
            stability_log.record(logs, "control", "health_sample", state="OK")
            (logs / "stability-secret.jsonl").write_text("fixture-secret")
            with zipfile.ZipFile(BytesIO(operations.support_bundle({}))) as bundle:
                self.assertIn("logs/1-stability-control.jsonl", bundle.namelist())
                self.assertFalse(any("secret" in name for name in bundle.namelist()))


class ClientEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_limits_and_session_csrf_protection(self):
        from control.app import main, auth
        from starlette.requests import Request
        from starlette.responses import Response
        from fastapi import HTTPException
        from unittest.mock import AsyncMock

        def request(body, headers=()):
            return Request({"type": "http", "method": "POST", "scheme": "https",
                            "path": "/api/diagnostics/client-events", "query_string": b"",
                            "headers": list(headers)},
                           AsyncMock(return_value={"type": "http.request", "body": body}))

        call_next = AsyncMock(return_value=Response())
        with patch.object(auth, "session", return_value=None):
            response = await main.require_admin_session(request(b"{}"), call_next)
            self.assertEqual(response.status_code, 401)
        with patch.object(auth, "session", return_value={"csrf": "fixture-csrf"}):
            response = await main.require_admin_session(request(b"{}"), call_next)
            self.assertEqual(response.status_code, 403)
            response = await main.require_admin_session(
                request(b"{}", [(b"x-mdd-csrf-token", b"fixture-csrf")]), call_next)
            self.assertEqual(response.status_code, 200)
        item = {"scope": "devices", "outcome": "timeout", "status_code": 0,
                "elapsed_ms": 15000, "client_epoch": 1000, "sequence": 1}
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", directory):
            result = await main.api_client_diagnostics(request(json.dumps({"events": [item]}).encode()))
            self.assertEqual(result["accepted"], 1)
            for body, status in ((b"{", 400), (b"[]", 400), (b" " * 8193, 413)):
                with self.assertRaises(HTTPException) as caught:
                    await main.api_client_diagnostics(request(body))
                self.assertEqual(caught.exception.status_code, status)


if __name__ == "__main__":
    unittest.main()
