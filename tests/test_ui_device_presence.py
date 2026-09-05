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

    def test_device_page_uses_the_tested_selection_and_clears_stale_selection(self):
        source = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text()
        page = source.split("export function DevicesPage(", 1)[1].split("\nfunction CountryExitControl(", 1)[0]
        self.assertIn("deviceSelection(devices, selectedDeviceId, showDisconnected)", page)
        self.assertIn("visibleDevices.map", page)
        self.assertNotIn("{devices.map", page)
        self.assertIn("if (active !== selectedDeviceId) setSelectedDeviceId(active)", page)
        self.assertIn("if (!d) return", page)
        self.assertIn("No communication devices found", page)
