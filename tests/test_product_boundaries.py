import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from control.app import config, main

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PAGE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class ProductBoundaryTests(unittest.TestCase):
    def temp_config(self):
        temp = tempfile.TemporaryDirectory()
        paths = patch.multiple(
            config,
            DATA_DIR=temp.name,
            CONFIG_PATH=str(Path(temp.name) / "config.yaml"),
        )
        return temp, paths

    def test_default_thirteenth_line_is_allowed_but_fourteenth_is_refused(self):
        temp, paths = self.temp_config()
        with temp, paths:
            self.assertEqual(config.sim_line_limit(), 13)
            for iid in range(1, config.DEFAULT_SIM_LINE_LIMIT + 1):
                config.upsert_instance({"id": str(iid), "name": f"SIM {iid}"})
            with self.assertRaises(config.LineLimitError):
                config.upsert_instance({"id": "14", "name": "SIM 14"})
            edited = config.upsert_instance({"id": "13", "name": "kept"})
            self.assertEqual(edited["name"], "kept")

    def test_operator_limit_is_validated_and_enforced(self):
        temp, paths = self.temp_config()
        with temp, paths:
            saved = config.update_settings({"max_sim_lines": "3"})
            self.assertEqual(saved["max_sim_lines"], 3)
            for iid in range(1, 4):
                config.upsert_instance({"id": str(iid), "name": f"SIM {iid}"})
            with self.assertRaisesRegex(config.LineLimitError, "at most 3"):
                config.upsert_instance({"id": "4", "name": "SIM 4"})

            for value in (0, 33, True, "", "1.5"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    config.validate_sim_line_limit(value)

    def test_malformed_saved_limit_falls_back_to_thirteen(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"settings": {"max_sim_lines": 99}, "instances": {}})
            self.assertEqual(config.get_settings()["max_sim_lines"], 13)

    def test_new_cellular_modems_default_to_flight_mode_but_operator_can_disable_it(self):
        temp, paths = self.temp_config()
        with temp, paths:
            self.assertTrue(config.get_settings()["device_defaults"]["flight_mode"])
            saved = config.update_settings({"device_defaults": {"flight_mode": False}})
            self.assertFalse(saved["device_defaults"]["flight_mode"])
            self.assertFalse(saved["device_defaults"]["cellular_enabled"])
            self.assertTrue(saved["device_defaults"]["vowifi_enabled"])

    def test_stale_remote_controls_are_removed_on_load_and_save(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({
                "settings": {"telegram": {"commands": {"enabled": True}}},
                "instances": {"1": {"id": "1", "sip": {
                    "external": [{"username": "remote", "password": "secret"}]}}},
            })
            loaded = config.load()
            self.assertNotIn("commands", loaded["settings"]["telegram"])
            self.assertEqual(loaded["instances"]["1"]["sip"]["external"], [])

            saved = config.upsert_instance({"id": "1", "sip": {
                "external": [{"username": "remote", "password": "secret"}]}})
            self.assertEqual(saved["sip"]["external"], [])

    def test_retired_activation_event_is_removed_from_every_notification_channel(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({
                "settings": {key: {"events": {"activation_reminder": True}}
                             for key in ("webhook", "telegram", "pushplus", "feishu")},
                "instances": {},
            })
            settings = config.load()["settings"]
            for key in ("webhook", "telegram", "pushplus", "feishu"):
                self.assertNotIn("activation_reminder", settings[key]["events"])
                self.assertNotIn("software_update", settings[key]["events"])

    def test_retired_release_settings_are_ignored(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"settings": {"updates": {"update_mode": "automatic"}},
                         "instances": {}})
            self.assertNotIn("updates", config.load()["settings"])
            self.assertNotIn("updates", config.update_settings({
                "updates": {"proxy_mode": "library"}, "timezone": "UTC"}))

    def test_only_lines_within_the_configured_limit_are_startable(self):
        temp, paths = self.temp_config()
        with temp, paths:
            config.save({"instances": {
                str(iid): {"id": str(iid), "index": iid}
                for iid in range(1, 6)
            }, "settings": {"max_sim_lines": 4}})
            self.assertTrue(config.line_allowed("4"))
            self.assertFalse(config.line_allowed("5"))


if __name__ == "__main__":
    unittest.main()
