"""Exercise native identity recovery with fake commands, never live ModemManager."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import mdd_orchestrator as host


class NativeModemIdentityTests(unittest.TestCase):
    TARGET = "89000000000000000011"
    OLD = "89000000000000000022"
    MODEM = "/org/freedesktop/ModemManager1/Modem/7"
    SIM = "/org/freedesktop/ModemManager1/SIM/9"

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = host.Orchestrator(self.directory / "data", self.directory)
        self.modem = {"id": "modem-a", "tty": "/dev/ttyUSB2",
                      "usb_generation": "generation-a"}
        self.app._set_cellular_recovery(
            "modem-a", "pending", operation_id="switch-a",
            expected_iccid_sha256=hashlib.sha256(self.TARGET.encode()).hexdigest(),
            usb_generation="generation-a", deadline_at=time.time() + 600)

    def recover(self):
        return self.app.process_cellular_recoveries(
            [self.modem], {"modem-a": {"flight_mode": False}}, True)

    def test_physical_port_lookup_does_not_match_a_longer_tty_name(self):
        other = "/org/freedesktop/ModemManager1/Modem/3"
        responses = [
            SimpleNamespace(returncode=0, stdout=f"{other}\n{self.MODEM}\n"),
            SimpleNamespace(returncode=0, stdout="modem.generic.primary-port : ttyUSB20\n"),
            SimpleNamespace(returncode=0, stdout="modem.generic.primary-port : ttyUSB2\n"),
        ]
        with patch.object(host, "run", side_effect=responses) as run:
            result = self.app.modemmanager_modem_for_tty("/dev/ttyUSB2", timeout=1)
        self.assertEqual(result, self.MODEM)
        self.assertEqual(len(run.call_args_list), 3)
        for call in run.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 1)

    def test_port_lookup_has_one_deadline_for_all_devices(self):
        with patch.object(host.time, "monotonic", side_effect=[10.0, 10.0, 11.1]), \
                patch.object(host, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=self.MODEM)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.app.modemmanager_modem_for_tty("/dev/ttyUSB2", timeout=1)
        self.assertEqual(run.call_count, 1)

    def test_missing_or_conflicting_iccid_never_borrows_bridge_imsi(self):
        self.app._set_cellular_recovery(
            "modem-a", "waiting_identity", power_cycle_completed=True,
            identity_deadline_at=time.time() + 60)
        for iccid in ("", "--", self.OLD):
            with self.subTest(iccid=iccid), \
                    patch.object(self.app, "modem_snapshot", return_value={
                        "sim_iccid": iccid, "sim_imsi": "001010000000001",
                        "sim_present": True, "subscription_available": True}), \
                    patch.object(host, "run") as run:
                self.assertEqual(self.recover(), {"modem-a"})
                self.assertEqual(self.app._cellular_recoveries["modem-a"]["state"],
                                 "waiting_identity")
                run.assert_not_called()

    def test_matching_current_iccid_and_subscription_need_no_hardware_write(self):
        with patch.object(self.app, "modem_snapshot", return_value={
                "sim_iccid": self.TARGET, "sim_present": True,
                "subscription_available": True}), patch.object(host, "run") as run:
            self.assertEqual(self.recover(), set())
        self.assertEqual(self.app._cellular_recoveries["modem-a"]["state"], "ready")
        run.assert_not_called()

    def fake_mmcli(self):
        fakebin = self.directory / "bin"
        fakebin.mkdir()
        command = fakebin / "mmcli"
        command.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["MDD_MM_TIMEOUT_TEST_LOG"])
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a") as handle:
    handle.write(json.dumps({"pid": os.getpid(), "args": args}) + "\n")
mode = os.environ["MDD_MM_TIMEOUT_TEST_MODE"]
modem_queries = sum(item["args"] == args for item in previous) + 1
hang = ((mode == "listing" and args == ["-L"])
        or (mode == "modem" and args[0] == "-m")
        or (mode == "sim" and args[0] == "-i")
        or (mode == "qmi_detail" and args[0] == "-m" and modem_queries == 3))
if hang:
    time.sleep(30)
if args == ["-L"]:
    print("/org/freedesktop/ModemManager1/Modem/7")
elif args[0] == "-m":
    print("modem.generic.primary-port : ttyUSB2")
    print("modem.generic.sim : /org/freedesktop/ModemManager1/SIM/9")
elif args[0] == "-i":
    print("sim.properties.iccid : 89000000000000000022")
    print("sim.properties.imsi : 001010000000001")
else:
    sys.exit(2)
''', encoding="utf-8")
        command.chmod(0o755)
        return fakebin

    def test_real_query_timeouts_reap_child_and_fail_only_target_without_uim_writes(self):
        fakebin = self.fake_mmcli()
        other = Mock()
        other.poll.return_value = None
        self.app.bridges["modem-b"] = other
        log = self.directory / "commands.jsonl"
        for mode in ("listing", "modem", "sim", "qmi_detail"):
            with self.subTest(stage=mode):
                log.unlink(missing_ok=True)
                self.app._set_cellular_recovery("modem-a", "pending")
                environment = {"PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
                               "MDD_MM_TIMEOUT_TEST_LOG": str(log),
                               "MDD_MM_TIMEOUT_TEST_MODE": mode}
                started = time.monotonic()
                with patch.dict(os.environ, environment), \
                        patch.object(host, "MODEMMANAGER_QUERY_TIMEOUT_SECONDS", .15):
                    self.assertEqual(self.recover(), {"modem-a"})
                    self.assertEqual(self.recover(), {"modem-a"})
                self.assertLess(time.monotonic() - started, 2)
                status = self.app._cellular_recoveries["modem-a"]
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["error_code"], "modemmanager_timeout")
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(commands)
                self.assertFalse(any("--reset" in item["args"] for item in commands))
                with self.assertRaises(ProcessLookupError):
                    os.kill(commands[-1]["pid"], 0)
                other.terminate.assert_not_called()

    def test_observational_timeout_does_not_stop_bridges_or_write_radio(self):
        fakebin = self.fake_mmcli()
        other = Mock()
        other.poll.return_value = None
        self.app.bridges["modem-b"] = other
        log = self.directory / "commands.jsonl"
        environment = {"PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
                       "MDD_MM_TIMEOUT_TEST_LOG": str(log),
                       "MDD_MM_TIMEOUT_TEST_MODE": "listing"}
        with patch.dict(os.environ, environment), \
                patch.object(host, "MODEMMANAGER_QUERY_TIMEOUT_SECONDS", .15):
            self.assertEqual(self.app.modemmanager_modem_for_tty("/dev/ttyUSB2"), "")
        environment["MDD_MM_TIMEOUT_TEST_MODE"] = "sim"
        log.unlink()
        with patch.dict(os.environ, environment), \
                patch.object(host, "MODEMMANAGER_QUERY_TIMEOUT_SECONDS", .15):
            self.app.apply_device_radios(
                [self.modem], {"modem-a": {"flight_mode": False}},
                through_modemmanager=True)
        snapshot = self.app.cellular_states["modem-a"]
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["query_error_code"], "modemmanager_timeout")
        self.assertNotEqual(snapshot.get("failed_reason"), "sim-missing")
        self.assertNotIn("modem-a", self.app.radio_states)
        commands = [json.loads(line)["args"] for line in log.read_text().splitlines()]
        self.assertFalse(any(action in args for args in commands
                             for action in ("--enable", "--disable", "--reset")))
        other.terminate.assert_not_called()

    def test_expired_claim_grace_and_query_timeout_do_not_take_serial_ownership(self):
        fakebin = self.fake_mmcli()
        peer_modem = {"id": "modem-b", "tty": "/dev/ttyUSB3",
                      "usb_generation": "generation-b"}
        peer = Mock()
        peer.poll.return_value = None
        self.app.bridges["modem-b"] = peer
        self.app.bridge_ports["modem-b"] = host.BASE_VPCD_PORT + host.VPCD_PORT_STRIDE
        self.app.last_reader_config = "reader-modem-a\nreader-modem-b"
        self.app._unclaimed_since[self.modem["tty"]] = (
            time.time() - host.MM_CLAIM_GRACE_SECONDS - 1)
        log = self.directory / "commands.jsonl"
        environment = {"PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
                       "MDD_MM_TIMEOUT_TEST_LOG": str(log),
                       "MDD_MM_TIMEOUT_TEST_MODE": "listing",
                       "MDD_VPCD_READER_CONFIG": str(self.directory / "readers" / "mdd-reader")}
        with patch.dict(os.environ, environment), \
                patch.object(host, "MODEMMANAGER_QUERY_TIMEOUT_SECONDS", .15), \
                patch.object(self.app, "usb_modems", return_value=[self.modem, peer_modem]), \
                patch.object(self.app, "driver_slots", return_value=3), \
                patch.object(self.app, "reader_stanza", side_effect=lambda modem, *_: "reader-" + modem["id"]), \
                patch.object(self.app, "disable_distro_vpcd_reader", return_value=False), \
                patch.object(self.app, "reader_config_unreadable", return_value=False), \
                patch.object(self.app, "mm_refusal_logged", return_value=True), \
                patch.object(self.app.sim_access_recovery, "reserve_attempt") as reserve, \
                patch.object(host.subprocess, "Popen", wraps=host.subprocess.Popen) as spawn:
            self.app.reconcile_hardware({}, {}, through_modemmanager=True)
        reserve.assert_not_called()
        # Only the real fake-mmcli query ran; no bridge or serial fallback was spawned.
        self.assertEqual(len(spawn.call_args_list), 1)
        self.assertEqual(spawn.call_args.args[0][0], "mmcli")
        self.assertNotIn("modem-a", self.app.bridges)
        self.assertNotIn("modem-a", self.app._degraded)
        self.assertIs(self.app.bridges["modem-b"], peer)
        peer.terminate.assert_not_called()

    def test_missing_mmcli_is_a_persistent_explicit_failure(self):
        with patch.object(self.app, "modem_snapshot", side_effect=FileNotFoundError("mmcli")):
            self.assertEqual(self.recover(), {"modem-a"})
        restored = host.Orchestrator(self.app.data, self.directory)
        self.assertEqual(restored._cellular_recoveries["modem-a"]["state"], "failed")
        self.assertEqual(restored._cellular_recoveries["modem-a"]["error_code"],
                         "modemmanager_unavailable")


if __name__ == "__main__":
    unittest.main()
