"""Regression coverage for honest DEVICE_IDENTITY handling on native readers."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "engine/swu_ike.py").read_text(encoding="utf-8")
PJSIP = (ROOT / "engine/templates/pjsip.conf.j2").read_text(encoding="utf-8")


class DeviceIdentityPolicyTests(unittest.TestCase):
    def test_engine_has_no_sample_equipment_identity_fallback(self):
        defaults = SOURCE[SOURCE.index("# IMEI (15 digits)"):SOURCE.index("NONE = 0")]
        self.assertIn("IMEI                                    = ''", defaults)
        self.assertIn("IMEISV                                  = ''", defaults)
        self.assertNotIn("123456789012", defaults)

    def test_missing_identity_omits_notify_in_both_exchange_paths(self):
        self.assertIn("identity_requested and self.has_device_identity()", SOURCE)
        self.assertIn("acknowledging without the Notify", SOURCE)
        self.assertIn("DEVICE_IDENTITY will be omitted", SOURCE)

    def test_unspecified_identity_type_prefers_a_complete_imeisv(self):
        encoder = SOURCE[SOURCE.index("def encode_device_identity_notification_data"):
                         SOURCE.index("def encode_payload_type_sk")]
        self.assertIn("self.device_identity_type not in (0x01, 0x02)", encoder)
        self.assertIn("len(self.imeisv) == 16", encoder)

    def test_identity_value_is_never_written_to_the_engine_log(self):
        encoder = SOURCE[SOURCE.index("def encode_device_identity_notification_data"):
                         SOURCE.index("def encode_payload_type_sk")]
        self.assertIn("value redacted", encoder)
        self.assertNotIn("digits.rstrip", encoder)

    def test_missing_smsc_does_not_render_an_invalid_sip_uri(self):
        self.assertIn("{% if smsc %}smsc_uri=", PJSIP)


if __name__ == "__main__":
    unittest.main()
