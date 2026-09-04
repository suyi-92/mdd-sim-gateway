"""Regression coverage for per-device capability operation state."""
import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parent.parent
          / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
I18N = (Path(__file__).resolve().parent.parent
        / "webui/src/i18n.jsx").read_text(encoding="utf-8")


class CapabilitySwitchStateTests(unittest.TestCase):
    def test_every_capability_switch_is_keyed_by_device_and_capability(self):
        switches = re.findall(r"<CapabilitySwitch\b[^>]*>", SOURCE)
        self.assertTrue(switches)
        for switch in switches:
            kind = re.search(r'kind="([^"]+)"', switch)
            key = re.search(r'key=\{`\$\{d\.id\}:([^`]+)`\}', switch)
            self.assertIsNotNone(kind, switch)
            self.assertIsNotNone(key, switch)
            self.assertEqual(key.group(1), kind.group(1), switch)

    def test_enabled_help_text_is_specific_to_each_capability(self):
        self.assertIn(
            "cellular: 'Working — connected to the carrier over the cellular network.'",
            SOURCE)
        self.assertIn(
            "flight: 'Flight mode is active; the cellular radio is disabled.'", SOURCE)
        self.assertIn("vowifi: 'Working — connected to the carrier over Wi-Fi.'", SOURCE)
        self.assertIn("'运行正常：已通过蜂窝网络连接运营商。'", I18N)

    def test_cellular_control_is_named_as_data_not_base_station_registration(self):
        self.assertIn("t('Cellular data (4G)')", SOURCE)
        self.assertNotIn("t('4G network')", SOURCE)
        self.assertIn(
            "cellular: 'Mobile data is disconnected; the modem radio can remain registered",
            SOURCE)
        self.assertIn("'蜂窝数据（4G）'", I18N)
        self.assertIn("模块仍可保持注册到蜂窝网络", I18N)

    def test_device_badge_combines_cellular_and_vowifi_state(self):
        self.assertIn("const capabilities = ['cellular', 'vowifi']", SOURCE)
        self.assertIn("<Badge state={deviceConnectivityState(x)} />", SOURCE)


if __name__ == "__main__":
    unittest.main()
