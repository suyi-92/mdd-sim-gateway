"""Updating a saved node must refresh its DNS pin and publish only complete bridges."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from host import mdd_orchestrator as orch


LINK = ("vless://uuid-1@relay.example.net:443?security=reality&type=tcp"
        "&sni=tls.example.net&fp=chrome&pbk=old-key&sid=abcd")


def proxy_config(link=LINK):
    return {"enabled": True, "profiles": {
        "node": {"type": "node", "name": "Saved node", "value": link}},
        "exits": {"gb": {"enabled": True, "profile_id": "node"}}}


class ProxyProfileUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = orch.Orchestrator(Path(directory.name), Path.cwd())

    def test_replacing_link_with_same_name_refreshes_dns_before_its_old_ttl(self):
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp", side_effect=[
                (["192.0.2.1"], 300), (["192.0.2.2"], 300)]) as resolve:
            self.app.build_proxy_config(proxy_config())
            first = deepcopy(self.app.next_xray_config)
            changed = proxy_config(LINK.replace("old-key", "new-key"))
            for _ in range(2):
                _, states = self.app.build_proxy_config(changed)
                self.assertTrue(states["gb"]["ready"])
            self.assertEqual(resolve.call_count, 2)
        before = first["outbounds"][0]
        after = self.app.next_xray_config["outbounds"][0]
        self.assertEqual(before["settings"]["vnext"][0]["address"], "192.0.2.1")
        self.assertEqual(after["settings"]["vnext"][0]["address"], "192.0.2.2")
        self.assertEqual(after["streamSettings"]["realitySettings"]["publicKey"], "new-key")
        self.assertEqual(after["streamSettings"]["realitySettings"]["serverName"],
                         "tls.example.net")

    def test_changed_node_cannot_fall_back_to_the_previous_nodes_dns_pin(self):
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp", side_effect=[
                (["192.0.2.1"], 300), RuntimeError("DNS unavailable")]):
            self.app.build_proxy_config(proxy_config())
            _, states = self.app.build_proxy_config(
                proxy_config(LINK.replace("old-key", "new-key")))
        self.assertFalse(states["gb"]["ready"])
        self.assertIsNone(self.app.next_xray_config)

    def test_a_display_name_edit_preserves_the_working_dns_pin(self):
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp",
                          return_value=(["192.0.2.1"], 300)) as resolve:
            self.app.build_proxy_config(proxy_config())
            before = deepcopy(self.app.next_xray_config)
            changed = proxy_config(LINK + "#New%20display%20name")
            changed["profiles"]["node"]["name"] = "Renamed node"
            self.app.build_proxy_config(changed)
        resolve.assert_called_once()
        self.assertEqual(self.app.next_xray_config, before)

    def test_failed_shared_node_does_not_publish_a_half_built_bridge(self):
        proxy = proxy_config()
        proxy["exits"]["us"] = {"enabled": True, "profile_id": "node"}
        proxy["profiles"]["other"] = {"type": "socks5", "name": "Other node",
                                         "server": "192.0.2.3", "port": 1080}
        proxy["exits"]["de"] = {"enabled": True, "profile_id": "other"}
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp",
                          side_effect=RuntimeError("DNS unavailable")):
            config, states = self.app.build_proxy_config(proxy)
        self.assertFalse(states["gb"]["ready"])
        self.assertFalse(states["us"]["ready"])
        self.assertTrue(states["de"]["ready"])
        self.assertIsNone(self.app.next_xray_config)
        self.assertEqual([out["tag"] for out in config["outbounds"]], ["exit-de"])

    def _processes(self):
        processes = []

        def start(*args, **kwargs):
            process = Mock()
            process.poll.return_value = None
            processes.append(process)
            return process

        for patcher in (patch.object(orch.shutil, "which", side_effect=lambda name: name),
                        patch.object(self.app, "isolate_country_tun_dns"),
                        patch.object(orch, "run", return_value=Mock(returncode=0)),
                        patch.object(orch.subprocess, "Popen", side_effect=start),
                        patch.object(orch.time, "sleep"),
                        patch.object(orch, "resolve_ipv4_direct_dns_tcp",
                                     return_value=(["192.0.2.1"], 300))):
            patcher.start()
            self.addCleanup(patcher.stop)
        return processes

    def _apply(self, proxy):
        config, _ = self.app.build_proxy_config(proxy)
        self.app.apply_xray(self.app.next_xray_config)
        self.app.apply_singbox(config)

    def test_link_update_applies_new_credentials_to_the_running_xray(self):
        processes = self._processes()
        self._apply(proxy_config())
        first_xray, first_singbox = self.app.xray, self.app.singbox
        self._apply(proxy_config())
        self.assertEqual(len(processes), 2, "unchanged settings must keep both children")
        self._apply(proxy_config(LINK.replace("old-key", "new-key")))
        self.assertIsNot(self.app.xray, first_xray)
        first_xray.terminate.assert_called_once()
        first_singbox.terminate.assert_not_called()
        self.assertEqual(len(processes), 3)
        actual = json.loads(self.app.xray_generated.read_text())
        self.assertEqual(actual["outbounds"][0]["streamSettings"]["realitySettings"]["publicKey"],
                         "new-key")

    def test_rejected_candidate_keeps_the_checked_running_processes(self):
        self._processes()
        self._apply(proxy_config())
        first_xray, first_singbox = self.app.xray, self.app.singbox
        config, _ = self.app.build_proxy_config(
            proxy_config(LINK.replace("old-key", "new-key")))
        with patch.object(orch, "run", return_value=Mock(
                returncode=1, stderr="invalid test candidate")):
            with self.assertRaisesRegex(RuntimeError, "Xray config invalid"):
                self.app.apply_xray(self.app.next_xray_config)
        self.app.apply_singbox(config)
        self.assertIs(self.app.xray, first_xray)
        self.assertIs(self.app.singbox, first_singbox)
        first_xray.terminate.assert_not_called()
        first_singbox.terminate.assert_not_called()

    def test_startup_failure_preserves_the_checked_xray_config(self):
        self._processes()
        self._apply(proxy_config())
        first_singbox = self.app.singbox
        previous = self.app.xray_generated.read_bytes()
        config, _ = self.app.build_proxy_config(
            proxy_config(LINK.replace("old-key", "new-key")))
        failed, restored = Mock(), Mock()
        failed.poll.return_value = 1
        restored.poll.return_value = None
        with patch.object(orch.subprocess, "Popen", side_effect=[failed, restored]):
            with self.assertRaisesRegex(RuntimeError, "Xray exited during startup"):
                self.app.apply_xray(self.app.next_xray_config)
        self.assertEqual(self.app.xray_generated.read_bytes(), previous)
        self.assertIs(self.app.xray, restored)
        self.app.apply_singbox(config)
        self.assertIs(self.app.singbox, first_singbox)

    def test_unchanged_node_survives_a_temporary_dns_refresh_outage(self):
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp", side_effect=[
                (["192.0.2.1"], 300), RuntimeError("DNS unavailable")]), \
                patch.object(orch.time, "time", return_value=100):
            self.app.build_proxy_config(proxy_config())
            before = deepcopy(self.app.next_xray_config)
            with patch.object(orch.time, "time", return_value=10000):
                _, states = self.app.build_proxy_config(proxy_config())
        self.assertTrue(states["gb"]["ready"])
        self.assertEqual(self.app.next_xray_config, before)

    def test_removing_last_valid_exit_stops_the_obsolete_listener(self):
        self._processes()
        self._apply(proxy_config())
        first_singbox = self.app.singbox
        invalid = proxy_config("vless://uuid-1@relay.example.net:invalid")
        with patch.object(self.app, "apply_routes") as routes:
            self.app.reconcile_proxy({"proxy": invalid, "lines": []})
        routes.assert_called_once_with(set())
        first_singbox.terminate.assert_called_once()
        self.assertIsNone(self.app.singbox)
        self.assertIsNone(self.app.xray)


if __name__ == "__main__":
    unittest.main()
