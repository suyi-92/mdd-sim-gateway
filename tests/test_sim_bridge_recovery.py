"""Bound runtime SIM transport recovery without replaying commands or resetting hardware."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from host import vpcd_modem_bridge as bridge


class SIMAccessRecoveryTests(unittest.TestCase):
    def card(self, channels=(1, 2, 3)):
        card = bridge.ModemCard.__new__(bridge.ModemCard)
        card.debug = False
        card.access_health = bridge.SIMAccessHealth(channels)
        return card

    def test_stuck_channel_is_not_hidden_by_healthy_siblings_or_identity_probes(self):
        card = self.card()
        now = [0.0]

        def at(command):
            if '"01' in command:
                raise bridge.ModemError("Phone failure")
            return b'+CSIM: 4,"9000"'

        card._at = Mock(side_effect=at)
        with patch.object(bridge.time, "monotonic", side_effect=lambda: now[0]):
            for second in (0, 15, 31):
                now[0] = second
                self.assertEqual(card.transmit(bridge.SELECT_MF, 1), bytes.fromhex("6F00"))
                self.assertEqual(card.transmit(bridge.SELECT_MF, 2), bytes.fromhex("9000"))
                card.csim(bridge.SELECT_MF)
                self.assertEqual(card.access_health.failed.is_set(), second == 31)
        self.assertEqual(card._at.call_count, 9, "a failed APDU must never be replayed")

    def test_a_success_on_the_same_channel_restarts_its_failure_streak(self):
        card = self.card()
        card._at = Mock(side_effect=[bridge.ModemError("Phone failure"),
                                    b'+CSIM: 4,"9000"', bridge.ModemError("Phone failure")])
        with patch.object(bridge.time, "monotonic", side_effect=(0, 100)):
            for _ in range(3):
                card.transmit(bridge.SELECT_MF, 1)
        self.assertFalse(card.access_health.failed.is_set())
        self.assertEqual(card.access_health.streaks[1][0], 1)

    def test_pin_file_and_authentication_card_errors_are_not_transport_failures(self):
        card = self.card()
        for status in ("63C2", "6982", "6A82", "9862", "6F00", "61FF", "9F10", "6C10"):
            card._at = Mock(return_value=('+CSIM: 4,"%s"' % status).encode())
            for _ in range(10):
                self.assertEqual(card.transmit(bytes.fromhex("0020000100"), 1),
                                 bytes.fromhex(status))
            self.assertEqual(card._at.call_count, 10)
        self.assertFalse(card.access_health.failed.is_set())
        self.assertEqual(card.access_health.streaks, {})

    def test_invalidated_channels_include_extended_cla_and_only_owned_channels(self):
        card = self.card((4, 19))
        card._at = Mock(return_value=b'+CSIM: 4,"6881"')
        with patch.object(bridge.time, "monotonic", side_effect=(0, 0, 15, 15, 31, 31)):
            for _ in range(3):
                card.transmit(bridge.SELECT_MF, 4)
                card.transmit(bytes.fromhex("80E2000000"), 19)
                card.transmit(bridge.SELECT_MF, 2)  # not allocated by this bridge
        self.assertTrue(card.access_health.failed.is_set())
        self.assertEqual(set(card.access_health.streaks), {4, 19})
        self.assertEqual([call.args[0].split('"')[1][:2] for call in card._at.call_args_list],
                         ["40", "CF", "02"] * 3)

    def test_locally_rejected_legacy_cla_and_virtual_open_do_not_count(self):
        card = self.card((4,))
        card._at = Mock(side_effect=AssertionError("local APDU must not reach the modem"))
        for _ in range(10):
            self.assertEqual(card.transmit(bytes.fromhex("A0A40000023F00"), 4),
                             bytes.fromhex("6881"))
            self.assertEqual(card.transmit(bytes.fromhex("0070000001"), 4),
                             bytes.fromhex("049000"))
        self.assertFalse(card.access_health.failed.is_set())
        card._at.assert_not_called()

    def test_short_malformed_and_missing_responses_count_without_replay(self):
        card = self.card((4,))
        card._at = Mock(side_effect=[b'+CSIM: 1,"F"', b'+CSIM: 2,"90"', b"OK"])
        with patch.object(bridge.time, "monotonic", side_effect=(0, 15, 31)):
            for _ in range(3):
                self.assertEqual(card.transmit(bridge.SELECT_MF, 4), bytes.fromhex("6F00"))
        self.assertTrue(card.access_health.failed.is_set())
        self.assertEqual(card._at.call_count, 3)

    def test_serial_io_errors_count_and_are_reported_as_one_failed_apdu(self):
        card = self.card((19,))
        card._at = Mock(side_effect=OSError("USB transport unavailable"))
        with patch.object(bridge.time, "monotonic", side_effect=(0, 15, 31)):
            for _ in range(3):
                self.assertEqual(card.transmit(bridge.SELECT_MF, 19), bytes.fromhex("6F00"))
        self.assertTrue(card.access_health.failed.is_set())
        self.assertEqual(card._at.call_count, 3)

    def test_threshold_requires_three_failures_and_thirty_seconds_on_one_channel(self):
        health = bridge.SIMAccessHealth((1, 4))
        with patch.object(bridge.time, "monotonic", return_value=0):
            for _ in range(100):
                health.observe(1, True)
        self.assertFalse(health.failed.is_set())
        with patch.object(bridge.time, "monotonic", return_value=31):
            health.observe(4, True)
        self.assertFalse(health.failed.is_set(), "separate channels do not share a streak")
        with patch.object(bridge.time, "monotonic", return_value=32):
            health.observe(1, True)
        self.assertTrue(health.failed.is_set())

    def test_unfinished_continuation_does_not_hide_repeated_get_response_failures(self):
        for continuation in ("6110", "9F10", "6C10"):
            with self.subTest(continuation=continuation):
                card = self.card((1,))
                def at(command):
                    if '"01A4' in command:
                        return ('+CSIM: 4,"%s"' % continuation).encode()
                    raise bridge.ModemError("transport unavailable")
                card._at = Mock(side_effect=at)
                with patch.object(bridge.time, "monotonic", side_effect=(0, 15, 31)):
                    for _ in range(3):
                        card.transmit(bridge.SELECT_MF, 1)
                        card.transmit(bytes.fromhex("00C0000010"), 1)
                self.assertTrue(card.access_health.failed.is_set())
                self.assertEqual(card._at.call_count, 6)

    def test_internal_shared_channel_restore_cannot_clear_client_failure_streak(self):
        card = self.card((4,))
        def at(command):
            if '"40A4' in command:
                return b'+CSIM: 4,"9000"'
            raise bridge.ModemError("transport unavailable")
        card._at = Mock(side_effect=at)
        arbiter = bridge.ChannelArbiter(card, 4)
        with patch.object(bridge.time, "monotonic", side_effect=(0, 15, 31)):
            for _ in range(3):
                arbiter._restore(0)
                card.transmit(bytes.fromhex("00B000000A"), 4)
        self.assertTrue(card.access_health.failed.is_set())
        self.assertEqual(card._at.call_count, 6)

    def test_shutdown_drains_real_inflight_at_and_rejects_queued_at_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            entered, release = threading.Event(), threading.Event()
            card = bridge.ModemManagerCard.__new__(bridge.ModemManagerCard)
            card.lock = threading.RLock()
            card.debug, card.timeout, card.modem = False, 10, "fake"
            card.identity = Mock(return_value={"imei": "", "iccid": ""})
            card.open_channel = Mock(side_effect=(1, 2, 3))
            card.close = Mock()
            responses, observations, commands = [], [], []

            def blocked_at(args, **_kwargs):
                command = args[-1]
                commands.append(command)
                if '"007080' in command:
                    return SimpleNamespace(stdout=b'+CSIM: 4,"9000"', stderr=b"", returncode=0)
                entered.set()
                card.access_health.failed.set()
                if not release.wait(2):
                    raise AssertionError("test did not release the in-flight AT")
                return SimpleNamespace(stdout=b'+CSIM: 4,"9000"', stderr=b"", returncode=0)

            def worker(arbiter, _host, _port, slot, _atr, _debug, stopping):
                if slot == 2:
                    stopping.wait(2)
                    return
                if slot == 1:
                    entered.wait(2)
                responses.append(arbiter.card.transmit(bridge.SELECT_MF, arbiter.channel))

            def release_after_shutdown():
                entered.wait(2)
                card.stopping.wait(2)
                observations.append(sum('"007080' in command for command in commands))
                release.set()

            releaser = threading.Thread(target=release_after_shutdown)
            releaser.start()
            try:
                with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                      "--identity-refresh", "0"]), \
                        patch.object(bridge, "ModemManagerCard", return_value=card), \
                        patch.object(bridge, "serve_slot", side_effect=worker), \
                        patch.object(bridge.subprocess, "run", side_effect=blocked_at) as at, \
                        patch.object(bridge.signal, "signal"):
                    self.assertEqual(bridge.main(), bridge.SIM_ACCESS_RECOVERY_EXIT)
                self.assertEqual(at.call_count, 4, "one in-flight AT plus three owned CLOSEs")
                self.assertCountEqual(responses, [bytes.fromhex("9000"), bytes.fromhex("6F00")])
                self.assertEqual(observations, [0], "owned CLOSE must wait for in-flight AT")
                self.assertEqual([command for command in commands if '"007080' in command],
                                 ['--command=AT+CSIM=10,"0070800100"',
                                  '--command=AT+CSIM=10,"0070800200"',
                                  '--command=AT+CSIM=10,"0070800300"'])
            finally:
                release.set()
                releaser.join(2)

    def test_undrainable_worker_forces_terminal_exit_without_closing_its_channels(self):
        class ForcedExit(Exception):
            pass

        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / "modem.json"
            release = threading.Event()
            card = Mock()
            card.identity.return_value = {"imei": "", "iccid": ""}
            card.open_channel.side_effect = (1, 2, 3)
            workers = []
            def blocked_worker(*_args):
                workers.append(threading.current_thread())
                card.access_health.failed.set()
                release.wait(2)
            try:
                with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                      "--identity-refresh", "0",
                                                      "--metadata-file", str(metadata)]), \
                        patch.object(bridge, "ModemManagerCard", return_value=card), \
                        patch.object(bridge, "serve_slot", side_effect=blocked_worker), \
                        patch.object(bridge, "BRIDGE_WORKER_STOP_SECONDS", 0.02), \
                        patch.object(bridge.os, "_exit", side_effect=ForcedExit) as hard_exit, \
                        patch.object(bridge.signal, "signal"):
                    with self.assertRaises(ForcedExit):
                        bridge.main()
                hard_exit.assert_called_once_with(bridge.SIM_ACCESS_CLEANUP_EXIT)
                card.close_channel.assert_not_called()
                card.close.assert_not_called()
                state = json.loads(metadata.read_text())
                self.assertEqual(state["channel_error_code"], "sim_access_cleanup_incomplete")
                self.assertTrue(state["channel_cleanup_pending"])
                self.assertEqual(state["channel_allocated"], 3)
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)

    def test_hard_deadline_actually_terminates_blocked_workers_in_a_child_process(self):
        script = textwrap.dedent('''
            from pathlib import Path
            import sys
            import threading
            from unittest.mock import Mock, patch
            from host import vpcd_modem_bridge as bridge

            metadata, closed = sys.argv[1:]
            card = Mock()
            card.identity.return_value = {"imei": "", "iccid": ""}
            card.open_channel.side_effect = (1, 2, 3)
            card.close_channel.side_effect = lambda _channel: Path(closed).touch()
            def stuck(*_args):
                card.access_health.failed.set()
                threading.Event().wait()
            with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                  "--identity-refresh", "0",
                                                  "--metadata-file", metadata]), \\
                    patch.object(bridge, "ModemManagerCard", return_value=card), \\
                    patch.object(bridge, "serve_slot", side_effect=stuck), \\
                    patch.object(bridge, "BRIDGE_WORKER_STOP_SECONDS", 0.03):
                sys.exit(bridge.main())
        ''')
        with tempfile.TemporaryDirectory() as temp:
            metadata, closed = Path(temp) / "modem.json", Path(temp) / "closed"
            result = subprocess.run([sys.executable, "-c", script, str(metadata), str(closed)],
                                    cwd=Path(__file__).resolve().parents[1], timeout=5,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, bridge.SIM_ACCESS_CLEANUP_EXIT, result.stderr)
            self.assertFalse(closed.exists(), "hard deadline cannot CLOSE an owned in-flight channel")
            state = json.loads(metadata.read_text())
            self.assertEqual(state["channel_allocated"], 3)
            self.assertTrue(state["channel_cleanup_pending"])
            self.assertEqual(state["channel_error_code"], "sim_access_cleanup_incomplete")

    def test_failed_bridge_stops_workers_then_closes_only_owned_channels(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / "modem.json"
            card = Mock()
            card.identity.return_value = {"imei": "", "iccid": ""}
            card.open_channel.side_effect = (4, 5, 19)
            actions = []
            thread = Mock()
            thread.is_alive.return_value = False
            thread.start.side_effect = lambda: card.access_health.failed.set()
            thread.join.side_effect = lambda _timeout: actions.append("joined")
            card.close_channel.side_effect = lambda channel: actions.append(channel) or True
            with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                  "--metadata-file", str(metadata),
                                                  "--identity-refresh", "0"]), \
                    patch.object(bridge, "ModemManagerCard", return_value=card), \
                    patch.object(bridge.threading, "Thread", return_value=thread), \
                    patch.object(bridge.signal, "signal"):
                self.assertEqual(bridge.main(), bridge.SIM_ACCESS_RECOVERY_EXIT)
            self.assertEqual(actions, ["joined", "joined", "joined", 4, 5, 19])
            self.assertEqual(card.access_health.channels, {4, 5, 19})
            state = json.loads(metadata.read_text())
            self.assertEqual(state["channel_status"], "error")
            self.assertEqual(state["channel_allocated"], 0)
            self.assertIn("SIM access failed", state["channel_error"])
            card.close.assert_called_once()

    def test_identity_retirement_is_preserved_during_main_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / "modem.json"
            card = Mock()
            card.identity.return_value = {"imei": "", "iccid": "8900000000000000001"}
            card.open_channel.side_effect = (1, 2, 3)

            def thread_factory(*, target, args, **_kwargs):
                thread = Mock()
                thread.is_alive.return_value = False
                if target is bridge.refresh_metadata:
                    def retire():
                        args[-1].update(channel_status="error", channel_allocated=0,
                                        channel_error="card_identity_changed",
                                        iccid="8900000000000000002", iccid_verified=True)
                        card.access_health.failed.set()
                        args[-2].set()
                    thread.start.side_effect = retire
                return thread

            with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                  "--metadata-file", str(metadata)]), \
                    patch.object(bridge, "ModemManagerCard", return_value=card), \
                    patch.object(bridge.threading, "Thread", side_effect=thread_factory), \
                    patch.object(bridge.signal, "signal"):
                self.assertEqual(bridge.main(), 0)
            state = json.loads(metadata.read_text())
            self.assertEqual(state["channel_error"], "card_identity_changed")
            self.assertEqual(state["iccid"], "8900000000000000002")
            self.assertEqual(state["channel_status"], "error")
            card.close_channel.assert_not_called()

    def test_inflight_identity_read_does_not_republish_ready_after_shutdown(self):
        stopping = threading.Event()
        initial = {"iccid": "8900000000000000001", "iccid_verified": True}
        card = Mock()

        def refresh(_previous):
            # Runtime watchdog can set the stop flag while a slow identity AT read is
            # completing. Its late response must not replace the final error metadata.
            stopping.set()
            return initial

        card.refresh_identity.side_effect = refresh
        with patch.object(bridge, "write_metadata") as publish:
            bridge.refresh_metadata(card, "/fixture", {}, 0, initial, stopping)
        publish.assert_not_called()

    def test_unconfirmed_owned_close_is_terminal_and_keeps_pending_channel_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / "modem.json"
            card = bridge.ModemManagerCard.__new__(bridge.ModemManagerCard)
            card.lock = threading.RLock()
            card.debug, card.timeout, card.modem = False, 10, "fake"
            card.identity = Mock(return_value={"imei": "", "iccid": ""})
            card.open_channel = Mock(side_effect=(1, 2, 3))
            card.close = Mock()
            thread = Mock()
            thread.is_alive.return_value = False
            thread.start.side_effect = lambda: card.access_health.failed.set()
            def at(args, **_kwargs):
                status = b"6A86" if "0070800200" in args[-1] else b"9000"
                return SimpleNamespace(stdout=b'+CSIM: 4,"' + status + b'"',
                                       stderr=b"", returncode=0)
            with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                  "--identity-refresh", "0",
                                                  "--metadata-file", str(metadata)]), \
                    patch.object(bridge, "ModemManagerCard", return_value=card), \
                    patch.object(bridge.threading, "Thread", return_value=thread), \
                    patch.object(bridge.subprocess, "run", side_effect=at) as command, \
                    patch.object(bridge.signal, "signal"):
                self.assertEqual(bridge.main(), bridge.SIM_ACCESS_CLEANUP_EXIT)
            self.assertEqual(command.call_count, 3)
            state = json.loads(metadata.read_text())
            self.assertTrue(state["channel_cleanup_pending"])
            self.assertEqual(state["channel_error_code"], "sim_access_cleanup_incomplete")
            self.assertEqual(state["channel_allocated"], 1)
            self.assertEqual(state["logical_channels"][0]["channel"], 2)


class RecoveryBudgetTests(unittest.TestCase):
    def test_existing_corrupt_budget_fails_closed_without_replacing_original_file(self):
        for document in ("{", "[]", '{"a": {"failures": 1}}'):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "budget.json"
                path.write_text(document)
                budget = bridge.BridgeRecoveryBudget(path)
                self.assertTrue(budget.load_error)
                self.assertFalse(budget.reserve_attempt("a"))
                self.assertFalse(budget.reserve_attempt("unrelated"))
                budget.record_exit("a", 75)
                self.assertEqual(path.read_text(), document)
                self.assertIn("blocked", budget.error("a"))
                self.assertIn("budget_error", budget.snapshot())

    def test_existing_unreadable_budget_fails_closed_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.json"
            path.write_text("{}")
            with patch("builtins.open", side_effect=PermissionError("read refused")):
                budget = bridge.BridgeRecoveryBudget(path)
            self.assertFalse(budget.reserve_attempt("a"))
            budget.record_exit("a", 75)
            self.assertEqual(path.read_text(), "{}")

    def test_replacements_exhaust_hourly_budget_across_supervisor_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sim-access-recovery-budget.json"
            with patch.object(bridge.time, "time", return_value=10000):
                budget = bridge.BridgeRecoveryBudget(path)
                budget.record_exit("a", bridge.SIM_ACCESS_RECOVERY_EXIT)
                self.assertEqual(budget.retry_at("a"), 10015)
                self.assertEqual(budget.retry_at("b"), 0)
            for now, code, expected in ((10020, 1, 10050), (10060, 75, 10120),
                                         (10130, 1, 13600)):
                with patch.object(bridge.time, "time", return_value=now):
                    budget = bridge.BridgeRecoveryBudget(path)
                    self.assertTrue(budget.reserve_attempt("a"))
                    budget.record_exit("a", code)
                    self.assertEqual(budget.retry_at("a"), expected)
            with patch.object(bridge.time, "time", return_value=11000):
                self.assertIn("paused after three", budget.error("a"))
                self.assertEqual(budget.records["a"]["failures"], 4)
            with patch.object(bridge.time, "time", return_value=13601):
                self.assertEqual(budget.error("a"), "")
                self.assertEqual(budget.retry_at("a"), 0)
                self.assertEqual(budget.snapshot(), {})
                budget.record_exit("a", 75)
                self.assertEqual(budget.records["a"]["failures"], 1)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_stable_or_slow_replacements_do_not_erase_the_hourly_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                budget.record_exit("a", 75)
            for now, code in ((10120, 1), (10240, 75), (10360, 1)):
                with patch.object(bridge.time, "time", return_value=now):
                    self.assertTrue(budget.reserve_attempt("a"))
                    budget.record_exit("a", code, uptime=120)
            with patch.object(bridge.time, "time", return_value=10360):
                self.assertTrue(budget.records["a"]["limited"])
                self.assertEqual(budget.retry_at("a"), 13600)

    def test_unobserved_child_exits_cannot_bypass_budget_after_supervisor_crashes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.json"
            with patch.object(bridge.time, "time", return_value=10000):
                bridge.BridgeRecoveryBudget(path).record_exit("a", 75)
            # Each supervisor dies after reserving/spawning, before observing any child
            # exit. Reloading cannot forget those attempts or permit a fourth process.
            for now in (10020, 10060, 10130):
                with patch.object(bridge.time, "time", return_value=now):
                    budget = bridge.BridgeRecoveryBudget(path)
                    self.assertTrue(budget.reserve_attempt("a"))
                    self.assertFalse(bridge.BridgeRecoveryBudget(path).reserve_attempt("a"))
            with patch.object(bridge.time, "time", return_value=11000):
                budget = bridge.BridgeRecoveryBudget(path)
                self.assertEqual(budget.records["a"]["attempts"], 3)
                self.assertFalse(budget.reserve_attempt("a"))
                self.assertTrue(budget.reserve_attempt("b"))

    def test_failed_reservation_write_never_grants_spawn_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                budget.record_exit("a", 75)
            with patch.object(bridge.time, "time", return_value=10020), \
                    patch.object(bridge, "write_metadata", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    budget.reserve_attempt("a")
            self.assertEqual(budget.records["a"]["attempts"], 0)

    def test_diagnostics_are_detached_and_expire_without_touching_other_modems(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                budget.record_exit("a", 75)
                snapshot = budget.snapshot()
                snapshot["a"]["count"] = 100
                self.assertEqual(budget.records["a"]["failures"], 1)
            with patch.object(bridge.time, "time", return_value=11000):
                budget.record_exit("b", 75)
            with patch.object(bridge.time, "time", return_value=13600):
                self.assertEqual(set(budget.snapshot()), {"b"})
                self.assertEqual(budget.retry_at("a"), 0)
                self.assertEqual(budget.retry_at("b"), 11015)

    def test_ordinary_startup_errors_do_not_open_a_runtime_recovery_incident(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            budget.record_exit("a", 1)
            self.assertEqual(budget.records, {})
            self.assertFalse(Path(budget.path).exists())

    def test_identity_retirement_preserves_attempts_without_adding_failure_or_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                budget.record_exit("a", 75)
            with patch.object(bridge.time, "time", return_value=10020):
                self.assertTrue(budget.reserve_attempt("a"))
                previous = dict(budget.records["a"])
            with patch.object(bridge.time, "time", return_value=10100):
                budget.record_exit("a", 0, uptime=80)
                self.assertEqual(budget.records["a"], previous)


class NativeSupervisorRecoveryTests(unittest.TestCase):
    MODEM = {"id": "a", "tty": "/dev/ttyUSB2", "usb_generation": "generation-a"}

    def app(self, directory):
        from host.mdd_orchestrator import Orchestrator
        app = Orchestrator(Path(directory), Path(directory))
        app.root.mkdir(parents=True, exist_ok=True)
        return app

    def reconcile(self, app, spawn, modems=None):
        from host import mdd_orchestrator as orchestrator
        with patch.object(app, "usb_modems", return_value=modems or [self.MODEM]), \
                patch.object(app, "driver_slots", return_value=3), \
                patch.object(app, "reader_stanza", return_value="fake reader"), \
                patch.object(app, "disable_distro_vpcd_reader", return_value=False), \
                patch.object(app, "reader_config_unreadable", return_value=False), \
                patch.dict(os.environ, {"MDD_VPCD_READER_CONFIG": str(app.root / "readers")}), \
                patch.object(orchestrator, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="", stderr="")), \
                patch.object(orchestrator.subprocess, "Popen", spawn):
            return app.reconcile_hardware({}, {})

    def test_native_spawn_reserves_before_child_creation_and_restart_without_exit_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bridge.time, "time", return_value=10000):
                self.app(temp).sim_access_recovery.record_exit("a", 75)
            observed = []
            def spawn(_command, **_kwargs):
                saved = bridge.BridgeRecoveryBudget(self.app(temp).sim_access_recovery.path)
                observed.append(saved.records["a"]["attempts"])
                child = Mock()
                child.poll.return_value = None
                return child
            # No supervisor observes a child's exit before it is replaced by a new
            # supervisor. Each actual Popen still sees its durable reservation first.
            for now in (10020, 10060, 10130):
                with patch.object(bridge.time, "time", return_value=now):
                    self.reconcile(self.app(temp), Mock(side_effect=spawn))
            self.assertEqual(observed, [1, 2, 3])
            with patch.object(bridge.time, "time", return_value=11000):
                spawn = Mock(return_value=Mock())
                self.reconcile(self.app(temp), spawn,
                               [self.MODEM, {**self.MODEM, "id": "b", "tty": "/dev/ttyUSB3"}])
                spawn.assert_called_once()
                command = spawn.call_args.args[0]
                self.assertEqual(command[command.index("--hardware-id") + 1], "b")

    def test_corrupt_budget_blocks_native_spawn_after_supervisor_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            path = Path(app.sim_access_recovery.path)
            path.write_text("{broken")
            app = self.app(temp)
            spawn = Mock()
            self.reconcile(app, spawn)
            spawn.assert_not_called()
            self.assertEqual(path.read_text(), "{broken")

    def test_native_popen_failures_spend_reserved_attempts_and_do_not_modify_terminal_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            legacy = {"version": 1, "devices": {}}
            app.bridge_recovery_path.write_text(json.dumps(legacy), encoding="utf-8")
            original = app.bridge_recovery_path.read_bytes()
            with patch.object(bridge.time, "time", return_value=10000):
                app.sim_access_recovery.record_exit("a", 75)
            for now in (10020, 10060, 10130):
                with patch.object(bridge.time, "time", return_value=now):
                    with self.assertRaises(OSError):
                        self.reconcile(self.app(temp), Mock(side_effect=OSError("spawn failed")))
            with patch.object(bridge.time, "time", return_value=11000):
                app = self.app(temp)
                spawn = Mock()
                self.reconcile(app, spawn)
                spawn.assert_not_called()
                self.assertEqual(app.sim_access_recovery.records["a"]["attempts"], 3)
            self.assertEqual(app.bridge_recovery_path.read_bytes(), original)
            self.assertNotEqual(app.sim_access_recovery.path, str(app.bridge_recovery_path))

    def test_managed_stop_observing_cleanup_exit_persists_terminal_and_blocks_new_supervisor(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.hw_state_path.write_text(json.dumps({"assignments": {"a": self.MODEM}}))
            process = Mock()
            process.poll.return_value = None
            process.returncode = None
            process.wait.side_effect = lambda _timeout: setattr(
                process, "returncode", bridge.SIM_ACCESS_CLEANUP_EXIT)
            app.bridges["a"] = process
            app.stop_bridge("a")
            self.assertEqual(app._bridge_terminal["a"]["error_code"],
                             "sim_access_cleanup_incomplete")
            replacement = self.app(temp)
            self.assertFalse(replacement._bridge_retry_due("a"))
            self.assertTrue(replacement._bridge_retry_due("b"))

    def write_exit_metadata(self, app, error, generation="generation-a"):
        path = app.data / "modems" / "a.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hardware_id": "a", "usb_generation": generation,
                                    "channel_error_code": error, "channel_status": "error"}))
        return path

    def test_initial_runtime_exit_write_failure_is_recovered_from_same_generation_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            self.write_exit_metadata(app, "sim_access_runtime_failed")
            with patch.object(app.sim_access_recovery, "record_exit", side_effect=OSError("disk unavailable")):
                spawn = Mock()
                with self.assertRaises(OSError):
                    self.reconcile(app, spawn)
                spawn.assert_not_called()
            with patch.object(bridge.time, "time", return_value=10000):
                restored = self.app(temp)
                spawn = Mock()
                self.reconcile(restored, spawn)
                spawn.assert_not_called()
                self.assertEqual(restored.sim_access_recovery.records["a"]["since"], 10000)
            with patch.object(bridge.time, "time", return_value=13601):
                restored = self.app(temp)
                spawn = Mock(return_value=Mock())
                self.reconcile(restored, spawn)
                spawn.assert_called_once()
                self.assertEqual(restored.sim_access_recovery.records["a"]["since"], 10000)

    def test_cleanup_terminal_write_failure_cannot_bypass_metadata_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            self.write_exit_metadata(app, "sim_access_cleanup_incomplete")
            with patch.object(app, "_persist_bridge_terminal", side_effect=OSError("disk unavailable")):
                spawn = Mock()
                with self.assertRaises(OSError):
                    self.reconcile(app, spawn)
                spawn.assert_not_called()
            restored = self.app(temp)
            spawn = Mock()
            self.reconcile(restored, spawn)
            spawn.assert_not_called()
            self.assertEqual(restored._bridge_terminal["a"]["error_code"],
                             "sim_access_cleanup_incomplete")
            self.reconcile(restored, spawn,
                           [{**self.MODEM, "usb_generation": "new-generation"}])
            spawn.assert_called_once()

    def test_old_generation_runtime_marker_does_not_create_new_incident(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            self.write_exit_metadata(app, "sim_access_runtime_failed", "old-generation")
            spawn = Mock(return_value=Mock())
            self.reconcile(app, spawn)
            spawn.assert_called_once()
            self.assertEqual(app.sim_access_recovery.records, {})

    def test_qmi_recovery_never_writes_after_stop_reports_cleanup_failure(self):
        from host import mdd_orchestrator as orchestrator
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.hw_state_path.write_text(json.dumps({"assignments": {"a": self.MODEM}}))
            process = Mock()
            process.poll.return_value = None
            process.returncode = None
            process.wait.side_effect = lambda _timeout: setattr(
                process, "returncode", bridge.SIM_ACCESS_CLEANUP_EXIT)
            app.bridges["a"] = process
            app._set_cellular_recovery(
                "a", "pending", usb_generation="generation-a", deadline_at=bridge.time.time() + 600,
                expected_iccid_sha256=hashlib.sha256(b"target-card").hexdigest())
            with patch.object(app, "modem_snapshot", return_value={
                    "sim_iccid": "old-card", "mm_object": "/org/freedesktop/ModemManager1/Modem/1"}), \
                    patch.object(orchestrator, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="modem.generic.primary-port: cdc-wdm1\n"
                        "modem.generic.primary-sim-slot: 1")) as run:
                blocked = app.process_cellular_recoveries([self.MODEM], {"a": {"flight_mode": False}}, True)
            self.assertEqual(blocked, {"a"})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "mmcli")
            self.assertEqual(app._cellular_recoveries["a"]["error_code"],
                             "sim_access_cleanup_incomplete")

    def test_explicit_rescan_acknowledges_only_the_previous_cleanup_failure(self):
        from host import mdd_orchestrator as orchestrator
        for new_failure in (False, True):
            with self.subTest(new_failure=new_failure), tempfile.TemporaryDirectory() as temp:
                app = self.app(temp)
                metadata = self.write_exit_metadata(app, "sim_access_cleanup_incomplete")
                app.hw_state_path.write_text(json.dumps({"assignments": {"a": self.MODEM}}))
                app._restore_bridge_exit_evidence(self.MODEM)
                request = {"operation_id": "0" * 16, "requested_at": 1, "scope": "device",
                           "device_id": "a", "device_type": "modem"}
                app.device_rescan_request_path.write_text(json.dumps(request))
                if new_failure:
                    process = Mock()
                    process.poll.return_value = None
                    process.returncode = None
                    def finish(_timeout):
                        process.returncode = bridge.SIM_ACCESS_CLEANUP_EXIT
                        self.write_exit_metadata(app, "sim_access_cleanup_incomplete")
                    process.wait.side_effect = finish
                    app.bridges["a"] = process
                with patch.object(orchestrator, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="", stderr="")):
                    app.process_device_rescan_request()
                restored = self.app(temp)
                if new_failure:
                    self.assertFalse(restored._restore_bridge_exit_evidence(self.MODEM))
                    self.assertFalse(restored._bridge_retry_due("a"))
                else:
                    self.assertTrue(restored._restore_bridge_exit_evidence(self.MODEM))
                    self.assertTrue(restored._bridge_retry_due("a"))
                    self.assertEqual(json.loads(metadata.read_text())["channel_error_code"],
                                     "operator_retry_acknowledged")

    def test_profile_restart_never_resets_uim_after_incomplete_channel_cleanup(self):
        for preexisting in (False, True):
            with self.subTest(preexisting=preexisting), tempfile.TemporaryDirectory() as temp:
                app = self.app(temp)
                app.hw_state_path.write_text(json.dumps({"assignments": {"a": self.MODEM}}))
                if preexisting:
                    app._bridge_terminal["a"] = {
                        "error_code": "sim_access_cleanup_incomplete", "usb_generation": "generation-a"}
                    app._persist_bridge_terminal()
                process = Mock()
                process.poll.return_value = None
                process.returncode = None
                process.wait.side_effect = lambda _timeout: setattr(
                    process, "returncode", bridge.SIM_ACCESS_CLEANUP_EXIT)
                app.bridges["a"] = process
                app.bridge_restart_request_dir.mkdir(parents=True)
                request = {"request_id": "cleanup-test", "device_id": "a",
                           "expected_iccid_sha256": "0" * 64, "usb_generation": "generation-a",
                           "uim_reset": True, "cellular_refresh": True}
                (app.bridge_restart_request_dir / "cleanup-test.json").write_text(json.dumps(request))
                with patch.object(app, "_uim_power_cycle") as reset, \
                        patch.object(app, "_set_cellular_recovery") as cellular:
                    app.process_bridge_restart_requests()
                reset.assert_not_called()
                cellular.assert_not_called()
                state = json.loads((app.bridge_restart_status_dir / "cleanup-test.json").read_text())
                self.assertEqual(state["state"], "failed")
                self.assertEqual(state["error"], "sim_access_cleanup_incomplete")
                self.assertFalse(self.app(temp)._bridge_retry_due("a"))


if __name__ == "__main__":
    unittest.main()
