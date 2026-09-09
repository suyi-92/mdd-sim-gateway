"""A saved proxy edit must invalidate old routes, verdicts and only the affected lines."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import egress, main, status
from host import mdd_orchestrator as orch
from test_egress_test_paths import proxy_config


INST = {"id": "1", "enabled": True, "mcc": "234", "mnc": "33"}


def route(proxy):
    return {"ready": True, "mode": "manual", "interface": "mdd-gb",
            "epdg": egress.epdg_for(INST), "addresses": ["93.184.216.34"],
            "config_revision": egress.country_exit_revision(proxy, "gb")}


class AppliedRouteTests(unittest.TestCase):
    def test_engine_waits_for_replaced_link_even_if_the_old_route_is_public_and_ready(self):
        proxy = proxy_config()
        old = route(proxy)
        proxy["profiles"]["node"]["value"] = proxy["profiles"]["node"]["value"].replace("uuid-1", "uuid-2")
        new = route(proxy)
        with patch.object(egress, "publish"), patch.object(egress.time, "sleep") as wait, \
                patch.object(egress, "status", side_effect=[
                    {"lines": {"1": old}}, {"lines": {"1": new}}]):
            actual = egress.ensure_line(INST, {"proxy": proxy})
        self.assertEqual(actual, new)
        wait.assert_called_once()

    def test_confirmed_route_rejects_old_configuration_wrong_epdg_and_fake_ip(self):
        proxy = proxy_config()
        for change in [{"config_revision": "old"}, {"epdg": "other.example.net"},
                       {"addresses": ["198.18.0.1"]}, {"interface": "mdd-us"}]:
            with self.subTest(change=change), patch.object(
                    egress, "status", return_value={"lines": {"1": route(proxy) | change}}):
                self.assertIsNone(egress.confirmed_line_route(INST, {"proxy": proxy}))
        with patch.object(egress, "status", return_value={"lines": {"1": route(proxy)}}):
            self.assertIsNotNone(egress.confirmed_line_route(INST, {"proxy": proxy}))

    def test_exit_resolver_cache_avoids_repeating_a_failing_host_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            app = orch.Orchestrator(Path(directory), Path.cwd())
            with patch.object(orch.socket, "getaddrinfo", side_effect=OSError("DNS timeout")) as dns, \
                    patch.object(orch, "resolve_ipv4_via_socks_doh",
                                 return_value=(["93.184.216.34"], 60)) as remote:
                # getaddrinfo raises gaierror on a failed resolver, not arbitrary socket I/O.
                dns.side_effect = orch.socket.gaierror("DNS timeout")
                first = app.resolve("epdg.example.net", "127.0.0.1", 22157)
                second = app.resolve("epdg.example.net", "127.0.0.1", 22157)
            self.assertEqual(first, second)
            dns.assert_called_once()
            remote.assert_called_once()

    def test_link_edit_invalidates_only_its_country_epdg_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            app = orch.Orchestrator(Path(directory), Path.cwd(), dry_run=True)
            proxy = proxy_config()
            app.build_proxy_config(proxy)
            gb_key = f"epdg.example:127.0.0.1:{orch.country_proxy_port('gb')}"
            nz_key = f"epdg.example:127.0.0.1:{orch.country_proxy_port('nz')}"
            app.epdg_proxy_dns = {key: (time.time() + 60, ["93.184.216.34"])
                                  for key in [gb_key, nz_key]}
            app.build_proxy_config(proxy)
            self.assertIn(gb_key, app.epdg_proxy_dns)
            proxy["profiles"]["node"]["value"] = proxy["profiles"]["node"]["value"].replace("sid=abcd", "sid=1234")
            app.build_proxy_config(proxy)
            self.assertNotIn(gb_key, app.epdg_proxy_dns)
            self.assertIn(nz_key, app.epdg_proxy_dns)


class AppliedRouteStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_ike_timeout_is_not_overwritten_by_host_dns_when_the_route_is_resolved(self):
        with patch.multiple(status.engine, is_running=lambda _iid: True,
                            read_run_json=lambda *_args: {"state": "PIN_DISABLED"},
                            tunnel_installed=lambda _iid: False), \
                patch.object(egress, "confirmed_line_route", return_value=route(proxy_config())), \
                patch.object(status, "resolve_epdg", return_value=False) as host_dns, \
                patch.object(status, "classify_ike", return_value=("tunnel_network", "timeout")):
            actual = await status.compute(INST)
        self.assertEqual(actual["reason_code"], "tunnel_network")
        host_dns.assert_not_called()


class QueueChangedExitTests(unittest.TestCase):
    def setUp(self):
        self.old = proxy_config()
        self.old["profiles"]["nz"] = {"type": "socks5", "name": "NZ", "server": "proxy.example.net"}
        self.old["exits"]["nz"] = {"enabled": True, "profile_id": "nz"}
        self.current = deepcopy(self.old)
        self.current["profiles"]["node"]["value"] = self.current["profiles"]["node"]["value"].replace("sid=abcd", "sid=1234")
        self.instances = [INST, {**INST, "id": "2", "enabled": False},
                          {"id": "5", "enabled": True, "mcc": "530", "mnc": "05"}]
        for patcher in [patch.multiple(main.hub, egress_updates={}, exit_ledgers={
                "1": {"exhausted": True}, "5": {"exhausted": True}}, health={}, ok_since={}),
                patch.object(main.cfg, "list_instances", return_value=self.instances),
                patch.object(main, "_save_exit_ledgers")]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_replaces_old_verdicts_and_queues_only_enabled_lines_of_the_changed_country(self):
        main._queue_changed_country_exits(self.old, self.current)
        self.assertEqual(set(main.hub.egress_updates), {"1"})
        self.assertNotIn("1", main.hub.exit_ledgers)
        self.assertEqual(main.hub.exit_ledgers["5"], {"exhausted": True})

    def test_renaming_a_node_or_its_link_fragment_does_not_rebuild_it(self):
        changed = deepcopy(self.old)
        changed["profiles"]["node"]["name"] = "Renamed"
        changed["profiles"]["node"]["value"] += "#Renamed"
        main._queue_changed_country_exits(self.old, changed)
        self.assertFalse(main.hub.egress_updates)
        self.assertIn("1", main.hub.exit_ledgers)

    def test_proxy_edit_does_not_clear_a_pin_failure(self):
        main.hub.health["1"] = {"frozen_code": "pin_invalid"}
        main._queue_changed_country_exits(self.old, self.current)
        self.assertNotIn("1", main.hub.egress_updates)
        self.assertEqual(main.hub.health["1"]["frozen_code"], "pin_invalid")

    def test_settings_endpoint_dispatches_the_saved_change_on_the_control_event_loop(self):
        loop = SimpleNamespace(is_running=lambda: True, call_soon_threadsafe=Mock())
        with patch.object(main.hub, "event_loop", loop), \
                patch.object(main.cfg, "get_settings", return_value={"proxy": self.old}), \
                patch.object(main.cfg, "update_settings", return_value={"proxy": self.current}), \
                patch.object(egress, "publish") as publish:
            main.api_put_settings({"proxy": self.current})
        publish.assert_called_once_with(settings={"proxy": self.current})
        loop.call_soon_threadsafe.assert_called_once_with(
            main._queue_saved_proxy_change, self.old)

    def test_delayed_settings_callback_uses_the_latest_saved_connection(self):
        latest = deepcopy(self.current)
        latest["profiles"]["node"]["value"] = latest["profiles"]["node"]["value"].replace("uuid-1", "uuid-3")
        with patch.object(main.cfg, "get_settings", return_value={"proxy": latest}):
            main._queue_saved_proxy_change(self.old)
        self.assertEqual(main.hub.egress_updates["1"]["revision"],
                         egress.country_exit_revision(latest, "gb"))

    def test_display_edit_does_not_strand_an_already_queued_connection_edit(self):
        main._queue_changed_country_exits(self.old, self.current)
        ticket = main.hub.egress_updates["1"]
        renamed = deepcopy(self.current)
        renamed["profiles"]["node"]["name"] = "Renamed"
        main._queue_changed_country_exits(self.current, renamed)
        self.assertIs(main.hub.egress_updates["1"], ticket)
        self.assertEqual(ticket["revision"], egress.country_exit_revision(renamed, "gb"))


class ApplyChangedExitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = {"proxy": proxy_config()}
        self.ticket = {"revision": egress.country_exit_revision(self.settings["proxy"], "gb"),
                       "running": True, "retry_at": 0}
        self.start = AsyncMock(return_value={"ok": True})
        self.runtime = SimpleNamespace(get=AsyncMock(return_value={"running": False}),
                                       invalidate=Mock())
        for patcher in [
            patch.multiple(main.hub, egress_updates={"1": self.ticket}, egress_rebuilding=set(),
                           health={}, _learning=set(), runtime=self.runtime),
            patch.object(main.cfg, "get_instance", return_value=INST),
            patch.object(main.cfg, "get_settings", return_value=self.settings),
            patch.object(main, "_line_auto_start_allowed", return_value=(True, "")),
            patch.object(main, "_record_lifecycle"),
            patch.object(main, "_start_instance", self.start),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_rebuild_uses_the_public_start_preflight(self):
        await main._apply_changed_country_exit("1", self.ticket)
        self.start.assert_awaited_once_with("1", health_reason="maintenance_rebuild",
                                            engine_reason="country-exit-updated")
        self.assertFalse(main.hub.egress_updates)
        self.runtime.invalidate.assert_called_once_with("1")

    async def test_a_newer_save_survives_completion_of_an_older_rebuild(self):
        newer = {**self.ticket, "revision": "newer"}

        async def save(*_args, **_kwargs):
            main.hub.egress_updates["1"] = newer

        self.start.side_effect = save
        await main._apply_changed_country_exit("1", self.ticket)
        self.assertIs(main.hub.egress_updates["1"], newer)

    async def test_two_edits_cannot_start_the_same_reader_concurrently(self):
        main.hub.egress_rebuilding.add("1")
        await main._apply_changed_country_exit("1", self.ticket)
        self.start.assert_not_awaited()
        self.assertFalse(self.ticket["running"])

    async def test_a_live_call_delays_rebuild(self):
        self.runtime.get.return_value = {"running": True}
        ami = SimpleNamespace(active_channel_count=AsyncMock(return_value=1))
        with patch.object(main.hub, "ami_for", AsyncMock(return_value=ami)):
            await main._apply_changed_country_exit("1", self.ticket)
        self.start.assert_not_awaited()
        self.assertIn("1", main.hub.egress_updates)

    async def test_unknown_call_state_on_a_connected_tunnel_delays_rebuild(self):
        self.runtime.get.return_value = {"running": True}
        with patch.object(main.hub, "ami_for", AsyncMock(return_value=None)), \
                patch.object(main.engine, "tunnel_installed", return_value=True):
            await main._apply_changed_country_exit("1", self.ticket)
        self.start.assert_not_awaited()

    async def test_a_line_disabled_after_the_save_is_not_started(self):
        with patch.object(main, "_line_auto_start_allowed", return_value=(False, "line_disabled")):
            await main._apply_changed_country_exit("1", self.ticket)
        self.start.assert_not_awaited()
        self.assertNotIn("1", main.hub.egress_updates)

    async def test_wrong_pin_does_not_enter_an_automatic_retry_loop(self):
        self.start.side_effect = main.HTTPException(409, {"code": "pin_invalid"})
        await main._apply_changed_country_exit("1", self.ticket)
        self.assertNotIn("1", main.hub.egress_updates)
        self.assertEqual(main.hub.health["1"]["frozen_code"], "pin_invalid")
        self.assertIsNone(main.hub.health["1"]["next_retry_at"])


class ManualExitCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = orch.Orchestrator(Path(directory.name), Path.cwd(), dry_run=True)
        self.app.root.mkdir(parents=True)
        self.config, self.states = self.app.build_proxy_config(proxy_config())
        self.stamp = time.time()
        self.request = {"ts": self.stamp, "line": "1", "node": "Example node",
                        "config_revision": self.states["gb"]["config_revision"]}

    def run_report(self, result=2):
        self.app.stalled_path.write_text(json.dumps({"countries": {"gb": self.request}}))
        with patch.object(self.app, "drop_exit_connections", return_value=result) as drop:
            self.app.process_stalled_reports(self.states)
        return drop

    def test_manual_node_has_private_cleanup_api_and_consumes_current_reports(self):
        self.assertEqual(self.config["experimental"]["clash_api"]["external_controller"],
                         orch.CLASH_API)
        self.run_report().assert_called_once_with("gb")
        self.assertEqual(self.app.handled_stalled["gb"], self.stamp)

    def test_old_link_report_cannot_close_the_replacement_link(self):
        self.request["config_revision"] = "previous"
        self.run_report().assert_not_called()

    def test_unreachable_cleanup_api_does_not_consume_the_request(self):
        self.run_report(result=None)
        self.assertNotIn("gb", self.app.handled_stalled)
        self.run_report().assert_called_once()

    def test_peer_that_connected_after_the_report_is_protected(self):
        self.app.desired_path.write_text(json.dumps({"lines": [
            {"id": "2", "country": "gb", "enabled": True}]}))
        path = self.app.data / "instances/2/run/swu_status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"state": "CONNECTED"}))
        with patch.object(orch, "run", return_value=SimpleNamespace(returncode=0, stdout="running")):
            self.run_report().assert_not_called()


if __name__ == "__main__":
    unittest.main()
