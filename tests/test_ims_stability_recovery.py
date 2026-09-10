"""Recovery changes must preserve the tunnel, calls, generations and private evidence."""
import ast
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import ims_recovery, stability
from engine import asterisk_supervisor as supervisor, stability_log
from tests.test_rekey_policy import FakeTunnel, SOURCE
from scripts import mdd_stability_report


class PeerReauthTests(unittest.TestCase):
    def test_authenticated_35_reauthenticates_once_without_repeated_proposals(self):
        tunnel = FakeTunnel(_rekey_failed=True, _rekey_last_notify=35)
        tunnel._rekey_tick()
        self.assertIsNotNone(tunnel._reauth_due_at)
        self.assertEqual(tunnel._child_rekey_mode, "pfs")
        tunnel._reauth_due_at -= 2
        with patch.dict(sys.modules, asterisk_supervisor=supervisor), patch.object(supervisor, "active_channels", return_value=0):
            tunnel._rekey_tick()
        self.assertEqual(tunnel.rekeys_started, 0)
        self.assertEqual(tunnel.teardowns, ["peer_no_additional_sas"])

    def test_call_grace_is_bounded_and_unknown_is_not_idle(self):
        for channels in (2, None):
            tunnel = FakeTunnel(_ike_rekey_failed=True, _ike_rekey_last_notify=35)
            tunnel._ike_rekey_tick()
            tunnel._reauth_due_at -= 2
            with patch.dict(sys.modules, asterisk_supervisor=supervisor), patch.object(supervisor, "active_channels", return_value=channels):
                tunnel._ike_rekey_tick()
                self.assertEqual(tunnel.teardowns, [])
                self.assertLessEqual(tunnel._reauth_due_at, tunnel._reauth_deadline)
                tunnel._reauth_due_at = tunnel._reauth_deadline = time.monotonic() - 1
                tunnel._ike_rekey_tick()
            self.assertEqual(tunnel.teardowns, ["peer_no_additional_sas"])

    def test_pcscf_change_only_queues_and_does_not_render_or_reload(self):
        node = next(n for n in ast.walk(ast.parse(SOURCE.read_text()))
                    if isinstance(n, ast.FunctionDef) and n.name == "swu_apply_pcscf")
        ns = {"stability_event": Mock(), "swu_log": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ns)
        with patch.dict(sys.modules, asterisk_supervisor=supervisor), \
                patch.object(supervisor, "request_restart", return_value="a" * 32) as request, \
                patch.object(subprocess, "run", side_effect=AssertionError("native reload")):
            ns[node.name]("2001:db8::1")
            ns[node.name]("2001:db8::1")
            ns[node.name]("2001:db8::2")
        self.assertEqual(request.call_count, 2)


class IMSRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ims_recovery._attempts.clear()
        self.inst = {"id": "7", "enabled": True}
        self.state = {"state": "REGISTERING", "reason_code": "reg_unanswered",
                      "detail": {"active_channels": 0}}
        self.runtime = {"running": True, "container_id": "old-generation"}
        self.ami = types.SimpleNamespace(reregister=AsyncMock(return_value=True),
                                        active_channel_count=AsyncMock(return_value=None))
        self.patches = [patch.object(ims_recovery.cfg, "get_instance", return_value=self.inst),
                        patch.object(ims_recovery.engine, "tunnel_installed", return_value=True),
                        patch.object(ims_recovery.engine, "request_ims_restart", return_value="a" * 32),
                        patch.object(ims_recovery, "_evidence", new=AsyncMock()),
                        patch.object(stability, "event")]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.addCleanup(ims_recovery._attempts.clear)

    async def hold(self):
        return await ims_recovery.hold("7", self.inst, self.state, self.runtime, self.ami)

    async def test_register_then_process_then_engine_with_full_transaction_windows(self):
        self.assertTrue(await self.hold())
        self.ami.reregister.assert_awaited_once()
        self.mocks[2].assert_not_called()
        self.assertTrue(await self.hold())
        attempt = ims_recovery._attempts["7"]
        self.assertGreater(attempt["deadline"] - time.monotonic(), 128)
        attempt["deadline"] = 0
        self.assertTrue(await self.hold())
        self.mocks[2].assert_called_once_with("7", "old-generation")
        self.assertTrue(await self.hold())
        attempt["deadline"] = 0
        self.assertFalse(await self.hold())
        self.assertFalse(await self.hold())
        self.assertEqual(self.mocks[2].call_count, 1)

    async def test_healthy_registration_cancels_escalation_and_logs_outcome(self):
        await self.hold()
        self.state["state"] = "OK"
        self.assertFalse(await self.hold())
        self.assertNotIn("7", ims_recovery._attempts)
        self.mocks[-1].assert_called_with("7", "ims_recovery_succeeded", inst=self.inst,
                                       phase="register", elapsed_ms=unittest.mock.ANY)

    async def test_calls_or_unknown_calls_never_escalate_even_after_timeout(self):
        await self.hold()
        ims_recovery._attempts["7"]["deadline"] = 0
        for channels in (1, None):
            self.state["detail"]["active_channels"] = channels
            self.assertTrue(await self.hold())
        self.mocks[2].assert_not_called()

    async def test_generation_change_and_user_stop_invalidate_old_recovery(self):
        await self.hold()
        ims_recovery._attempts["7"]["deadline"] = 0
        self.runtime["container_id"] = "replacement"
        await self.hold()
        self.mocks[2].assert_not_called()
        self.inst["enabled"] = False
        self.assertFalse(await self.hold())
        self.assertNotIn("7", ims_recovery._attempts)


class SupervisorTests(unittest.TestCase):
    def test_restart_guard_requires_connected_and_confirmed_idle(self):
        for state in ("CONNECTED", "DOWN", "CONNECTING", None):
            for calls in (None, 0, 1):
                self.assertEqual(supervisor.restart_allowed(state, calls), state == "CONNECTED" and calls == 0)

    def test_only_idle_process_gets_graceful_stop_no_tunnel_command(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            obj = supervisor.Supervisor(path, path)
            obj.child = Mock(pid=123)
            obj.child.poll.return_value = None
            supervisor.atomic_json(path / "swu_status.json", {"state": "CONNECTED"})
            supervisor.request_restart("pcscf_changed", path)
            with patch.object(supervisor, "active_channels", return_value=None), patch.object(supervisor, "cli") as cli:
                obj.tick()
                cli.assert_not_called()
            with patch.object(supervisor, "active_channels", return_value=0), patch.object(supervisor, "cli") as cli:
                obj.tick()
                cli.assert_called_once_with("core stop gracefully")
                obj.tick()
                self.assertEqual(cli.call_count, 1)
            obj.child.terminate.assert_not_called()

    def test_newer_ticket_is_not_acknowledged_during_render(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            first = supervisor.request_restart("pcscf_changed", path)
            obj = supervisor.Supervisor(path, path)
            def render(*args, **kwargs):
                supervisor.request_restart("pcscf_changed", path)
                return types.SimpleNamespace(returncode=0)
            with patch.object(supervisor.subprocess, "run", side_effect=render), \
                    patch.object(supervisor.subprocess, "Popen", return_value=Mock(pid=123)), \
                    patch.object(supervisor.threading, "Thread"):
                self.assertTrue(obj.start())
            self.assertEqual(obj.applied, first)
            self.assertNotEqual(supervisor.read_json(path / "asterisk.request.json")["request_id"], first)

    def test_native_crash_records_signal_and_stack_without_core_or_private_memory(self):
        if not shutil.which("gcc") or sys.platform != "linux":
            self.skipTest("Linux native compiler required")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            src = Path(__file__).resolve().parents[1] / "engine/native/crash_trace.c"
            library = path / "trace.so"
            subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-o", str(library), str(src), "-ldl"], check=True, capture_output=True)
            (path / "fixture.c").write_text('#include <signal.h>\nint main(void) { volatile char secret[] = "fixture-secret-never-log"; (void)secret; raise(SIGSEGV); }\n')
            subprocess.run(["gcc", "-rdynamic", "-o", str(path / "fixture"), str(path / "fixture.c")], check=True)
            trace, fd = supervisor.trace_file(path, "a" * 32)
            try:
                result = subprocess.run([str(path / "fixture")], cwd=path, timeout=5, pass_fds=(fd,),
                                        env={**os.environ, "LD_PRELOAD": str(library), "MDD_CRASH_FD": str(fd)},
                                        capture_output=True)
            finally:
                os.close(fd)
            self.assertEqual(result.returncode, -signal.SIGSEGV)
            text = trace.read_text()
            self.assertIn("MDD_CRASH_END", text)
            self.assertIn("frame=0", text)
            self.assertIn("function=main", text)
            self.assertNotIn("fixture-secret", text)
            self.assertFalse(list(path.glob("core*")))
            self.assertEqual(trace.stat().st_mode & 0o777, 0o600)

    def test_shareable_report_includes_private_safe_correlated_crash_timeline(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "instances/7/logs"
            path.mkdir(parents=True)
            stability_log.record(path, "asterisk", "native_crash", signal_code=11,
                                 asterisk_session="b" * 32, trace_available=True, password="do-not-export")
            report = mdd_stability_report.summarize(Path(root), 1)
            event = report["lines"][0]["recent_timeline"][0]
            self.assertEqual(event["signal_code"], 11)
            self.assertEqual(event["asterisk_session"], "b" * 32)
            self.assertNotIn("do-not-export", json.dumps(report))

    def test_register_timeout_is_distinct_from_an_actual_sip_response(self):
        with patch.object(stability, "event") as event:
            for received in ("0", "1"):
                stability.ami_event("7", {"Event": "MDDRegisterResponse", "StatusCode": "408",
                                          "ResponseReceived": received, "CSeq": "17", "ProcessId": "123",
                                          "ExpirationSeconds": "0", "Secret": "fixture-secret"})
                self.assertEqual(event.call_args.kwargs["response_received"], received == "1")
                self.assertEqual(event.call_args.kwargs["status_code"], 408)
                self.assertNotIn("Secret", event.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
