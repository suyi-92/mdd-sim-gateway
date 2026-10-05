import os
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from control.app import config


class PortAllocationTests(unittest.TestCase):
    def test_all_32_lines_can_be_allocated_without_overlaps(self):
        data = {"instances": {}}
        with patch.object(config, "_host_port_free", return_value=True):
            for index in range(config.MAX_SIM_LINE_LIMIT):
                block = config.alloc_ports_auto(data)
                ports = config._block_ports(block)
                self.assertFalse(ports & config._reserved_ports(data))
                self.assertLessEqual(max(ports), 65535)
                data["instances"][str(index)] = {"ports": block}
        self.assertEqual(len(data["instances"]), 32)

    def test_28_legacy_blocks_are_preserved_and_four_new_lines_fit(self):
        data = {"instances": {str(i): {"ports": {
            k: v for k, v in config._alloc_ports(i).items() if k != "rtp_span"}}
            for i in range(28)}}
        saved = deepcopy(data["instances"])
        with patch.object(config, "_host_port_free", return_value=True):
            for i in range(28, 32):
                block = config.alloc_ports_auto(data)
                self.assertFalse(config._block_ports(block) & config._reserved_ports(data))
                self.assertLessEqual(max(config._block_ports(block)), 65535)
                data["instances"][str(i)] = {"ports": block}
        self.assertEqual({k: data["instances"][k] for k in saved}, saved)

    def test_fallback_checks_live_service_and_rtp_conflicts(self):
        data = {"instances": {str(i): {"ports": config._alloc_ports(i)} for i in range(28)}}
        with patch.object(config, "_host_port_free", return_value=True):
            first = config.alloc_ports_auto(data)
        for conflict in (first["sip_udp"], first["webrtc"], first["rtp_start"] + 11):
            with self.subTest(conflict=conflict), patch.object(
                    config, "_host_port_free", side_effect=lambda port, **kw: port != conflict):
                chosen = config.alloc_ports_auto(data)
                self.assertNotIn(conflict, config._block_ports(chosen))

    def test_real_exhaustion_is_typed_and_does_not_modify_saved_lines(self):
        data = {"instances": {"1": {"ports": config._alloc_ports(0)}}}
        saved = deepcopy(data)
        with patch.object(config, "_host_port_free", return_value=False):
            with self.assertRaises(config.PortAllocationError):
                config.alloc_ports_auto(data)
        self.assertEqual(data, saved)

    def test_new_blocks_use_the_compact_rtp_span(self):
        block = config._alloc_ports(0)
        self.assertEqual(block["rtp_span"], config.DEFAULT_RTP_SPAN)
        self.assertEqual(config.rtp_span(block), 12)
        self.assertEqual(len(config._block_ports(block)), 16)

    def test_saved_blocks_without_span_keep_legacy_width(self):
        block = {key: value for key, value in config._alloc_ports(0).items()
                 if key != "rtp_span"}
        self.assertEqual(config.rtp_span(block), config.LEGACY_RTP_SPAN)
        self.assertIn(block["rtp_start"] + 59, config._block_ports(block))

    def test_rendered_rtp_end_matches_the_effective_span(self):
        base = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"webrtc": {"password": "password"}},
        }
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            compact = {**base, "ports": config._alloc_ports(0)}
            rendered = config.render_instance_json(compact, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 11)

            legacy_ports = {key: value for key, value in config._alloc_ports(0).items()
                            if key != "rtp_span"}
            legacy = {**base, "ports": legacy_ports}
            rendered = config.render_instance_json(legacy, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 59)

    @staticmethod
    def _windows_mirrored_port_probe(port, *, tcp=True, udp=True):
        return not (udp and port in {10010, 10011})

    def test_auto_allocator_skips_a_live_rtp_udp_conflict(self):
        with patch.object(config, "_host_port_free",
                          side_effect=self._windows_mirrored_port_probe):
            block = config.alloc_ports_auto({"instances": {}})

        self.assertEqual(block["sip_udp"], 5070)
        self.assertEqual(block["rtp_start"], 12000)
        self.assertEqual(block["rtp_span"], 12)

    def test_manual_port_selection_rejects_a_live_rtp_udp_conflict(self):
        with patch.object(config, "_host_port_free",
                          side_effect=self._windows_mirrored_port_probe):
            with self.assertRaisesRegex(ValueError, r"port 10010 \(RTP/UDP\).+already in use"):
                config.ports_from_sip_base({"instances": {}}, 5060)


if __name__ == "__main__":
    unittest.main()
