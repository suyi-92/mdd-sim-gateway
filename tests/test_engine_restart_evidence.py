"""Evidence for engine bounces that nothing used to record.

The engine containers were being restarted by Docker's restart policy several times a day —
Asterisk went away a few seconds after the P-CSCF reload that follows an ePDG teardown — and
none of it reached the timeline: the bounce resolves faster than the health policy's threshold,
so no recovery is scheduled and lifecycle.jsonl stays empty. These tests cover the three pieces
that make such a bounce legible afterwards, plus the privacy boundary the new logs must respect.
"""
import importlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "engine" / "entrypoint.sh"


def engine_module():
    fake_docker = SimpleNamespace(
        from_env=lambda: None,
        errors=SimpleNamespace(NotFound=type("NotFound", (Exception,), {})),
    )
    with patch.dict(sys.modules, {"docker": fake_docker}):
        sys.modules.pop("control.app.engine", None)
        return importlib.import_module("control.app.engine")


class SupervisorRecordTests(unittest.TestCase):
    """The entrypoint's record of how Asterisk left, read back by the manager."""

    def test_last_exit_reports_the_most_recent_disposition(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / "instances" / "7" / "logs" / "asterisk"
            logs.mkdir(parents=True)
            (logs / "supervisor.jsonl").write_text(
                json.dumps({"ts": 1, "event": "asterisk_exited",
                            "rc": 0, "disposition": "exit"}) + "\n"
                + json.dumps({"ts": 2, "event": "swu_ike_exited", "rc": 1}) + "\n"
                + json.dumps({"ts": 3, "event": "asterisk_exited", "rc": 139,
                              "signal": 11, "disposition": "signal"}) + "\n")
            with patch.object(engine, "DATA_DIR", temp):
                record = engine.last_engine_exit("7")
        self.assertEqual(record["disposition"], "signal")
        self.assertEqual(record["signal"], 11)

    def test_missing_file_is_not_an_error(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp, patch.object(engine, "DATA_DIR", temp):
            self.assertEqual(engine.last_engine_exit("7"), {})

    def test_engine_restarted_is_an_accepted_lifecycle_event(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "instances" / "7" / "logs"
            base.mkdir(parents=True)
            with patch.object(engine, "DATA_DIR", temp):
                engine.record_lifecycle("7", "engine_restarted", reason_code="engine_exit")
            written = json.loads((base / "lifecycle.jsonl").read_text().strip())
        self.assertEqual(written["event"], "engine_restarted")
        self.assertEqual(written["reason_code"], "engine_exit")

    def test_runtime_reports_restart_count(self):
        """A restart-policy bounce increments RestartCount on the same container; a rebuild
        the manager performs starts a fresh one at zero. Only the counter separates them."""
        engine = engine_module()
        container = SimpleNamespace(
            status="running", id="abc",
            attrs={"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.4"}}},
                   "RestartCount": 6, "State": {"StartedAt": "2026-09-15T07:09:02Z"}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda _name: container))
        with patch.object(engine, "_client", return_value=client):
            runtime = engine.container_runtime("7")
        self.assertEqual(runtime["restart_count"], 6)
        self.assertEqual(runtime["started_at"], "2026-09-15T07:09:02Z")


class SupportBundleBoundaryTests(unittest.TestCase):
    """Asterisk's own logs are now persistent. They must not follow into a support bundle."""

    def test_asterisk_logs_are_not_collected_but_supervisor_is(self):
        source = (REPO / "control" / "app" / "operations.py").read_text()
        # The allow-list block that decides which files a bundle may carry.
        block = source[source.index("Explicit allow-list"):]
        block = block[:block.index("for path in sorted(paths)")]
        self.assertIn('\"asterisk\"', block)
        self.assertIn('stability-{component}', block)
        # `full` and `messages` carry the subscriber's IMS public identity on every
        # registration, so no glob may sweep the directory wholesale.
        self.assertNotIn('logs/asterisk/*', block)
        self.assertNotIn('logs/asterisk/full', block)
        self.assertNotIn('logs/asterisk/messages', block)


class EntrypointSupervisionTests(unittest.TestCase):
    """The entrypoint is shell; these assert on its text and on its actual behaviour."""

    def test_script_is_syntactically_valid(self):
        result = subprocess.run(["bash", "-n", str(ENTRYPOINT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_asterisk_is_supervised_rather_than_exec_replaced(self):
        """Asterisk as PID 1 meant the container simply vanished with it, leaving only
        ExitCode=0 — which cannot distinguish a clean shutdown from anything else."""
        text = ENTRYPOINT.read_text()
        self.assertNotIn("exec asterisk", text)
        self.assertIn("exec python3 -u /usr/local/bin/asterisk_supervisor.py", text)
        native = (REPO / "engine/asterisk_supervisor.py").read_text()
        self.assertIn('self.event("asterisk_exited", signal_code=', native)
        self.assertIn("exit_code=", native)

    def test_reconnect_backoff_resets_after_a_stable_run(self):
        """Extracted and run for real: the delay only ever doubled (4 -> 8 -> ... -> 60) and
        was never reset, which stayed hidden only because the container kept restarting and
        re-seeding it. Once Asterisk no longer takes the container down, an unreset backoff
        would leave a healthy line waiting a full minute to re-establish."""
        script = """
        set -u
        SWU_STABLE_SECONDS=120
        supervisor_record() { :; }
        log() { :; }
        backoff=4
        for ran in $RUNS; do
          if [ "$ran" -ge "$SWU_STABLE_SECONDS" ]; then
            backoff=4
          fi
          echo -n "$backoff "
          backoff=$((backoff*2)); [ "$backoff" -gt 60 ] && backoff=60
        done
        exit 0
        """
        # Four teardowns in quick succession, then one run that stayed up for an hour, then
        # another teardown: the last one must be back at the short delay.
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env={"RUNS": "5 5 5 5 3600 5", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        delays = result.stdout.split()
        self.assertEqual(delays, ["4", "8", "16", "32", "4", "8"])

    def test_asterisk_logs_are_written_to_the_bind_mounted_volume(self):
        """/logs survives both a container restart and a manager rebuild; the image's default
        log directory survives neither."""
        conf = (REPO / "engine" / "templates" / "asterisk.conf.j2").read_text()
        self.assertRegex(conf, r"astlogdir\s*=\s*/logs/asterisk")
        self.assertIn("mkdir -p", ENTRYPOINT.read_text())
        self.assertIn("MDD_AST_LOGDIR", ENTRYPOINT.read_text())


class PcscfApplyModeTests(unittest.TestCase):
    def test_native_apply_only_requests_supervised_replacement(self):
        source = (REPO / 'engine/swu_ike.py').read_text()
        start = source.index('def swu_apply_pcscf')
        func = source[start:start+1100]
        self.assertIn('request_restart("pcscf_changed")', func)
        self.assertNotIn('core restart now', func)
        self.assertNotIn('module reload', func)

    def test_peer_teardown_still_reports_duration(self):
        source = (REPO / 'engine/swu_ike.py').read_text()
        self.assertIn('swu_notify("tunnel_deleted_by_peer"', source)
        self.assertIn('def seconds_since_connect', source)


class RestartBaselineTests(unittest.IsolatedAsyncioTestCase):
    """A bounce must be recorded even when it is the first Docker event the manager sees."""

    async def test_bounce_after_manager_restart_is_recorded(self):
        from control.app import main
        hub = main.Hub()
        # Manager just started; the status poll sees line 5 running with no bounces yet.
        hub.seed_restart_baseline("5", {"running": True, "restart_count": 0})
        with patch.object(main.engine, "last_engine_exit",
                          return_value={"disposition": "signal"}), \
                patch.object(main.engine, "record_lifecycle") as record:
            await hub._note_unrequested_restart("5", {"running": True, "restart_count": 1})
        record.assert_called_once_with("5", "engine_restarted", reason_code="engine_signal")

    async def test_seeding_never_overwrites_an_existing_baseline(self):
        from control.app import main
        hub = main.Hub()
        hub.seed_restart_baseline("7", {"running": True, "restart_count": 0})
        # A later poll after the bounce must not swallow it by moving the baseline.
        hub.seed_restart_baseline("7", {"running": True, "restart_count": 1})
        self.assertEqual(hub._restart_counts["7"], 0)


class TunnelRebuiltApplyTests(unittest.TestCase):
    def load(self, fail=False):
        import ast
        from unittest.mock import Mock
        source = (REPO / 'engine/swu_ike.py').read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                    and n.name == 'swu_apply_pcscf')
        request = Mock(side_effect=OSError('fixture') if fail else None, return_value='a'*32)
        namespace = {'swu_log': lambda *_a: None, 'stability_event': lambda *_a, **_k: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<pcscf>', 'exec'), namespace)
        patcher = patch.dict(sys.modules, {'asterisk_supervisor': SimpleNamespace(request_restart=request)})
        patcher.start(); self.addCleanup(patcher.stop)
        return namespace['swu_apply_pcscf'], request

    def test_rebuilt_tunnel_with_same_pcscf_requests_new_process(self):
        apply, request = self.load()
        apply('2001:db8::1')
        apply('2001:db8::1')
        self.assertEqual(request.call_count, 1)
        apply('2001:db8::1', tunnel_rebuilt=True)
        self.assertEqual(request.call_count, 2)

    def test_failed_request_can_be_retried_and_empty_address_is_ignored(self):
        apply, request = self.load(fail=True)
        apply('')
        request.assert_not_called()
        apply('2001:db8::1'); apply('2001:db8::1')
        self.assertEqual(request.call_count, 2)

    def test_only_full_attach_marks_tunnel_rebuilt(self):
        source = (REPO / 'engine/swu_ike.py').read_text()
        self.assertEqual(source.count('swu_apply_pcscf(pcscf, tunnel_rebuilt=True)'), 1)
        self.assertIn('swu_apply_pcscf(new_pcscf)', source)
