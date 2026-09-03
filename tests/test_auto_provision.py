import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import config, main


class AutoProvisionTests(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "id": "2", "name": "234-33", "provisioning_state": "draft",
            "enabled": False, "imsi": "234330123456789", "mcc": "234", "mnc": "33",
            "iccid": "8944110000000000000", "smsc": "+447700900000",
            "debug": {"asterisk": True, "charon": False},
        }
        self.card = {
            "present": True, "index": 4, "name": "VoWiFi Modem test 00 01",
            "hardware_kind": "modem", "hardware_id": "test", "reader_port": "",
            "imsi": self.draft["imsi"], "mcc": "234", "mnc": "33",
            "iccid": self.draft["iccid"], "smsc": self.draft["smsc"],
            "pin_enabled": False,
            "virtual_slots": [
                {"index": 3, "name": "slot 0"},
                {"index": 4, "name": "slot 1"},
                {"index": 5, "name": "slot 2"},
            ],
        }

    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_complete_draft_is_promoted_and_bound_to_modem_slots(
            self, hardware_imei, upsert, _publish):
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        upsert.side_effect = lambda value, **kwargs: value

        result = main._auto_promote_card_draft(self.draft, self.card, [self.card])

        self.assertEqual(result["provisioning_state"], "ready")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["reader_index"], 4)
        self.assertEqual(result["swu_reader"], "slot 1")
        self.assertEqual(result["imei_source_device_id"], "test")
        self.assertFalse(result["debug"]["asterisk"])
        self.assertEqual(len(result["imeisv"]), 16)

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_modem_draft_without_hardware_imei_stays_stopped(self, hardware_imei, upsert):
        hardware_imei.return_value = ("", "modem-test", "modem")

        result = main._auto_promote_card_draft(self.draft, self.card, [self.card])

        self.assertEqual(result["provisioning_state"], "draft")
        self.assertFalse(result["enabled"])
        self.assertIn("IMEI", result["auto_provision_missing"])
        upsert.assert_not_called()

    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_native_reader_without_imei_or_smsc_still_promotes_for_vowifi(
            self, hardware_imei, upsert, _publish):
        hardware_imei.return_value = ("", "reader-test", "reader")
        draft = {**self.draft, "smsc": ""}
        card = {**self.card, "hardware_kind": "reader", "hardware_id": "reader-test",
                "virtual_slots": [], "smsc": ""}
        upsert.side_effect = lambda value, **kwargs: value

        result = main._auto_promote_card_draft(draft, card, [card])

        self.assertEqual(result["provisioning_state"], "ready")
        self.assertEqual(result["imei"], "")
        self.assertEqual(result["imeisv"], "")
        self.assertEqual(result["smsc"], "")
        self.assertNotIn("auto_provision_missing", result)

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_pin_locked_draft_waits_for_saved_pin(self, hardware_imei, upsert):
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        self.card["pin_enabled"] = True

        result = main._auto_promote_card_draft(self.draft, self.card, [self.card])

        self.assertIn("SIM PIN", result["auto_provision_missing"])
        upsert.assert_not_called()

    def test_ui_missing_keys_cover_every_field_that_blocks_a_draft(self):
        missing = main._draft_provisioning_missing(
            {"provisioning_state": "draft"}, {"pin_enabled": True}, "")
        self.assertEqual(missing, ["imsi", "mcc_mnc", "imei", "pin"])
        self.assertEqual(main._draft_provisioning_missing(
            {**self.draft, "pin": "0000"}, self.card, "490154203237518"), [])
        self.assertEqual(main._draft_provisioning_missing(
            {"provisioning_state": "draft"}, {"pin_enabled": False}, "", "reader"),
            ["imsi", "mcc_mnc"])

    def test_non_blocking_reader_warnings_are_reported_separately(self):
        warnings = main._provisioning_warnings(
            {"smsc": ""}, {"smsc": ""}, "", "reader")
        self.assertEqual(warnings, ["outbound_sms_disabled", "device_identity_omitted"])

    def test_start_on_identityless_reader_clears_a_stale_modem_identity(self):
        inst = {"id": "2", "iccid": "test-card", "imei": "490154203237518",
                "imeisv": "4901542032375100", "imei_source_device_id": "old-modem"}
        card = {"present": True, "iccid": "test-card", "hardware_kind": "reader"}
        cleared = {**inst, "imei": "", "imeisv": "",
                   "imei_source_device_id": "reader-test"}
        with patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_hardware_imei_for_card",
                             return_value=("", "reader-test", "reader")), \
                patch.object(main.cfg, "upsert_instance", return_value=cleared) as upsert:
            result = main._apply_current_hardware_imei(inst)

        self.assertEqual(result["imei"], "")
        upsert.assert_called_once_with({
            "id": "2", "imei": "", "imeisv": "",
            "imei_source_device_id": "reader-test",
        })

    def test_country_api_requests_and_releases_an_idle_test_runtime(self):
        settings = {"proxy": {"enabled": True,
                              "exits": {"gb": {"enabled": True}}}}
        live = {"updated_at": 1000, "exits": {"gb": {
            "ready": True, "proxy_host": "127.0.0.1", "proxy_port": 1080,
            "node": "Example node", "interface": "mdd-gb",
        }}}
        with patch.object(main.cfg, "get_settings", return_value=settings), \
                patch.object(main.egress, "publish") as publish, \
                patch.object(main.egress, "request_test",
                             return_value=("request-token", 1000.25)) as request, \
                patch.object(main.egress, "status", side_effect=[
                    {"updated_at": 999, "exits": {}}, live]), \
                patch.object(main.egress, "test_udp_proxy", return_value=42), \
                patch.object(main.egress, "finish_test", return_value=True) as finish:
            result = asyncio.run(main._test_egress_country("gb"))
        self.assertEqual(result["latency_ms"], 42)
        publish.assert_called_once()
        request.assert_called_once_with("gb")
        finish.assert_called_once_with("gb", "request-token")

    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_ready_disabled_line_is_never_promoted(self, hardware_imei, upsert):
        ready = {**self.draft, "provisioning_state": "ready", "enabled": False}

        result = main._auto_promote_card_draft(ready, self.card, [self.card])

        self.assertIs(result, ready)
        hardware_imei.assert_not_called()
        upsert.assert_not_called()

    def test_giffgaff_profile_rebuilds_required_sip_identity(self):
        first = config.carrier_sip_defaults("234", "10", "test-card")
        again = config.carrier_sip_defaults("234", "010", "test-card")

        self.assertEqual(first, again)
        self.assertEqual(first["access_type"], "wlan1")
        self.assertTrue(first["user_eq_phone"])
        self.assertIn("country=GB", first["pani"])
        self.assertNotIn("ffffffffffff", first["pani"])

    def test_cmlink_profile_formats_telephone_requests_for_ee_mvno_ims(self):
        profile = config.carrier_sip_defaults(
            "234", "33", "test-cmlink-card", {"spn": "CMLink"})

        self.assertEqual(profile["access_type"], "wlan1")
        self.assertTrue(profile["user_eq_phone"])
        self.assertIn("country=GB", profile["pani"])
        self.assertNotIn("ffffffffffff", profile["pani"])

    def test_generic_ee_profile_does_not_inherit_mvno_sip_flags(self):
        self.assertEqual(config.carrier_sip_defaults("234", "33", "test-ee-card"), {})

    def test_unknown_carrier_does_not_invent_sip_identity(self):
        self.assertEqual(config.carrier_sip_defaults("001", "01", "test-card"), {})

    def test_modem_group_prefers_present_slot_with_known_sim_identity(self):
        siblings = [
            {"index": 0, "name": "slot 0", "present": False, "iccid": None},
            {"index": 1, "name": "slot 1", "present": True,
             "iccid": "8944110000000000000"},
            {"index": 2, "name": "slot 2", "present": True, "iccid": None},
        ]

        selected = main._modem_card_representative(siblings)

        self.assertEqual(selected["name"], "slot 1")
        self.assertEqual(selected["iccid"], "8944110000000000000")

    @patch.object(main.sim, "list_readers")
    @patch.object(main, "_modem_identity_for_reader")
    def test_replugged_modem_rebuilds_all_saved_reader_bindings(self, identity, readers):
        identity.return_value = {"hardware_id": "2c7c-0125-4-1", "slots": 3}
        readers.return_value = [
            "VoWiFi Modem 2c7c-0125-2-1 00 00",
            "VoWiFi Modem 2c7c-0125-2-1 00 01",
            "VoWiFi Modem 2c7c-0125-2-1 00 02",
            "VoWiFi Modem 2c7c-0125-4-1 00 00",
            "VoWiFi Modem 2c7c-0125-4-1 00 01",
            "VoWiFi Modem 2c7c-0125-4-1 00 02",
        ]

        binding = main._modem_reader_binding(
            "VoWiFi Modem 2c7c-0125-4-1 00 01")

        self.assertEqual(binding, {
            "pin_reader": "VoWiFi Modem 2c7c-0125-4-1 00 00",
            "swu_reader": "VoWiFi Modem 2c7c-0125-4-1 00 01",
            "ami_reader": "VoWiFi Modem 2c7c-0125-4-1 00 02",
            "reader_index": 4,
            "reader_port": "",
            "imei_source_device_id": "2c7c-0125-4-1",
        })

    def test_engine_render_uses_carrier_profile_but_keeps_explicit_overrides(self):
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card",
            "imei": "490154203237518", "ami_secret": "test-secret",
            "sip": {"webrtc": {"enable": True, "password": "test-password"},
                    "access_type": "custom-access"},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["sip"]["access_type"], "custom-access")
        self.assertTrue(rendered["sip"]["user_eq_phone"])
        self.assertIn("country=GB", rendered["sip"]["pani"])

    def test_a_native_reader_gives_every_engine_role_the_line_s_own_slot(self):
        """A USB PC/SC reader has one slot. The rendered PIN/IMS readers used to be fixed at
        "0" and "2", so on a one-reader host ami_usim addressed a slot that does not exist and
        the SIM read as USIM = NO_CARD while IMS-AKA never ran (issue #8)."""
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card", "reader_index": 0,
            "imei": "490154203237518", "ami_secret": "test-secret",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["pin_reader"], "0")
        self.assertEqual(rendered["ami_reader"], "0")

    def test_a_native_reader_on_a_higher_index_is_followed_by_every_role(self):
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card", "reader_index": 3,
            "imei": "490154203237518", "ami_secret": "test-secret",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["pin_reader"], "3")
        self.assertEqual(rendered["ami_reader"], "3")

    def test_a_modem_line_keeps_its_dedicated_logical_slots(self):
        """A modem bridge really does expose three channels; the roles must stay apart."""
        base = {
            "id": "3", "index": 0, "imsi": "234100000000000",
            "mcc": "234", "mnc": "10", "iccid": "test-card", "reader_index": 4,
            "imei": "490154203237518", "ami_secret": "test-secret",
            "pin_reader": "VoWiFi Modem 2c7c-0125-4-1 00 00",
            "ami_reader": "VoWiFi Modem 2c7c-0125-4-1 00 02",
            "sip": {"webrtc": {"enable": True, "password": "test-password"}},
        }

        rendered = config.render_instance_json(base, {})

        self.assertEqual(rendered["pin_reader"], "VoWiFi Modem 2c7c-0125-4-1 00 00")
        self.assertEqual(rendered["ami_reader"], "VoWiFi Modem 2c7c-0125-4-1 00 02")

    def test_blank_sip_identity_fields_restore_carrier_defaults(self):
        merged = config.merge_carrier_sip_defaults("234", "10", "test-card", {
            "pani": "", "access_type": "", "user_eq_phone": False,
        })

        self.assertIn("country=GB", merged["pani"])
        self.assertEqual(merged["access_type"], "wlan1")
        self.assertFalse(merged["user_eq_phone"])

    @patch.object(main.egress, "publish")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main, "_hardware_imei_for_card")
    def test_giffgaff_draft_promotion_applies_carrier_profile(
            self, hardware_imei, upsert, _publish):
        draft = {**self.draft, "mcc": "234", "mnc": "10", "sip": {
            "listen_addr": "0.0.0.0", "transport": "udp"}}
        card = {**self.card, "mcc": "234", "mnc": "10"}
        hardware_imei.return_value = ("490154203237518", "test", "modem")
        upsert.side_effect = lambda value, **kwargs: value

        result = main._auto_promote_card_draft(draft, card, [card])

        self.assertEqual(result["sip"]["access_type"], "wlan1")
        self.assertTrue(result["sip"]["user_eq_phone"])
        self.assertIn("country=GB", result["sip"]["pani"])


class HotplugDraftPromotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_vowifi_still_promotes_complete_draft_without_starting_engine(self):
        draft = {
            "id": "2", "iccid": "test-card", "provisioning_state": "draft",
            "enabled": False,
        }
        card = {
            "present": True, "iccid": "test-card", "hardware_id": "test-modem",
            "hardware_kind": "modem",
        }
        promoted = {**draft, "provisioning_state": "ready", "enabled": True}
        desired = {"devices": {"test-modem": {"vowifi_enabled": False}}}

        with patch.object(main.asyncio, "sleep", new=AsyncMock()), \
                patch.object(main.cfg, "get_instance", return_value=draft), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main.device_state, "desired", return_value=desired), \
                patch.object(main, "_auto_promote_card_draft",
                             return_value=promoted) as promote, \
                patch.object(main, "_start_engine_checked") as start:
            await main._auto_start_hotplugged_line("2")

        promote.assert_called_once_with(draft, card, [card])
        start.assert_not_called()
        self.assertNotIn("2", main.hub.hotplug_starts)


class HardwareIdentityApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_native_reader_provision_does_not_require_imei_or_smsc(self):
        card = main.sim.CardInfo(
            reader="Reader", reader_index=0, reader_port="1-1", present=True,
            iccid="test-card", imsi="001010000000001", mcc="001", mnc="01",
            pin_enabled=False, pin_tries=3, smsc=None,
        )
        live = {"name": "Reader", "index": 0, "reader_port": "1-1",
                "present": True, "iccid": "test-card", "hardware_kind": "reader"}
        with patch.object(main, "_resolve_reader_index", return_value=0), \
                patch.object(main.sim, "list_readers", return_value=["Reader"]), \
                patch.object(main.sim, "read_card", return_value=card), \
                patch.object(main.hub, "reader_lock", return_value=asyncio.Lock()), \
                patch.object(main.hub, "cards_list", return_value=[live]), \
                patch.object(main, "_hardware_imei_for_card",
                             return_value=("", "reader-test", "reader")), \
                patch.object(main.cfg, "merge_carrier_sip_defaults",
                             return_value={"webrtc": {"enable": False}}), \
                patch.object(main.cfg, "list_instances", return_value=[]), \
                patch.object(main.cfg, "load", return_value={"instances": {}}), \
                patch.object(main.cfg, "alloc_ports_auto", return_value={"sip": 5060}), \
                patch.object(main.cfg, "upsert_instance",
                             side_effect=lambda value: value) as upsert, \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.hub, "reset_health"), \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main, "_refresh_card_matches"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_provision({"reader_index": 0, "webrtc": False})

        saved = upsert.call_args.args[0]
        self.assertEqual(saved["imei"], "")
        self.assertEqual(saved["imeisv"], "")
        self.assertEqual(saved["smsc"], "")
        self.assertTrue(result["ok"])
        start.assert_called_once()

    async def test_native_reader_identity_can_be_cleared_without_blocking_vowifi(self):
        device = {"id": "reader-test", "device_type": "reader", "name": "Reader",
                  "stable_path": "1-1", "instance_id": "2"}
        inst = {"id": "2", "imei": "490154203237518", "imeisv": "4901542032375100"}
        cleared = {**inst, "imei": "", "imeisv": "",
                   "imei_source_device_id": "reader-test"}
        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                patch.object(main.device_state, "set_hardware",
                             return_value={"imei": ""}) as set_hardware, \
                patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.cfg, "upsert_instance", return_value=cleared) as upsert, \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_device_hardware("reader-test", {"imei": ""})

        self.assertTrue(result["ok"])
        self.assertEqual(result["imei_masked"], "")
        set_hardware.assert_called_once_with("reader-test", {
            "device_type": "reader", "name": "Reader",
            "stable_path": "1-1", "imei": "",
        })
        upsert.assert_called_once_with({
            "id": "2", "imei": "", "imei_source_device_id": "reader-test",
            "imeisv": "",
        })


class ImsIdentityLearningTests(unittest.IsolatedAsyncioTestCase):
    def test_modemmanager_number_requires_ims_confirmation(self):
        self.assertTrue(main._needs_ims_msisdn_learning({
            "msisdn": "447000000000", "msisdn_source": "modemmanager"}))
        self.assertTrue(main._needs_ims_msisdn_learning({"msisdn": ""}))
        self.assertFalse(main._needs_ims_msisdn_learning({
            "msisdn": "+447000000001", "msisdn_source": "ims"}))
        self.assertFalse(main._needs_ims_msisdn_learning({
            "msisdn": "+447000000001", "msisdn_source": "manual"}))

    async def test_ims_correction_is_persisted_and_applied_to_running_engine(self):
        current = {"id": "2", "msisdn": "447000000000",
                   "msisdn_source": "modemmanager"}
        corrected = {**current, "msisdn": "+447000000001", "msisdn_source": "ims"}
        with patch.object(main.asyncio, "sleep", new=AsyncMock()), \
                patch.object(main.engine, "exec_cli", return_value=""), \
                patch.object(main, "extract_ims_identity",
                             return_value={"msisdn": corrected["msisdn"]}), \
                patch.object(main.cfg, "get_instance", return_value=current), \
                patch.object(main.cfg, "upsert_instance", return_value=corrected) as upsert, \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "is_running", return_value=True), \
                patch.object(main, "_start_engine_checked") as restart, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami, \
                patch.object(main.hub, "broadcast", new=AsyncMock()), \
                patch.object(main.hub, "reset_health"):
            await main.learn_msisdn("2")

        upsert.assert_called_once_with({"id": "2", "msisdn": "+447000000001",
                                        "msisdn_source": "ims"})
        drop_ami.assert_awaited_once_with("2")
        restart.assert_called_once_with(corrected, {}, False)


class ExistingModemCardTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(main, "_modem_reader_binding")
    @patch.object(main.glob, "glob")
    def test_live_modem_binding_follows_saved_iccid_not_stale_reader_name(
            self, paths, reader_binding):
        import json
        import tempfile
        from pathlib import Path

        wanted = "8944110000000000000"
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "wrong.json"
            right = Path(tmp) / "right.json"
            wrong.write_text(json.dumps({
                "hardware_id": "modem-a", "iccid": "wrong-card",
            }))
            right.write_text(json.dumps({
                "hardware_id": "modem-b", "iccid": wanted,
            }))
            paths.return_value = [str(wrong), str(right)]
            expected = {
                "pin_reader": "modem-b slot 0", "swu_reader": "modem-b slot 1",
                "ami_reader": "modem-b slot 2", "reader_index": 4,
            }
            reader_binding.side_effect = lambda name: (
                expected if "modem-b" in name else {})

            result = main._live_modem_binding_for_instance({"iccid": wanted})

        self.assertEqual(result, expected)
        reader_binding.assert_called_once_with("VoWiFi Modem modem-b 00 00")

    @patch.object(main.engine, "start", return_value="container-id")
    @patch.object(main, "_apply_current_hardware_imei", side_effect=lambda inst: inst)
    @patch.object(main, "_live_modem_binding_for_instance")
    @patch.object(main.cfg, "upsert_instance")
    @patch.object(main.cfg, "line_allowed", return_value=True)
    def test_every_engine_start_repairs_stale_modem_binding(
            self, _allowed, upsert, live_binding, _imei, start):
        stale = {
            "id": "2", "iccid": "8944110000000000000",
            "pin_reader": "wrong slot 0", "swu_reader": "wrong slot 1",
            "ami_reader": "wrong slot 2", "reader_index": 1,
        }
        binding = {
            "pin_reader": "right slot 0", "swu_reader": "right slot 1",
            "ami_reader": "right slot 2", "reader_index": 4,
        }
        corrected = {**stale, **binding}
        live_binding.return_value = binding
        upsert.return_value = corrected

        result = main._start_engine_checked(stale, {}, reason="auto-recover:test")

        self.assertEqual(result, "container-id")
        upsert.assert_called_once_with({"id": "2", **binding})
        start.assert_called_once_with(
            corrected, {}, dev_mounts=False, reason="auto-recover:test")

    @patch.object(main, "_modem_identity_for_reader", return_value={
        "hardware_id": "wrong-modem", "iccid": "wrong-card",
    })
    def test_live_modem_reader_name_does_not_hide_wrong_card(self, _identity):
        inst = {
            "id": "2", "iccid": "8944110000000000000",
            "pin_reader": "wrong slot 0", "swu_reader": "wrong slot 1",
            "ami_reader": "wrong slot 2", "reader_index": 1,
        }

        mismatch = main._card_identity_mismatch(inst)

        self.assertEqual(mismatch["reader"], "wrong slot 1")
        self.assertEqual(mismatch["iccid"], "wrong-card")

    def test_startup_bootstrap_migrates_and_seeds_known_present_modem(self):
        old = {
            "id": "2", "iccid": "8944110000000000000", "imsi": "234330123456789",
            "mcc": "234", "mnc": "33", "smsc": "+447700900000",
            "pin_reader": "old pin", "swu_reader": "old swu", "ami_reader": "old ims",
            "reader_index": 1,
        }
        binding = {
            "pin_reader": "new pin", "swu_reader": "new swu", "ami_reader": "new ims",
            "reader_index": 5, "reader_port": "", "imei_source_device_id": "new",
        }
        states = [
            {"index": 4, "name": "VoWiFi Modem new 00 00", "present": False},
            {"index": 5, "name": "VoWiFi Modem new 00 01", "present": True},
        ]
        main.hub.cards.clear()
        self.addCleanup(main.hub.cards.clear)
        with patch.object(main.card, "reader_states", return_value=states), \
                patch.object(main, "_modem_identity_for_reader", return_value={
                    "hardware_id": "new", "iccid": old["iccid"], "slots": 3}), \
                patch.object(main, "_match_instance_by_iccid", return_value=old), \
                patch.object(main, "_modem_reader_binding", return_value=binding), \
                patch.object(main.cfg, "upsert_instance",
                             return_value={**old, **binding}) as upsert:
            recovered = main._bootstrap_saved_modem_cards()

        self.assertEqual(recovered, ["2"])
        upsert.assert_called_once_with({"id": "2", **binding})
        self.assertNotIn("VoWiFi Modem new 00 00", main.hub.cards)
        seeded = main.hub.cards["VoWiFi Modem new 00 01"]
        self.assertEqual(seeded["matched"], "2")
        self.assertEqual(seeded["iccid"], old["iccid"])

    async def test_metadata_match_migrates_reader_group_without_discovery_apdu(self):
        old = {
            "id": "2", "iccid": "8944110000000000000", "imsi": "234330123456789",
            "mcc": "234", "mnc": "33", "smsc": "+447700900000",
            "pin_reader": "VoWiFi Modem old 00 00",
            "swu_reader": "VoWiFi Modem old 00 01",
            "ami_reader": "VoWiFi Modem old 00 02", "reader_index": 1,
        }
        binding = {
            "pin_reader": "VoWiFi Modem new 00 00",
            "swu_reader": "VoWiFi Modem new 00 01",
            "ami_reader": "VoWiFi Modem new 00 02", "reader_index": 5,
            "reader_port": "", "imei_source_device_id": "new",
        }
        main.hub.cards.clear()
        self.addCleanup(main.hub.cards.clear)
        with patch.object(main.usbreader, "port_for_index", return_value=None), \
                patch.object(main, "_modem_identity_for_reader", return_value={
                    "hardware_id": "new", "iccid": old["iccid"], "slots": 3}), \
                patch.object(main, "_match_instance_by_iccid", return_value=old), \
                patch.object(main, "_modem_reader_binding", return_value=binding), \
                patch.object(main.cfg, "upsert_instance",
                             return_value={**old, **binding}) as upsert, \
                patch.object(main.sim, "read_card") as read_card, \
                patch.object(main, "_auto_start_hotplugged_line",
                             new=AsyncMock()) as auto_start:
            await main._on_card_insert("VoWiFi Modem new 00 01", 5)
            await asyncio.sleep(0)

        read_card.assert_not_called()
        upsert.assert_called_once_with({"id": "2", **binding})
        auto_start.assert_awaited_once_with("2")
        self.assertEqual(main.hub.cards["VoWiFi Modem new 00 01"]["matched"], "2")


class IdenticalNativeReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_swapped_reader_names_are_attributed_by_live_card_identity(self):
        """A reboot may swap pcscd names while the physical USB ports stay fixed.

        A running engine's pin_status still naming the newly enumerated reader must not lend
        that line's saved ICCID to the other physical card. Both cards are probed and matched
        to their own lines before the live reader indices are refreshed.
        """
        cards = {
            0: main.sim.CardInfo(
                reader="AK9563 00 00", reader_index=0, present=True,
                iccid="iccid-b", imsi="imsi-b", mcc="002", mnc="02",
                pin_enabled=False, pin_tries=3, smsc="+200"),
            1: main.sim.CardInfo(
                reader="AK9563 01 00", reader_index=1, present=True,
                iccid="iccid-a", imsi="imsi-a", mcc="001", mnc="01",
                pin_enabled=False, pin_tries=3, smsc="+100"),
        }
        instances = {
            "1": {"id": "1", "iccid": "iccid-a", "imsi": "imsi-a",
                  "reader_index": 0, "reader_port": "2-1"},
            "2": {"id": "2", "iccid": "iccid-b", "imsi": "imsi-b",
                  "reader_index": 1, "reader_port": "2-3"},
        }
        pin_readers = {"1": "AK9563 00 00", "2": "AK9563 01 00"}

        def upsert(update):
            iid = str(update["id"])
            instances[iid].update(update)
            return dict(instances[iid])

        main.hub.cards.clear()
        self.addCleanup(main.hub.cards.clear)
        with patch.object(main.usbreader, "port_for_index",
                          side_effect=lambda idx: {0: "2-3", 1: "2-1"}[idx]), \
                patch.object(main, "_modem_identity_for_reader", return_value=None), \
                patch.object(main.cfg, "list_instances",
                             side_effect=lambda: [dict(value) for value in instances.values()]), \
                patch.object(main.engine, "is_running", return_value=True), \
                patch.object(main.engine, "read_run_json",
                             side_effect=lambda iid, _name: {"reader": pin_readers[str(iid)]}), \
                patch.object(main.sim, "read_card", side_effect=lambda idx: cards[idx]) as read, \
                patch.object(main.cfg, "upsert_instance", side_effect=upsert), \
                patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()):
            await main._on_card_insert("AK9563 00 00", 0)
            await main._on_card_insert("AK9563 01 00", 1)
            await asyncio.sleep(0)

        self.assertEqual(read.call_count, 2)
        self.assertEqual(main.hub.cards["AK9563 00 00"]["matched"], "2")
        self.assertEqual(main.hub.cards["AK9563 01 00"]["matched"], "1")
        self.assertEqual(instances["1"]["reader_port"], "2-1")
        self.assertEqual(instances["1"]["reader_index"], 1)
        self.assertEqual(instances["2"]["reader_port"], "2-3")
        self.assertEqual(instances["2"]["reader_index"], 0)


class EsimProfileRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_active_profile_creates_line_and_schedules_auto_start(self):
        card = SimpleNamespace(
            iccid="89000000000000000067", imsi="234100000000001",
            mcc="234", mnc="10", pin_enabled=False, pin_tries=3,
            smsc="+447785016005",
        )
        draft = {"id": "5", "iccid": card.iccid, "provisioning_state": "draft"}
        scheduled = []

        def capture(coro):
            scheduled.append(coro)
            coro.close()

        with patch.dict(main.hub.cards, {"Reader": {
                "index": 4, "name": "Reader", "present": True,
                "reader_port": "1-1.2", "matched": "4",
                "iccid": "89441000400130000000",
        }}, clear=True), \
                patch.object(main.sim, "read_card", return_value=card), \
                patch.object(main, "_match_instance_by_iccid", return_value=None), \
                patch.object(main.cfg, "card_auto_create_suppressed", return_value=False), \
                patch.object(main, "_ensure_card_draft", return_value=draft) as ensure, \
                patch.object(main.hub, "broadcast", new=AsyncMock()) as broadcast, \
                patch.object(main.asyncio, "create_task", side_effect=capture):
            result = await main._esim_refresh_card("Reader", 4)

        self.assertEqual(result["iccid"], card.iccid)
        self.assertEqual(result["matched"], "5")
        ensure.assert_called_once()
        broadcast.assert_awaited_once()
        self.assertEqual(len(scheduled), 1)


if __name__ == "__main__":
    unittest.main()
