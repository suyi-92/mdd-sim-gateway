"""eSIM switch regressions; no production devices or services are accessed."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge_identity_fixture import verified_bridge
from control.app import main
from host import mdd_orchestrator as host


class ProfileIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_always_selects_automatic_and_later_profile_is_not_overwritten(self):
        for current, mode in (("new-card", "automatic"), ("new-card", "manual"), ("later-card", "automatic")):
            with self.subTest(current=current, mode=mode), \
                    patch.object(main.network_operations, "busy", return_value=False), \
                    patch.object(main, "_cellular_network_target", return_value=({}, {
                        "iccid": current, "cellular_network_mode": mode,
                        "cellular_operator_id": "00101" if mode == "manual" else ""}, "modem")), \
                    patch.object(main, "api_device_cellular_network_select", new=AsyncMock()) as select:
                await main._esim_restore_cellular_selection("modem-a", "new-card")
            if current == "new-card":
                select.assert_awaited_once_with("modem-a", {"mode": "automatic",
                    "operator_id": ""}, background=True)
            else:
                select.assert_not_awaited()

    async def test_verified_new_profile_outranks_old_mm_and_monitor_snapshots(self):
        identity = {**verified_bridge(), "hardware_id": "modem-a", "iccid": "new-card"}
        observed = {"shared": {"modemmanager_active": True}, "devices": {"modem-a": {
            "present": True, "actual": {"vowifi_bridge_active": True, "cellular_radio_enabled": True},
            "cellular": {"available": True, "sim_present": True, "sim_iccid": "old-card",
                         "registration": "roaming", "operator": "Old network"}}}}
        lines = [{"id": "old", "iccid": "old-card", "mcc": "515", "mnc": "02"},
                 {"id": "new", "iccid": "new-card", "mcc": "454", "mnc": "00"}]
        with patch.object(main, "_device_sources", return_value=({}, observed, {})), \
                patch.object(main, "_device_identities", return_value={"modem-a": identity}), \
                patch.object(main.hub, "cards_list", return_value=[{
                    "hardware_id": "modem-a", "present": True, "iccid": "old-card"}]), \
                patch.object(main.cfg, "list_instances", return_value=lines), \
                patch.object(main.device_state, "native_reader_devices", return_value={}), \
                patch.object(main.device_state, "hardware", return_value={}), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main, "_cached_line_status", return_value=None), \
                patch.object(main.egress, "status", return_value={}):
            devices = await main._unified_devices()
        self.assertEqual(devices[0]["instance_id"], "new")
        self.assertTrue(devices[0]["sim"]["present"])
        self.assertIsNone(devices[0]["cellular"])
        self.assertEqual(devices[0]["sim"]["carrier"]["plmn"], "454-00")

    def test_no_bridge_or_confirmed_card_removal_cannot_override_mm(self):
        identity = {**verified_bridge(), "iccid": "new-card"}
        for actual, failed in ((False, ""), (True, "sim-missing")):
            with self.subTest(actual=actual, failed=failed):
                self.assertFalse(main._device_bridge_identity_current(identity, {
                    "present": True, "actual": {"vowifi_bridge_active": actual},
                    "cellular": {"failed_reason": failed}}))
        self.assertFalse(main._device_bridge_identity_current({**identity, "updated_at": 0}, {
            "present": True, "actual": {"vowifi_bridge_active": True}}))

    async def test_profile_switch_preserves_disabled_device_intent(self):
        target = {"id": "new", "iccid": "new-card", "enabled": False}
        with patch.object(main, "_esim_restart_modem_bridge", new=AsyncMock(return_value={})), \
                patch.object(main, "_esim_refresh_modem_readers", new=AsyncMock(return_value=({}, []))), \
                patch.object(main, "_match_instance_by_iccid", return_value=target), \
                patch.object(main, "_refresh_instance_reader_binding", return_value=target), \
                patch.object(main, "_esim_vowifi_requested", return_value=False), \
                patch.object(main.cfg, "upsert_instance", side_effect=lambda x: {**target, **x}) as saved, \
                patch.object(main.egress, "publish"):
            result = await main._esim_recover_profile_switch("reader", "modem-a", "new-card")
        self.assertFalse(result["start_allowed"])
        self.assertFalse(saved.call_args.args[0]["enabled"])
        self.assertEqual(saved.call_args.args[0]["cellular_network_mode"], "automatic")
        self.assertEqual(saved.call_args.args[0]["cellular_operator_id"], "")

    async def test_later_device_off_cancels_background_start(self):
        with patch.object(main, "_esim_vowifi_requested", return_value=False), \
                patch.object(main.cfg, "get_instance", return_value={"id": "new"}), \
                patch.object(main, "_esim_profile_event", new=AsyncMock()) as event, \
                patch.object(main, "_start_instance", new=AsyncMock()) as start, \
                patch.object(main.hub, "esim_line_recoveries", set()):
            await main._esim_start_profile_line("reader", "modem-a", "new-card", "new")
        start.assert_not_awaited()
        self.assertEqual(event.await_args.args[2], "line_disabled")


class ProfileModemRefreshTests(unittest.TestCase):
    def test_flight_mode_defers_baseband_work_and_restart_resumes_same_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = host.Orchestrator(root / "data", root)
            app.root.mkdir(parents=True, exist_ok=True)
            host.atomic_json(app.hw_state_path, {"assignments": {"modem-a": {
                "tty": "/dev/ttyUSB2", "usb_generation": "generation-a"}}})
            host.atomic_json(app.bridge_restart_request_dir / "fixture.json", {
                "request_id": "fixture", "device_id": "modem-a",
                "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest(),
                "cellular_refresh": True})
            app.process_bridge_restart_requests()
            modem = {"id": "modem-a", "tty": "/dev/ttyUSB2",
                     "usb_generation": "generation-a"}
            self.assertEqual(app.process_cellular_recoveries(
                [modem], {"modem-a": {"flight_mode": True}}, False), set())
            self.assertEqual(app._cellular_recoveries["modem-a"]["state"],
                             "waiting_flight_mode")

            # A service restart loads the task and completes it only for the same USB/card.
            resumed = host.Orchestrator(root / "data", root)
            with patch.object(resumed, "modem_snapshot", return_value={
                    "sim_iccid": "new-card",
                    "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}), \
                    patch.object(host, "run") as run:
                blocked = resumed.process_cellular_recoveries(
                    [modem], {"modem-a": {"flight_mode": False}}, True)
            self.assertEqual(blocked, set())
            self.assertEqual(resumed._cellular_recoveries["modem-a"]["state"], "ready")
            run.assert_not_called()

    def test_usb_generation_change_cancels_deferred_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            app = host.Orchestrator(Path(temp), Path(temp))
            app._cellular_recoveries["modem-a"] = {
                "device_id": "modem-a", "state": "waiting_flight_mode",
                "usb_generation": "old-generation", "deadline_at": time.time() + 60,
                "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest(),
            }
            app.process_cellular_recoveries(
                [{"id": "modem-a", "usb_generation": "new-generation"}],
                {"modem-a": {"flight_mode": False}}, True)
            self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "cancelled")
            self.assertEqual(app._cellular_recoveries["modem-a"]["error_code"],
                             "device_generation_changed")

    def test_failed_initialization_keeps_radio_enable_blocked_until_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            app = host.Orchestrator(Path(temp), Path(temp))
            app._cellular_recoveries["modem-a"] = {
                "device_id": "modem-a", "state": "failed",
                "error_code": "sim_identity_unavailable",
            }
            blocked = app.process_cellular_recoveries(
                [{"id": "modem-a", "usb_generation": "generation-a"}],
                {"modem-a": {"flight_mode": False}}, True)
            self.assertEqual(blocked, {"modem-a"})

    def test_only_target_with_stale_sim_is_reset_and_returning_usb_is_awaited(self):
        with tempfile.TemporaryDirectory() as temp:
            app = host.Orchestrator(Path(temp), Path(temp))
            old, other = Mock(), Mock()
            old.poll.return_value = other.poll.return_value = None
            app.bridges = {"modem-a": old, "modem-b": other}
            host.atomic_json(app.hw_state_path, {"assignments": {"modem-a": {
                "tty": "/dev/ttyUSB2", "usb_generation": "generation-a"}}})
            host.atomic_json(app.bridge_restart_request_dir / "fixture.json", {
                "request_id": "fixture", "device_id": "modem-a",
                "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest()})
            app.process_bridge_restart_requests()
            self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "pending")
            other.terminate.assert_not_called()
            modem = {"id": "modem-a", "tty": "/dev/ttyUSB2",
                     "usb_generation": "generation-a"}
            with patch.object(app, "modem_snapshot", return_value={
                    "sim_iccid": "old-card", "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}), \
                    patch.object(host, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="modem.generic.device: /sys/devices/fixture\n"
                        "modem.generic.primary-port: cdc-wdm9\nmodem.generic.primary-sim-slot: 1")) as run:
                blocked = app.process_cellular_recoveries(
                    [modem], {"modem-a": {"flight_mode": False}}, True)
            self.assertEqual(blocked, {"modem-a"})
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/9", "--output-keyvalue"],
                ["timeout", "25s", "qmicli", "--device-open-proxy", "-d", "/dev/cdc-wdm9", "--uim-sim-power-off=1"],
                ["timeout", "25s", "qmicli", "--device-open-proxy", "-d", "/dev/cdc-wdm9", "--uim-sim-power-on=1"]])
            other.terminate.assert_not_called()
            self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "waiting_identity")
            with patch.object(app, "modem_snapshot", return_value={
                    "sim_iccid": "new-card", "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}):
                blocked = app.process_cellular_recoveries(
                    [modem], {"modem-a": {"flight_mode": False}}, True)
            self.assertEqual(blocked, set())
            self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "ready")

    def test_matching_sim_is_not_reset_and_failure_is_explicit(self):
        for current, code in (("new-card", 0), ("old-card", 1)):
            with self.subTest(current=current), tempfile.TemporaryDirectory() as temp:
                app = host.Orchestrator(Path(temp), Path(temp))
                host.atomic_json(app.hw_state_path, {"assignments": {"modem-a": {
                    "tty": "/dev/ttyUSB2", "usb_generation": "generation-a"}}})
                host.atomic_json(app.bridge_restart_request_dir / "fixture.json", {
                    "request_id": "fixture", "device_id": "modem-a",
                    "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest()})
                app.process_bridge_restart_requests()
                modem = {"id": "modem-a", "tty": "/dev/ttyUSB2",
                         "usb_generation": "generation-a"}
                with patch.object(app, "modem_snapshot", return_value={
                        "sim_iccid": current, "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}), \
                        patch.object(host, "run", side_effect=[
                            SimpleNamespace(returncode=0, stdout="modem.generic.device: /sys/devices/fixture\n"
                                            "modem.generic.primary-port: cdc-wdm9\nmodem.generic.primary-sim-slot: 1"),
                            SimpleNamespace(returncode=code), SimpleNamespace(returncode=code)]) as run:
                    app.process_cellular_recoveries(
                        [modem], {"modem-a": {"flight_mode": False}}, True)
                if current == "new-card":
                    run.assert_not_called()
                    self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "ready")
                else:
                    self.assertEqual(app._cellular_recoveries["modem-a"]["state"], "retry_wait")
                    self.assertIn("--uim-sim-power-on=1", run.call_args.args[0])
