"""Execute the same device selection module used by the React views."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DevicePresenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "JavaScript behavior checks require Node.js")
    def test_device_selection_and_unplug_reconnect_transitions(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(ROOT / "tests/webui_device_presence.mjs")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "JavaScript behavior checks require Node.js")
    def test_device_names_have_one_default_and_user_override_policy(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(ROOT / "tests/webui_device_names.mjs")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "JavaScript behavior checks require Node.js")
    def test_communication_line_details_are_complete_and_number_safe(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(ROOT / "tests/webui_sim_line_details.mjs")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_device_page_uses_the_tested_selection_and_clears_stale_selection(self):
        source = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text()
        page = source.split("export function DevicesPage(", 1)[1].split("\nfunction CountryExitControl(", 1)[0]
        self.assertIn("deviceSelection(devices, selectedDeviceId, showDisconnected)", page)
        self.assertIn("visibleDevices.map", page)
        self.assertNotIn("{devices.map", page)
        self.assertIn("if (active !== selectedDeviceId) setSelectedDeviceId(active)", page)
        self.assertIn("if (!d) return", page)
        self.assertIn("No communication devices found", page)

    def test_user_facing_device_surfaces_share_the_naming_helper(self):
        paths = (
            ROOT / "webui/src/views/UnifiedPages.jsx",
            ROOT / "webui/src/views/SimSelector.jsx",
            ROOT / "webui/src/views/Esim.jsx",
            ROOT / "webui/src/views/SimConfig.jsx",
        )
        for path in paths:
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIn("deviceTitle", source)
                self.assertIn("deviceNames.js", source)

        app = (ROOT / "webui/src/App.jsx").read_text(encoding="utf-8")
        self.assertIn("'hardware'", app)
        sim_config = (ROOT / "webui/src/views/SimConfig.jsx").read_text(encoding="utf-8")
        self.assertIn("r.includes(targetDevice.id)", sim_config)
