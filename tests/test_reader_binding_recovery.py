"""A migrated native reader must survive an unrelated modem's eSIM switch."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from control.app import config, main


TARGET = "8944110000000000000"
NATIVE = "8944110000000000001"
OLD = "8944110000000000002"
CHANNELS = ("pin_reader", "swu_reader", "ami_reader")


class ReaderBindingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "modems").mkdir()
        self.old_names = [f"VoWiFi Modem old-modem 00 {slot:02d}" for slot in range(3)]
        self.names = [f"VoWiFi Modem current-modem 00 {slot:02d}" for slot in range(3)]
        self.native_name = "SCR Prime CCID Reader (fixture-native) 00 00"
        self.readers = [*self.names, self.native_name]
        for hardware, iccid in (("old-modem", OLD), ("current-modem", TARGET)):
            (self.root / "modems" / f"{hardware}.json").write_text(json.dumps({
                "hardware_id": hardware, "iccid": iccid, "imei": "490154203237518", "slots": 3,
            }))
        self.target = {"id": "target", "iccid": TARGET, "enabled": True,
                       "reader_index": 1, "reader_port": "", "imei_source_device_id": "old-modem",
                       **dict(zip(CHANNELS, self.old_names))}
        self.native = {"id": "native", "iccid": NATIVE, "enabled": True,
                       "reader_index": 0, "reader_port": "3-2", "imei_source_device_id": "reader-native",
                       **dict(zip(CHANNELS, self.names))}
        self.old = {"id": "old", "iccid": OLD, "enabled": True,
                    "reader_index": 1, "reader_port": "", "imei_source_device_id": "current-modem",
                    **dict(zip(CHANNELS, self.names))}
        self.instances = {item["id"]: copy.deepcopy(item) for item in (self.native, self.target, self.old)}
        self.cards = {name: {"name": name, "index": index, "present": True,
                             "iccid": TARGET, "reader_port": ""}
                      for index, name in enumerate(self.names)}
        self.cards[self.native_name] = {"name": self.native_name, "index": 3, "present": True,
                                       "iccid": NATIVE, "reader_port": "3-2"}
        for patcher in (
            patch.object(config, "DATA_DIR", str(self.root)),
            patch.object(config, "get_settings", return_value={}),
            patch.object(main.hub, "cards", self.cards),
            patch.object(main.hub, "esim_switch_locks", {}),
            patch.object(main.hub, "esim_line_recoveries", set()),
            patch.object(main.sim, "list_readers", side_effect=lambda: self.readers[:]),
            patch.object(config, "list_instances", side_effect=lambda: list(self.instances.values())),
            patch.object(config, "get_instance", side_effect=lambda iid: copy.deepcopy(self.instances.get(str(iid)))),
            patch.object(config, "upsert_instance", side_effect=self.save),
            patch.object(main.hub, "broadcast", new=AsyncMock()),
            patch.object(main.hub, "drop_ami", new=AsyncMock()),
            patch.object(main.hub, "reset_health"),
            patch.object(main.egress, "publish"),
            patch.object(main, "push_status", new=AsyncMock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, update, **options):
        inst = self.instances[str(update["id"])]
        inst.update(update)
        if options.get("clear_modem_readers"):
            for key in CHANNELS:
                inst.pop(key, None)
        return copy.deepcopy(inst)

    async def test_switch_stops_only_target_modem_and_leaves_migrated_native_running(self):
        with patch.object(main.engine, "is_running", side_effect=lambda iid: iid in {"native", "old"}), \
                patch.object(main.engine, "stop") as stop:
            previous = await main._esim_prepare_profile_switch("current-modem")
        self.assertNotIn("native", previous)
        self.assertEqual([call.args[0] for call in stop.call_args_list], ["old"])
        self.assertTrue(self.instances["native"]["enabled"])

    async def test_manual_start_refreshes_modem_binding_before_the_real_identity_guard(self):
        with patch.object(main, "_preflight_pin", new=AsyncMock(return_value={"ok": True})), \
                patch.object(main, "_start_engine_checked", return_value="fixture-container") as start:
            result = await main.api_instance_start("target")
        self.assertEqual(result["container"], "fixture-container")
        actual = start.call_args.args[0]
        self.assertEqual([actual[key] for key in CHANNELS], self.names)
        self.assertEqual(actual["imei_source_device_id"], "current-modem")

    async def test_native_start_drops_generated_modem_overrides_before_pin_preflight(self):
        with patch.object(main, "_preflight_pin", new=AsyncMock(return_value={"ok": True})) as preflight, \
                patch.object(main, "_start_engine_checked", return_value="fixture-container"), \
                patch.object(main.usbreader, "index_for_port", return_value=3):
            await main.api_instance_start("native")
        actual = preflight.call_args.args[0]
        self.assertFalse(any(key in actual for key in CHANNELS))
        self.assertEqual(actual["reader_port"], "3-2")
        self.assertEqual(actual["reader_index"], 3)

    async def test_reprovision_also_rebinds_before_identity_checks(self):
        with patch.object(main, "_preflight_pin", new=AsyncMock(return_value={"ok": True})), \
                patch.object(main, "_start_engine_checked", return_value="fixture-container") as start:
            await main.api_reprovision("target")
        self.assertEqual(start.call_args.args[0]["swu_reader"], self.names[1])

    def test_absent_old_modem_metadata_cannot_assert_a_current_card_conflict(self):
        self.assertIsNone(main._card_identity_mismatch(self.target))

    def test_live_wrong_card_is_still_rejected(self):
        mismatch = main._card_identity_mismatch(self.old)
        self.assertEqual(mismatch["iccid"], TARGET)

    def test_unknown_native_identity_still_does_not_join_a_modem_switch(self):
        self.cards[self.native_name]["iccid"] = None
        self.assertFalse(main._esim_instance_uses_modem(self.native, "current-modem"))

    async def test_ambiguous_identity_aborts_switch_before_any_line_is_stopped(self):
        duplicate = "SCR Prime CCID Reader (fixture-other) 00 00"
        self.cards[duplicate] = {"name": duplicate, "present": True, "iccid": TARGET}
        before = copy.deepcopy(self.instances)
        with patch.object(main.engine, "stop") as stop, self.assertRaises(HTTPException) as error:
            await main._esim_prepare_profile_switch("current-modem")
        self.assertEqual(error.exception.status_code, 409)
        stop.assert_not_called()
        self.assertEqual(self.instances, before)

    async def test_duplicate_identity_on_native_and_modem_never_selects_a_reader(self):
        self.cards[self.native_name]["iccid"] = TARGET
        before = copy.deepcopy(self.instances)
        with patch.object(main, "_preflight_pin", new=AsyncMock()) as pin, \
                self.assertRaises(HTTPException) as error:
            await main.api_instance_start("target")
        self.assertEqual(error.exception.status_code, 409)
        pin.assert_not_awaited()
        self.assertEqual(self.instances, before)

    def test_readonly_identity_check_does_not_persist_repaired_binding(self):
        before = copy.deepcopy(self.instances)
        self.assertIsNone(main._card_identity_mismatch(self.target))
        self.assertEqual(self.instances, before)

    async def test_native_hotplug_cleans_old_channels_even_when_port_and_index_already_match(self):
        self.instances["native"]["reader_index"] = 3
        card = SimpleNamespace(iccid=NATIVE, imsi="001010000000001", mcc="001", mnc="01",
                               pin_enabled=False, pin_tries=3, smsc="", carrier_identity={})
        with patch.object(main.usbreader, "port_for_index", return_value="3-2"), \
                patch.object(main, "_find_running_by_reader", return_value=None), \
                patch.object(main.sim, "read_card", return_value=card), \
                patch.object(main, "_carrier_identity_update", return_value={}), \
                patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()):
            await main._on_card_insert(self.native_name, 3)
        self.assertFalse(any(key in self.instances["native"] for key in CHANNELS))
        self.assertEqual(self.instances["native"]["reader_index"], 3)
        self.assertEqual(self.instances["native"]["reader_port"], "3-2")

    async def test_post_switch_recovery_refreshes_binding_and_automatically_starts_target(self):
        card = {**self.cards[self.names[0]], "pin_enabled": False}
        with patch.object(main, "_esim_restart_modem_bridge", new=AsyncMock(return_value={"state": "channels_ready"})), \
                patch.object(main, "_esim_refresh_modem_readers", new=AsyncMock(return_value=(card, self.names))):
            recovered = await main._esim_recover_profile_switch(self.names[0], "current-modem", TARGET)
        self.assertEqual(recovered["instance_id"], "target")
        self.assertEqual(self.instances["target"]["swu_reader"], self.names[1])
        with patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main, "_preflight_pin", new=AsyncMock(return_value={"ok": True})), \
                patch.object(main, "_start_engine_checked", return_value="fixture-container") as start, \
                patch.object(main, "_record_lifecycle") as record:
            await main._esim_start_profile_line(self.names[0], "current-modem", TARGET, "target",
                                                pin_preflight_proof=recovered["pin_preflight_proof"])
        self.assertEqual(start.call_args.args[0]["swu_reader"], self.names[1])
        self.assertTrue(any(call.args[1] == "recovery_succeeded" for call in record.call_args_list))
        self.assertTrue(self.instances["native"]["enabled"])


class NativeBindingPersistenceTests(unittest.TestCase):
    def test_atomic_migration_clears_only_reader_channels_and_keeps_line_configuration(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(config, "DATA_DIR", temporary), \
                patch.object(config, "CONFIG_PATH", str(Path(temporary) / "config.yaml")):
            original = config.upsert_instance({
                "id": "1", "name": "fixture-native", "iccid": NATIVE,
                "imsi": "001010000000001", "mcc": "001", "mnc": "01",
                "ports": config._alloc_ports(0), "pin": "1234", "enabled": True,
                "proxy_country": "gb", "reader_index": 0, "reader_port": "3-2",
                **dict(zip(CHANNELS, [f"VoWiFi Modem fixture 00 {index:02d}" for index in range(3)])),
            })
            saved = config.upsert_instance({"id": "1", "reader_index": 3, "reader_port": "3-2"},
                                           clear_modem_readers=True)
            loaded = config.get_instance("1")
            self.assertEqual(saved, loaded)
            self.assertFalse(any(key in loaded for key in CHANNELS))
            for key in ("name", "iccid", "imsi", "pin", "ami_secret", "sip", "ports", "proxy_country", "enabled"):
                self.assertEqual(loaded[key], original[key], key)
            config.upsert_instance({"id": "1", "enabled": False})
            self.assertFalse(any(key in config.get_instance("1") for key in CHANNELS))


if __name__ == "__main__":
    unittest.main()
