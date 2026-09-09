"""Exercise the library test and the country API, including stale but matching state."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from control.app import egress, main
from host import mdd_orchestrator as orch
from test_admin_recovery_transfer import ApiClient


LINK = ("vless://uuid-1@relay.example.net:443?security=reality&type=tcp"
        "&fp=chrome&pbk=fixture-key&sid=abcd")


def proxy_config():
    return {"enabled": True, "profiles": {
        "node": {"name": "Example node", "type": "node", "value": LINK}},
        "exits": {"gb": {"enabled": True, "profile_id": "node"}}}


class LibraryProxyTestPaths(unittest.TestCase):
    def setUp(self):
        self.checked = []

        def check(command, **kwargs):
            self.checked.append(json.loads(Path(command[-1]).read_text()))
            return Mock(returncode=0)

        for patcher in (
            patch.object(egress, "_orchestrator_module", return_value=orch),
            patch.object(egress.shutil, "which", side_effect=lambda name: name),
            patch.object(egress.subprocess, "run", side_effect=check),
            patch.object(egress.subprocess, "Popen", return_value=Mock(poll=lambda: None)),
            patch.object(egress, "_wait_tcp"),
            patch.object(egress, "_stop_process"),
            patch.object(egress, "test_udp_proxy", return_value=42),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_library_uses_tcp_dns_and_preserves_the_original_reality_name(self):
        profile = proxy_config()["profiles"]["node"]
        original = deepcopy(profile)
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp",
                          return_value=(["192.0.2.44"], 60)) as dns:
            self.assertEqual(egress.test_proxy_profile(profile), 42)
        dns.assert_called_once_with("relay.example.net")
        actual = self.checked[1]["outbounds"][0]
        self.assertEqual(actual["settings"]["vnext"][0]["address"], "192.0.2.44")
        self.assertEqual(actual["streamSettings"]["realitySettings"]["serverName"],
                         "relay.example.net")
        self.assertEqual(profile, original)

    def test_ip_literal_needs_no_dns_and_keeps_explicit_sni(self):
        profile = {"type": "node", "value": LINK.replace(
            "relay.example.net", "192.0.2.44") + "&sni=tls.example.net"}
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp") as dns:
            egress.test_proxy_profile(profile)
        dns.assert_not_called()
        actual = self.checked[1]["outbounds"][0]
        self.assertEqual(actual["settings"]["vnext"][0]["address"], "192.0.2.44")
        self.assertEqual(actual["streamSettings"]["realitySettings"]["serverName"],
                         "tls.example.net")

    def test_dns_failure_cannot_fall_back_to_the_host_udp_resolver(self):
        with patch.object(orch, "resolve_ipv4_direct_dns_tcp",
                          side_effect=RuntimeError("private DNS detail")), \
                patch.object(egress.subprocess, "Popen") as start:
            with self.assertRaisesRegex(egress.EgressError, "node DNS lookup failed") as error:
                egress.test_proxy_profile(proxy_config()["profiles"]["node"])
        self.assertNotIn("private DNS detail", str(error.exception))
        start.assert_not_called()


class CountryTestRevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = {"proxy": proxy_config()}
        self.document = {"updated_at": 1, "enabled": True, "exits": {"gb": {
            "ready": True, "node": "Example node", "interface": "mdd-gb",
            "proxy_host": "127.0.0.1", "proxy_port": 22157,
            "config_revision": egress.country_exit_revision(self.settings["proxy"], "gb"),
        }}}
        self.probe = Mock(return_value=42)
        self.finish = Mock(return_value=True)
        for patcher in (
            patch.object(main.cfg, "get_settings", side_effect=lambda: self.settings),
            patch.object(egress, "publish"),
            patch.object(egress, "request_test", return_value=("fixture-token", 1000)),
            patch.object(egress, "finish_test", self.finish),
            patch.object(egress, "status", side_effect=lambda: self.document),
            patch.object(egress, "test_udp_proxy", self.probe),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_unchanged_old_status_still_requires_and_accepts_a_real_udp_probe(self):
        with patch.object(main.asyncio, "sleep", side_effect=AssertionError(
                "waiting for unrelated line DNS blocked the country test")):
            result = await main._test_egress_country("gb")
        self.assertEqual(result["latency_ms"], 42)
        self.probe.assert_called_once_with("127.0.0.1", 22157)
        self.finish.assert_called_once_with("gb", "fixture-token")

    async def test_old_ready_flag_cannot_hide_a_dead_udp_listener(self):
        self.probe.side_effect = egress.EgressError("connection refused")
        with self.assertRaises(main.HTTPException) as error:
            await main._test_egress_country("gb")
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail, "connection refused")
        self.finish.assert_called_once()

    async def test_replaced_link_waits_for_its_own_applied_configuration(self):
        self.settings["proxy"]["profiles"]["node"]["value"] = LINK.replace("uuid-1", "uuid-2")
        revision = egress.country_exit_revision(self.settings["proxy"], "gb")
        applied = False

        async def apply(_seconds):
            nonlocal applied
            self.document["exits"]["gb"]["config_revision"] = revision
            applied = True

        def probe(*_args):
            self.assertTrue(applied, "the API probed the previous node")
            return 42

        self.probe.side_effect = probe
        with patch.object(main.asyncio, "sleep", side_effect=apply):
            self.assertTrue((await main._test_egress_country("gb"))["ok"])

    async def test_legacy_status_without_a_revision_is_not_trusted(self):
        self.document["exits"]["gb"].pop("config_revision")

        async def apply(_seconds):
            self.document["exits"]["gb"]["config_revision"] = egress.country_exit_revision(
                self.settings["proxy"], "gb")

        with patch.object(main.asyncio, "sleep", side_effect=apply) as wait:
            await main._test_egress_country("gb")
        wait.assert_awaited_once()

    async def test_edit_during_probe_does_not_publish_success_for_the_previous_link(self):
        def edit(*_args):
            self.settings["proxy"]["profiles"]["node"]["value"] = LINK.replace("uuid-1", "uuid-2")
            return 42

        self.probe.side_effect = edit
        with self.assertRaises(main.HTTPException) as error:
            await main._test_egress_country("gb")
        self.assertEqual(error.exception.status_code, 409)
        self.finish.assert_called_once()


class CountryTestHttpPath(unittest.TestCase):
    def test_authenticated_page_endpoint_accepts_matching_applied_state(self):
        proxy = proxy_config()
        state = {"updated_at": 1, "exits": {"gb": {
            "ready": True, "proxy_host": "127.0.0.1", "proxy_port": 22157,
            "config_revision": egress.country_exit_revision(proxy, "gb"),
        }}}
        with patch.object(main.cfg, "get_settings", return_value={"proxy": proxy}), \
                patch.object(main.auth, "session", return_value={"csrf": "fixture-csrf"}), \
                patch.object(main, "_write_audit_record"), \
                patch.object(egress, "publish"), \
                patch.object(egress, "request_test", return_value=("fixture-token", 1000)), \
                patch.object(egress, "finish_test") as finish, \
                patch.object(egress, "status", return_value=state), \
                patch.object(egress, "test_udp_proxy", return_value=42):
            response = ApiClient().post("/api/egress/gb/test", content=b"{}", headers={
                "Content-Type": "application/json", "X-MDD-CSRF-Token": "fixture-csrf"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["latency_ms"], 42)
        finish.assert_called_once_with("gb", "fixture-token")

    def test_revision_is_country_scoped_and_tracks_connection_and_assignment_changes(self):
        proxy = proxy_config()
        revision = orch.country_exit_revision(proxy, "gb")
        proxy["profiles"]["unused"] = {"type": "node", "value": "fixture"}
        proxy["exits"]["us"] = {"enabled": True, "profile_id": "unused"}
        self.assertEqual(orch.country_exit_revision(proxy, "gb"), revision)
        scoped = orch.Orchestrator.proxy_reconcile_desired(
            {"proxy": proxy}, False, {"gb"})["proxy"]
        self.assertEqual(orch.country_exit_revision(scoped, "gb"), revision)
        proxy["profiles"]["node"]["value"] = LINK.replace("uuid-1", "uuid-2")
        self.assertNotEqual(orch.country_exit_revision(proxy, "gb"), revision)


if __name__ == "__main__":
    unittest.main()
