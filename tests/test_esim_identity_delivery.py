"""eSIM switch regressions; no production devices or services are accessed."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge_identity_fixture import verified_bridge
from control.app import main
from host import mdd_orchestrator as host


class ProfileIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_sim_selection_is_tracked_and_later_profile_is_not_overwritten(self):
        for current, mode in (("new-card", "automatic"), ("new-card", "manual"), ("later-card", "automatic")):
            with self.subTest(current=current, mode=mode), \
                    patch.object(main.network_operations, "busy", return_value=False), \
                    patch.object(main, "_cellular_network_target", return_value=({}, {
                        "iccid": current, "cellular_network_mode": mode,
                        "cellular_operator_id": "00101" if mode == "manual" else ""}, "modem")), \
                    patch.object(main, "api_device_cellular_network_select", new=AsyncMock()) as select:
                await main._esim_restore_cellular_selection("modem-a", "new-card")
            if current == "new-card":
                select.assert_awaited_once_with("modem-a", {"mode": mode,
                    "operator_id": "00101" if mode == "manual" else ""}, background=True)
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
    def test_only_target_with_stale_sim_is_reset_and_returning_usb_is_awaited(self):
        with tempfile.TemporaryDirectory() as temp:
            app = host.Orchestrator(Path(temp), Path(temp))
            old, other = Mock(), Mock()
            old.poll.return_value = other.poll.return_value = None
            app.bridges = {"modem-a": old, "modem-b": other}
            host.atomic_json(app.hw_state_path, {"assignments": {"modem-a": {"tty": "/dev/ttyUSB2"}}})
            host.atomic_json(app.bridge_restart_request_dir / "fixture.json", {
                "request_id": "fixture", "device_id": "modem-a",
                "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest()})
            with patch.object(app, "modem_snapshot", return_value={
                    "sim_iccid": "old-card", "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}), \
                    patch.object(host, "run", return_value=SimpleNamespace(
                        returncode=0, stdout="modem.generic.device: /sys/devices/fixture\n"
                        "modem.generic.primary-port: cdc-wdm9\nmodem.generic.primary-sim-slot: 1")) as run:
                app.process_bridge_restart_requests()
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/9", "--output-keyvalue"],
                ["timeout", "25s", "qmicli", "--device-open-proxy", "-d", "/dev/cdc-wdm9", "--uim-sim-power-off=1"],
                ["timeout", "25s", "qmicli", "--device-open-proxy", "-d", "/dev/cdc-wdm9", "--uim-sim-power-on=1"]])
            other.terminate.assert_not_called()
            app.finish_bridge_restart_requests(set())
            self.assertEqual(app._bridge_restarts["fixture"]["state"], "stopped")
            app.bridges["modem-a"] = SimpleNamespace(pid=22, poll=lambda: None)
            host.atomic_json(app.data / "modems" / "modem-a.json", {
                **verified_bridge(), "bridge_pid": 22, "iccid": "new-card",
                "channel_allocated": 3})
            app.cellular_states["modem-a"] = {"sim_iccid": "old-card"}
            app.finish_bridge_restart_requests({"modem-a"})
            self.assertEqual(app._bridge_restarts["fixture"]["state"], "spawned")
            app.cellular_states["modem-a"] = {"sim_iccid": "new-card"}
            app.finish_bridge_restart_requests({"modem-a"})
            self.assertEqual(app._bridge_restarts["fixture"]["state"], "channels_ready")

    def test_matching_sim_is_not_reset_and_failure_is_explicit(self):
        for current, code in (("new-card", 0), ("old-card", 1)):
            with self.subTest(current=current), tempfile.TemporaryDirectory() as temp:
                app = host.Orchestrator(Path(temp), Path(temp))
                host.atomic_json(app.hw_state_path, {"assignments": {"modem-a": {"tty": "/dev/ttyUSB2"}}})
                host.atomic_json(app.bridge_restart_request_dir / "fixture.json", {
                    "request_id": "fixture", "device_id": "modem-a",
                    "expected_iccid_sha256": hashlib.sha256(b"new-card").hexdigest()})
                with patch.object(app, "modem_snapshot", return_value={
                        "sim_iccid": current, "mm_object": "/org/freedesktop/ModemManager1/Modem/9"}), \
                        patch.object(host, "run", side_effect=[
                            SimpleNamespace(returncode=0, stdout="modem.generic.device: /sys/devices/fixture\n"
                                            "modem.generic.primary-port: cdc-wdm9\nmodem.generic.primary-sim-slot: 1"),
                            SimpleNamespace(returncode=code), SimpleNamespace(returncode=code)]) as run:
                    app.process_bridge_restart_requests()
                if current == "new-card":
                    run.assert_not_called()
                else:
                    self.assertEqual(app._bridge_restarts["fixture"]["state"], "failed")
                    self.assertIn("--uim-sim-power-on=1", run.call_args.args[0])
