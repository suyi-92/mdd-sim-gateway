import tempfile
import unittest
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from control.app import device_state, main
from host import mdd_orchestrator, vpcd_modem_bridge
from host.mdd_orchestrator import Orchestrator


class BridgeRestartHandshakeTests(unittest.TestCase):
    def test_restart_completes_only_for_new_pid_ready_channels_and_target_iccid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root)
            old = Mock()
            old.poll.return_value = None
            other = Mock()
            other.poll.return_value = None
            app.bridges = {"modem-1": old, "modem-2": other}
            app.bridge_ports = {"modem-1": 15360, "modem-2": 15616}
            request_id = "switch-1"
            mdd_orchestrator.atomic_json(
                app.bridge_restart_request_dir / f"{request_id}.json", {
                    "request_id": request_id,
                    "device_id": "modem-1",
                    "expected_iccid_sha256": hashlib.sha256(
                        b"profile-target").hexdigest(),
                    "requested_at": 100,
                })

            app.process_bridge_restart_requests()

            old.terminate.assert_called_once()
            old.wait.assert_called_once_with(8)
            other.terminate.assert_not_called()
            self.assertNotIn("modem-1", app.bridges)
            status_path = app.bridge_restart_status_dir / f"{request_id}.json"
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "stopped")

            replacement = SimpleNamespace(pid=22, poll=lambda: None)
            app.bridges["modem-1"] = replacement
            identity_path = app.data / "modems" / "modem-1.json"
            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 11, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "spawned")

            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-old",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            self.assertEqual(mdd_orchestrator.read_json(status_path)["state"], "spawned")

            mdd_orchestrator.atomic_json(identity_path, {
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target",
            })
            app.finish_bridge_restart_requests({"modem-1", "modem-2"})
            status = mdd_orchestrator.read_json(status_path)
            self.assertEqual(status["state"], "channels_ready")
            self.assertEqual(status["bridge_pid"], 22)
            self.assertIs(app.bridges["modem-2"], other)

    def test_requests_for_two_modems_have_independent_status_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root)
            first, second = Mock(), Mock()
            first.poll.return_value = second.poll.return_value = None
            app.bridges = {"modem-1": first, "modem-2": second}
            for request_id, device_id in (("switch-1", "modem-1"),
                                          ("switch-2", "modem-2")):
                mdd_orchestrator.atomic_json(
                    app.bridge_restart_request_dir / f"{request_id}.json", {
                        "request_id": request_id, "device_id": device_id,
                        "expected_iccid_sha256": hashlib.sha256(
                            f"profile-{device_id[-1]}".encode()).hexdigest(),
                    })

            app.process_bridge_restart_requests()

            self.assertEqual(
                mdd_orchestrator.read_json(
                    app.bridge_restart_status_dir / "switch-1.json")["device_id"],
                "modem-1")
            self.assertEqual(
                mdd_orchestrator.read_json(
                    app.bridge_restart_status_dir / "switch-2.json")["device_id"],
                "modem-2")


class ESimProfileSwitchControlTests(unittest.IsolatedAsyncioTestCase):
    def test_modem_reader_list_ignores_the_driver_spare_slot(self):
        readers = [
            "VoWiFi Modem modem-1 00 00",
            "VoWiFi Modem modem-1 00 01",
            "VoWiFi Modem modem-1 00 02",
            "VoWiFi Modem modem-1 00 03",
        ]
        identity = {
            "hardware_id": "modem-1", "channel_allocated": 3,
            "logical_channels": [
                {"slot": 0, "channel": 1, "role": "pin"},
                {"slot": 1, "channel": 2, "role": "swu"},
                {"slot": 2, "channel": 3, "role": "ims"},
            ],
        }
        with patch.object(main, "_modem_identity_for_reader", return_value=identity), \
                patch.object(main.sim, "list_readers", return_value=readers), \
                patch.object(main.hub, "cards", {}):
            active = main._esim_modem_reader_names(readers[0], "modem-1")

        self.assertEqual(active, readers[:3])
        self.assertEqual(device_state.vpcd_modem_reader_slot(readers[2]), 2)
        self.assertEqual(device_state.vpcd_modem_reader_slot("SCR Prime 00 00"), None)

    def test_legacy_line_is_scoped_from_its_live_modem_match(self):
        reader = "VoWiFi Modem modem-1 00 00"
        with patch.object(main.hub, "cards", {
                reader: {"name": reader, "present": True, "matched": "legacy"}}):
            self.assertTrue(main._esim_instance_uses_modem(
                {"id": "legacy"}, "modem-1"))

    async def test_refresh_requires_every_virtual_reader_to_report_target_iccid(self):
        readers = [
            "VoWiFi Modem modem-1 00 00",
            "VoWiFi Modem modem-1 00 01",
            "VoWiFi Modem modem-1 00 02",
        ]
        target = SimpleNamespace(
            iccid="profile-target", imsi="imsi-target", mcc="234", mnc="15",
            mnc_len=2, pin_enabled=False, pin_tries=3, smsc="",
            carrier_identity={})
        cards = {reader: {"name": reader, "index": index, "present": True,
                          "iccid": "profile-old"}
                 for index, reader in enumerate(readers)}
        instance = {"id": "2", "iccid": "profile-target"}
        with patch.object(main.sim, "list_readers", return_value=readers), \
                patch.object(main.sim, "read_card", return_value=target) as full_read, \
                patch.object(main.sim, "read_iccid",
                             return_value="profile-target") as iccid_read, \
                patch.object(main, "_match_instance_by_iccid", return_value=instance), \
                patch.object(main, "_carrier_identity_update", return_value={}), \
                patch.object(main.hub, "cards", cards), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            info, refreshed = await main._esim_refresh_modem_readers(
                readers[0], "modem-1", "profile-target")

        self.assertEqual(info["iccid"], "profile-target")
        self.assertEqual(refreshed, readers)
        self.assertEqual({card["iccid"] for card in cards.values()}, {"profile-target"})
        full_read.assert_called_once_with(0)
        self.assertEqual([call.args[0] for call in iccid_read.call_args_list], [1, 2])

    async def test_refresh_does_not_probe_the_unbacked_driver_spare(self):
        readers = [
            "VoWiFi Modem modem-1 00 00",
            "VoWiFi Modem modem-1 00 01",
            "VoWiFi Modem modem-1 00 02",
            "VoWiFi Modem modem-1 00 03",
        ]
        target = SimpleNamespace(
            iccid="profile-target", imsi="imsi-target", mcc="234", mnc="15",
            mnc_len=2, pin_enabled=False, pin_tries=3, smsc="",
            carrier_identity={})
        cards = {reader: {"name": reader, "index": index, "present": index < 3,
                          "iccid": "profile-old" if index < 3 else None}
                 for index, reader in enumerate(readers)}
        identity = {
            "hardware_id": "modem-1", "channel_allocated": 3,
            "logical_channels": [{"slot": slot, "channel": slot + 1}
                                 for slot in range(3)],
        }
        instance = {"id": "2", "iccid": "profile-target"}

        def read_card(index):
            self.assertLess(index, 3, "the unbacked 00 03 reader must never be probed")
            return target

        def read_iccid(index):
            self.assertLess(index, 3, "the unbacked 00 03 reader must never be probed")
            return "profile-target"

        with patch.object(main, "_modem_identity_for_reader", return_value=identity), \
                patch.object(main.sim, "list_readers", return_value=readers), \
                patch.object(main.sim, "read_card", side_effect=read_card), \
                patch.object(main.sim, "read_iccid", side_effect=read_iccid), \
                patch.object(main, "_match_instance_by_iccid", return_value=instance), \
                patch.object(main, "_carrier_identity_update", return_value={}), \
                patch.object(main.hub, "cards", cards), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            _info, refreshed = await main._esim_refresh_modem_readers(
                readers[0], "modem-1", "profile-target")

        self.assertEqual(refreshed, readers[:3])
        self.assertIsNone(cards[readers[3]]["iccid"])

    def test_target_egress_is_prewarmed_without_persisting_enabled_state(self):
        target = {"id": "2", "iccid": "profile-target", "enabled": False,
                  "provisioning_state": "ready"}
        other = {"id": "3", "iccid": "profile-other", "enabled": False}
        with patch.object(main, "_match_instance_by_iccid", return_value=target), \
                patch.object(main.cfg, "list_instances", return_value=[target, other]), \
                patch.object(main.egress, "publish") as publish:
            self.assertTrue(main._esim_prewarm_target_egress("profile-target"))

        preview = publish.call_args.kwargs["instances"]
        self.assertTrue(preview[0]["enabled"])
        self.assertFalse(preview[1]["enabled"])
        self.assertFalse(target["enabled"])

    async def test_post_switch_line_retries_a_transient_country_exit(self):
        target = {"id": "2", "iccid": "profile-target", "enabled": True}
        error = main.HTTPException(503, {
            "code": "egress_unavailable", "message": "country exit is starting"})
        events = AsyncMock()
        with patch.object(main.cfg, "get_instance", return_value=target), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main, "api_instance_start",
                             new=AsyncMock(side_effect=[error, {"ok": True}])) as start, \
                patch.object(main, "_esim_profile_event", new=events), \
                patch.object(main, "_record_lifecycle"), \
                patch.object(main, "ESIM_LINE_RECOVERY_RETRY_DELAY", 0), \
                patch.object(main.hub, "cards", {
                    "reader": {"present": True, "iccid": "profile-target"},
                }), patch.object(main.hub, "esim_line_recoveries", set()):
            await main._esim_start_profile_line(
                "reader", "modem-1", "profile-target", "2")

        self.assertEqual(start.await_count, 2)
        self.assertEqual(
            [call.args[2] for call in events.await_args_list],
            ["recovery_retry", "line_started"])

    async def test_verified_pin_disabled_switch_skips_the_duplicate_full_card_preflight(self):
        target = {"id": "2", "iccid": "profile-target", "enabled": True,
                  "reader_index": 1}
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()
            return Mock()

        preflight = AsyncMock(return_value={"ok": True})
        with patch.object(main.cfg, "get_instance", return_value=target), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main, "_card_identity_mismatch", return_value=None), \
                patch.object(main, "_preflight_pin", new=preflight), \
                patch.object(main, "_reader_port_for_instance", return_value=None), \
                patch.object(main, "_reader_index_for_instance", return_value=1), \
                patch.object(main, "_start_engine_checked", return_value="container") as start, \
                patch.object(main.hub, "cards", {
                    "reader": {"present": True, "iccid": "profile-target",
                               "pin_enabled": False},
                }), patch.object(main.hub, "reset_health"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.asyncio, "create_task", side_effect=capture):
            result = await main._start_instance(
                "2", pin_preflight_verified=True,
                health_reason="esim_profile_switch",
                engine_reason="esim_profile_switch")

        self.assertTrue(result["ok"])
        preflight.assert_not_awaited()
        self.assertEqual(start.call_args.kwargs["reason"], "esim_profile_switch")
        self.assertEqual(len(scheduled), 1)

    async def test_changed_or_pin_locked_card_does_not_reuse_the_switch_preflight(self):
        target = {"id": "2", "iccid": "profile-target", "enabled": True,
                  "reader_index": 1}
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()
            return Mock()

        preflight = AsyncMock(return_value={"ok": True})
        with patch.object(main.cfg, "get_instance", return_value=target), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main, "_card_identity_mismatch", return_value=None), \
                patch.object(main, "_preflight_pin", new=preflight), \
                patch.object(main, "_reader_port_for_instance", return_value=None), \
                patch.object(main, "_reader_index_for_instance", return_value=1), \
                patch.object(main, "_start_engine_checked", return_value="container"), \
                patch.object(main.hub, "cards", {
                    "reader": {"present": True, "iccid": "profile-target",
                               "pin_enabled": True},
                }), patch.object(main.hub, "reset_health"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.asyncio, "create_task", side_effect=capture):
            await main._start_instance("2", pin_preflight_verified=True)

        preflight.assert_awaited_once_with(target)
        self.assertEqual(len(scheduled), 1)

    def test_processed_notification_is_removed_from_only_the_selected_cached_se(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(main, "_ESIM_CACHE_PATH", str(Path(temp, "esim-cache.json"))), \
                patch.object(main.time, "time", return_value=200):
            main._esim_cache_write({
                "eid-one": {"ts": 100, "ses": [
                    {"id": "first", "profiles": [{"iccid": "profile-target"}],
                     "notifications": [{"seqNumber": 73}, {"seqNumber": 74}]},
                    {"id": "second", "profiles": [{"iccid": "profile-other"}],
                     "notifications": [{"seqNumber": 75}]},
                ]},
                "eid-two": {"ts": 100, "ses": [
                    {"id": "first", "profiles": [{"iccid": "profile-unrelated"}],
                     "notifications": [{"seqNumber": 76}]},
                ]},
            })

            self.assertTrue(main._esim_cache_remove_notifications(
                "profile-target", "first"))
            cached = main._esim_cache_load()

        self.assertEqual(cached["eid-one"]["ses"][0]["notifications"], [])
        self.assertEqual(
            cached["eid-one"]["ses"][1]["notifications"], [{"seqNumber": 75}])
        self.assertEqual(
            cached["eid-two"]["ses"][0]["notifications"], [{"seqNumber": 76}])
        self.assertEqual(cached["eid-one"]["ts"], 200)

    def test_chip_cache_never_persists_transient_notifications(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(main, "_ESIM_CACHE_PATH", str(Path(temp, "esim-cache.json"))):
            main._esim_cache_store([{
                "id": "default", "eid": "eid-one",
                "profiles": [{"iccid": "profile-target"}],
                "notifications": [{"seqNumber": 73}],
            }], "")
            cached = main._esim_cache_load()

        self.assertEqual(cached["eid-one"]["ses"][0]["notifications"], [])

    async def test_legacy_cached_notifications_are_not_reported_as_live(self):
        cached = {"ts": 100, "imei": "", "ses": [{
            "id": "default", "profiles": [{"iccid": "profile-target"}],
            "notifications": [{"seqNumber": 73}],
        }]}
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main.hub, "cards", {
                    "reader": {"present": True, "iccid": "profile-target"},
                }), patch.object(main, "_esim_cache_for_iccid", return_value=cached):
            result = await main.api_esim_chip_cached(reader="reader")

        self.assertTrue(result["cached"])
        self.assertEqual(result["ses"][0]["notifications"], [])
        self.assertEqual(cached["ses"][0]["notifications"], [{"seqNumber": 73}])

    async def test_prepare_and_restore_preserve_the_exact_running_snapshot(self):
        lines = {
            "1": {"id": "1", "enabled": True, "imei_source_device_id": "modem-1"},
            "2": {"id": "2", "enabled": True, "imei_source_device_id": "modem-1"},
            "3": {"id": "3", "enabled": True, "imei_source_device_id": "modem-2"},
        }

        def get_instance(iid):
            return dict(lines[str(iid)])

        def upsert(value):
            iid = str(value["id"])
            lines[iid].update(value)
            return dict(lines[iid])

        with patch.object(main.cfg, "list_instances",
                          side_effect=lambda: [dict(line) for line in lines.values()]), \
                patch.object(main.cfg, "get_instance", side_effect=get_instance), \
                patch.object(main.cfg, "upsert_instance", side_effect=upsert), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "is_running",
                             side_effect=lambda iid: str(iid) == "1"), \
                patch.object(main.engine, "stop") as stop, \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.egress, "publish"):
            previous = await main._esim_prepare_profile_switch("modem-1")
            self.assertFalse(lines["1"]["enabled"])
            self.assertFalse(lines["2"]["enabled"])
            self.assertTrue(lines["3"]["enabled"])
            await main._esim_restore_profile_switch(previous)

        stop.assert_called_once_with("1")
        self.assertTrue(lines["1"]["enabled"])
        self.assertTrue(lines["2"]["enabled"])
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["id"], "1")

    async def test_successful_lpa_with_failed_recovery_reports_the_switch(self):
        """Recovery failure after a successful enable must not read as a failed switch.

        The eUICC already runs the new profile; lines stay fail-closed, but the response
        says the switch happened so the UI can show the true profile state (issue #26).
        """
        error = main.HTTPException(503, "bridge failed")
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("modem-1", "modem-1")), \
                patch.object(main, "_esim_prepare_profile_switch",
                             new=AsyncMock(return_value={"1": {"enabled": True,
                                                                "running": True}})), \
                patch.object(main, "_esim_modem_reader_names", return_value=["reader"]), \
                patch.object(main, "_esim_prewarm_target_egress", return_value=True), \
                patch.object(main, "_esim_profile_event", new=AsyncMock()), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": "a"}), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock()), \
                patch.object(main, "_esim_cache_update_profile"), \
                patch.object(main, "_esim_recover_profile_switch",
                             new=AsyncMock(side_effect=error)), \
                patch.object(main.egress, "publish"), \
                patch.object(main, "_esim_restore_profile_switch",
                             new=AsyncMock()) as restore:
            result = await main.api_esim_enable("profile-target", {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["iccid"], "profile-target")
        self.assertEqual(result["recovery_error"], "bridge failed")
        restore.assert_not_awaited()
        self.assertNotIn("reader", main.hub.lpa_busy)

    async def test_successful_modem_switch_returns_while_line_starts_in_background(self):
        recovery = {
            "card": {"iccid": "profile-target"},
            "readers": ["reader"],
            "instance_id": "2",
            "bridge": {"state": "channels_ready"},
        }
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()
            return Mock()

        events = AsyncMock()
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("modem-1", "modem-1")), \
                patch.object(main, "_esim_prepare_profile_switch",
                             new=AsyncMock(return_value={})), \
                patch.object(main, "_esim_modem_reader_names", return_value=["reader"]), \
                patch.object(main, "_esim_prewarm_target_egress", return_value=True), \
                patch.object(main, "_esim_profile_event", new=events), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": "a"}), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock()), \
                patch.object(main, "_esim_cache_update_profile"), \
                patch.object(main, "_esim_recover_profile_switch",
                             new=AsyncMock(return_value=recovery)), \
                patch.object(main.asyncio, "create_task", side_effect=capture):
            result = await main.api_esim_enable("profile-target", {})

        self.assertTrue(result["ok"])
        self.assertTrue(result["recovery_pending"])
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(
            [call.args[2] for call in events.await_args_list],
            ["switching", "enabled"])
        self.assertNotIn("reader", main.hub.lpa_busy)

    async def test_post_switch_probe_retries_through_the_refresh_window(self):
        old = SimpleNamespace(iccid="profile-old")
        new = SimpleNamespace(iccid="profile-new")
        reads = [RuntimeError("card is resetting"), old, new]

        def read_card(_idx):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(main.sim, "read_card", side_effect=read_card), \
                patch.object(main, "ESIM_CARD_REFRESH_INTERVAL", 0):
            result = await main._esim_probe_card(
                0, expect_iccid="profile-new", attempts=5)
        self.assertEqual(result.iccid, "profile-new")

    async def test_post_switch_probe_reports_the_card_truth_when_attempts_run_out(self):
        old = SimpleNamespace(iccid="profile-old")
        with patch.object(main.sim, "read_card", return_value=old), \
                patch.object(main, "ESIM_CARD_REFRESH_INTERVAL", 0):
            result = await main._esim_probe_card(
                0, expect_iccid="profile-new", attempts=3)
        self.assertEqual(result.iccid, "profile-old")

    async def test_native_reader_switch_disables_the_old_profiles_line(self):
        lines = {"7": {"id": "7", "enabled": True}}

        def upsert(value):
            lines[str(value["id"])].update(value)
            return dict(lines[str(value["id"])])

        with patch.object(main.hub, "cards",
                          {"reader": {"name": "reader", "matched": "7"}}), \
                patch.object(main.cfg, "get_instance",
                             side_effect=lambda iid: dict(lines[str(iid)])), \
                patch.object(main.cfg, "upsert_instance", side_effect=upsert), \
                patch.object(main.hub, "reset_health"), \
                patch.object(main.egress, "publish"):
            previous = await main._esim_prepare_reader_profile_switch("reader")

        self.assertFalse(lines["7"]["enabled"])
        self.assertEqual(previous, {"7": {"enabled": True, "running": False}})

    async def test_native_reader_lpa_failure_restores_the_old_line(self):
        previous = {"7": {"enabled": True, "running": False}}
        error = main.HTTPException(400, "lpac failed")
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("reader:reader", "")), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": None}), \
                patch.object(main, "_esim_prepare_reader_profile_switch",
                             new=AsyncMock(return_value=previous)), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock(side_effect=error)), \
                patch.object(main, "_esim_restore_profile_switch",
                             new=AsyncMock()) as restore:
            with self.assertRaises(main.HTTPException):
                await main.api_esim_enable("profile-target", {})

        restore.assert_awaited_once_with(previous)

    async def test_lpa_failure_restores_previous_line_snapshot(self):
        previous = {"1": {"enabled": True, "running": True}}
        error = main.HTTPException(400, "lpac failed")
        with patch.object(main, "_esim_resolve_reader", return_value=("reader", 0)), \
                patch.object(main, "_esim_switch_identity",
                             return_value=("modem-1", "modem-1")), \
                patch.object(main, "_esim_prepare_profile_switch",
                             new=AsyncMock(return_value=previous)), \
                patch.object(main, "_esim_modem_reader_names", return_value=["reader"]), \
                patch.object(main, "_esim_prewarm_target_egress", return_value=True), \
                patch.object(main, "_esim_profile_event", new=AsyncMock()), \
                patch.object(main, "_esim_resolve_se", return_value={"id": "se", "aid": "a"}), \
                patch.object(main.lpa, "profile_enable", new=lambda *_a, **_k: object()), \
                patch.object(main, "_esim_run", new=AsyncMock(side_effect=error)), \
                patch.object(main, "_esim_restore_profile_switch",
                             new=AsyncMock()) as restore:
            with self.assertRaises(main.HTTPException):
                await main.api_esim_enable("profile-target", {})

        restore.assert_awaited_once_with(previous)
        self.assertNotIn("reader", main.hub.lpa_busy)


def _encode_iccid(iccid: str) -> bytes:
    digits = iccid + "F" * (len(iccid) % 2)
    return bytes(int(digits[i], 16) | (int(digits[i + 1], 16) << 4)
                 for i in range(0, len(digits), 2))


class BridgeIdentityTests(unittest.TestCase):
    """The bridge must publish the card's ICCID, not the baseband's cached one.

    An eSIM profile switch happens over AT+CSIM behind the baseband, whose AT+CCID cache
    then keeps the previous profile until a SIM re-init — which made every post-switch
    bridge rebuild verification time out (issue #26).
    """

    CARD_ICCID = "8900000000000000001"
    STALE_AT_ICCID = "8900000000000000002"

    def _card(self, csim_responses):
        card = object.__new__(vpcd_modem_bridge.ModemCard)
        card.debug = False
        card.calls = []

        def csim(apdu):
            card.calls.append(apdu.hex().upper())
            response = csim_responses.get(apdu.hex().upper())
            if response is None:
                raise vpcd_modem_bridge.ModemError("unexpected APDU")
            return response

        def _at(command):
            if command in ("AT+CGSN", "AT+GSN"):
                return b"123456789012345\r\nOK"
            if command in ("AT+CCID", "AT+ICCID"):
                return b'+QCCID: %s\r\nOK' % self.STALE_AT_ICCID.encode()
            raise vpcd_modem_bridge.ModemError("unexpected AT command")

        card.csim = csim
        card._at = _at
        return card

    def test_decode_bcd_iccid(self):
        self.assertEqual(
            vpcd_modem_bridge.decode_bcd_iccid(_encode_iccid(self.CARD_ICCID)),
            self.CARD_ICCID)
        self.assertEqual(vpcd_modem_bridge.decode_bcd_iccid(b"\xab" * 10), "")

    def test_identity_prefers_the_card_over_the_baseband_cache(self):
        card = self._card({
            "00A4080C022FE2": b"\x90\x00",
            "00B000000A": _encode_iccid(self.CARD_ICCID) + b"\x90\x00",
            "00A40004023F00": b"\x90\x00",
        })
        identity = card.identity()
        self.assertEqual(identity["iccid"], self.CARD_ICCID)
        self.assertEqual(identity["imei"], "123456789012345")
        # The basic-channel file state is restored for the baseband afterwards.
        self.assertEqual(card.calls[-1], "00A40004023F00")

    def test_identity_falls_back_to_at_when_the_card_read_fails(self):
        card = self._card({})
        identity = card.identity()
        self.assertEqual(identity["iccid"], self.STALE_AT_ICCID)

    def test_incomplete_refresh_keeps_verified_hardware_imei_only(self):
        previous = {"imei": "123456789012345", "iccid": self.CARD_ICCID}
        refreshed = vpcd_modem_bridge.retain_hardware_identity(
            previous, {"imei": "", "iccid": ""})

        self.assertEqual(refreshed["imei"], previous["imei"])
        # A blank ICCID is meaningful evidence of removal, unlike a blank IMEI read.
        self.assertEqual(refreshed["iccid"], "")

    def test_complete_refresh_replaces_the_previous_identity(self):
        refreshed = vpcd_modem_bridge.retain_hardware_identity(
            {"imei": "123456789012345", "iccid": self.CARD_ICCID},
            {"imei": "350000000000018", "iccid": self.STALE_AT_ICCID})

        self.assertEqual(refreshed, {
            "imei": "350000000000018", "iccid": self.STALE_AT_ICCID})


if __name__ == "__main__":
    unittest.main()
