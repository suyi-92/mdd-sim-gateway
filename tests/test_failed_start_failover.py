import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import egress, main
from control.app.egress_contract import proxy_fingerprint
from host import mdd_orchestrator as orch


class FailedStartFailoverTests(unittest.IsolatedAsyncioTestCase):
    transport = "host"

    def setUp(self):
        self.inst = {"id": "probe-line", "enabled": True, "mcc": "310", "mnc": "240"}
        self.settings = {"proxy": {"enabled": True, "exits": {
            "us": {"enabled": True, "mode": "subscription"}}}}
        self.state = {"version": 1, "enabled": True, "transport": "socks5",
                      "updated_at": time.time(),
                      "config_fingerprint": proxy_fingerprint(self.settings["proxy"]),
                      "exits": {"us": {"ready": True, "mode": "subscription",
                                         "selection": "managed", "transport": "socks5",
                                         "node": "node-a", "node_tag": "exit-us-0",
                                         "candidates": ["node-a", "node-b"],
                                         "proxy_host": "127.0.0.1", "proxy_port": 22538}}}
        self.state["exits"]["us"]["config_revision"] = egress.country_exit_revision(
            self.settings["proxy"], "us")
        if self.transport == "host":
            # Native proxy-status has no container fingerprint/version contract.
            for key in ("version", "transport", "config_fingerprint"):
                self.state.pop(key)
            self.state["exits"]["us"].pop("transport")
        self.real_reselect = egress.request_reselect
        self.reselect = Mock()
        self.probe = Mock(side_effect=egress.EgressError("probe failed"))
        self.lifecycle = Mock()
        failure = main.HTTPException(503, {"code": "egress_unavailable",
                                           "message": "startup DNS resolution failed"})
        patches = [
            patch.object(main, "hub", main.Hub()),
            patch.object(main.cfg, "get_settings", return_value=self.settings),
            patch.object(main.cfg, "get_instance", return_value=self.inst),
            patch.object(main.cfg, "list_instances", return_value=[self.inst]),
            patch.object(main, "_line_auto_start_allowed", return_value=(True, "")),
            patch.object(main, "_local_card_fault", return_value=""),
            patch.object(main, "_save_exit_ledgers"),
            patch.object(main, "_record_lifecycle", self.lifecycle),
            patch.object(main, "_start_engine_checked", side_effect=failure),
            patch.object(main.engine, "read_run_json", return_value={"state": "CONNECTED"}),
            patch.object(main.engine, "ike_evidence", return_value={"retransmits": 0}),
            patch.object(main.egress, "transport", return_value=self.transport),
            patch.object(main.egress, "status", side_effect=lambda: copy.deepcopy(self.state)),
            patch.object(main.egress, "test_udp_proxy", self.probe),
            patch.object(main.egress, "request_reselect", self.reselect),
            patch.object(main.egress, "report_stalled_exit"),
            patch.object(main.notify_push, "dispatch"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.health = main.hub.health_for(self.inst["id"])
        main.hub.exit_ledgers.clear()
        self.health.update({"frozen_code": "reg_unanswered", "retry_count": 3,
                            "auto_retrying": True})
        main.hub.broadcast = AsyncMock()
        main.hub.ok_since[self.inst["id"]] = time.monotonic() - 35 * 3600

    async def recover(self):
        await main._auto_recover_instance(self.inst["id"], self.inst, 120)

    async def test_three_verified_failures_switch_even_after_a_long_healthy_run(self):
        for _ in range(2):
            await self.recover()
            self.reselect.assert_not_called()
        await self.recover()
        self.reselect.assert_called_once_with(
            self.inst, "health-freeze:egress_unavailable", stable_for=0.0,
            probe_context=egress.exit_probe_context(self.inst, self.settings))
        self.assertEqual(main.hub.exit_ledgers[self.inst["id"]]["tried"], ["node-a"])
        self.assertEqual(self.probe.call_count, 3)
        self.assertIn("recovery_failed", [call.args[1] for call in self.lifecycle.call_args_list])

    async def test_a_successful_udp_probe_breaks_the_failure_streak(self):
        await self.recover()
        await self.recover()
        self.probe.side_effect = None
        self.probe.return_value = 300
        await self.recover()
        self.assertEqual(main.hub.exit_ledgers[self.inst["id"]]["strikes"], 0)
        self.probe.side_effect = egress.EgressError("probe failed again")
        await self.recover()
        self.reselect.assert_not_called()
        self.assertEqual(main.hub.exit_ledgers[self.inst["id"]]["strikes"], 1)

    async def test_manual_pin_is_not_probed_or_changed(self):
        self.state["exits"]["us"]["selection"] = "manual"
        for _ in range(4):
            await self.recover()
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_registered_same_country_peer_is_not_interrupted(self):
        peer = {"id": "peer", "enabled": True, "mcc": "310"}
        main.hub.status_cache["peer"] = {"state": "OK"}
        with patch.object(main.cfg, "list_instances", return_value=[self.inst, peer]):
            await self.recover()
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_peer_registering_during_the_probe_blocks_attribution(self):
        with patch.object(main, "_peer_line_registered", side_effect=[False, True]):
            await self.recover()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)
        self.reselect.assert_not_called()

    async def test_selector_change_during_probe_does_not_blame_replacement(self):
        def change_selector(*_args, **_kwargs):
            self.state["exits"]["us"].update(node="node-b", node_tag="exit-us-1")
            raise egress.EgressError("old node probe failed")
        self.probe.side_effect = change_selector
        await self.recover()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)
        self.reselect.assert_not_called()

    async def test_configuration_change_during_probe_discards_old_evidence(self):
        def change_configuration(*_args, **_kwargs):
            self.settings["proxy"]["exits"]["us"]["enabled"] = False
            raise egress.EgressError("old configuration probe failed")
        self.probe.side_effect = change_configuration
        await self.recover()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_stale_contract_does_not_start_a_node_walk(self):
        self.state["updated_at"] -= 100
        await self.recover()
        self.probe.assert_not_called()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_invalid_listener_or_unready_state_does_not_probe(self):
        selected = self.state["exits"]["us"]
        selected["ready"] = False
        await self.recover()
        selected["ready"] = True
        selected["proxy_host"] = "invalid/endpoint"
        await self.recover()
        self.probe.assert_not_called()

    async def test_changed_probe_evidence_is_rechecked_by_the_judge(self):
        context = egress.exit_probe_context(self.inst, self.settings)
        self.state["exits"]["us"].update(node="node-b", node_tag="exit-us-1")
        self.assertEqual(main._judge_exit_failure(
            self.inst["id"], self.inst, {"reason_code": "egress_unavailable"},
            35 * 3600, failed_probe=context), main.failover.HOLD)
        self.reselect.assert_not_called()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_ordinary_engine_start_errors_do_not_probe_or_switch(self):
        with patch.object(main, "_start_engine_checked", side_effect=RuntimeError("Docker error")):
            await self.recover()
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_pin_or_hardware_errors_do_not_probe_or_switch(self):
        for code in ("pin_invalid", "hardware_imei_required", "card_mismatch"):
            with patch.object(main, "_start_engine_checked", side_effect=main.HTTPException(
                    409, {"code": code})):
                await self.recover()
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_exhausted_pool_backs_off_instead_of_retrying_every_two_minutes(self):
        for _ in range(3):
            await self.recover()
        self.state["exits"]["us"].update(node="node-b", node_tag="exit-us-1")
        for _ in range(3):
            await self.recover()
        self.assertEqual(self.reselect.call_count, 1)
        self.assertTrue(main.hub.exit_ledgers[self.inst["id"]]["exhausted"])
        self.assertGreater(self.health["next_retry_at"] - time.monotonic(), 3500)
        failed = self.lifecycle.call_args_list[-1]
        self.assertEqual(failed.kwargs["delay_seconds"], 3600)

    async def test_probe_failure_itself_does_not_cancel_normal_recovery(self):
        self.probe.side_effect = RuntimeError("unexpected probe error")
        await self.recover()
        self.assertGreater(self.health["next_retry_at"], time.monotonic())
        self.reselect.assert_not_called()

    async def test_user_stop_during_probe_cancels_attribution_and_rescheduling(self):
        def user_stop(*_args, **_kwargs):
            main.hub.reset_health(self.inst["id"], "user_requested")
            raise egress.EgressError("old probe failed")
        self.probe.side_effect = user_stop
        await self.recover()
        self.reselect.assert_not_called()
        self.assertIsNone(main.hub.health_for(self.inst["id"])["next_retry_at"])
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_direct_and_single_node_routes_are_not_probed(self):
        self.state["exits"]["us"]["mode"] = "direct"
        await self.recover()
        self.state["exits"]["us"]["mode"] = "subscription"
        self.state["exits"]["us"]["candidates"] = ["node-a"]
        await self.recover()
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_request_publication_keeps_the_verified_node_and_revision(self):
        context = egress.exit_probe_context(self.inst, self.settings)
        written = {}
        read_json = egress._read_json
        with patch.object(egress, "_read_json", side_effect=lambda path:
                          {} if path == egress._RESELECT else read_json(path)), \
                patch.object(egress, "_atomic_json", side_effect=lambda _p, value: written.update(value)):
            self.assertEqual(self.real_reselect(
                self.inst, "verified-failure", probe_context=context), "us")
        request = written["countries"]["us"]
        self.assertEqual(request["node"], "node-a")
        self.assertEqual(request["expected_node"], "node-a")
        self.assertEqual(request["config_revision"], context["assignment_revision"])

    async def test_request_publication_discards_a_changed_selector(self):
        context = egress.exit_probe_context(self.inst, self.settings)
        self.state["exits"]["us"].update(node="node-b", node_tag="exit-us-1")
        with patch.object(egress, "_atomic_json") as write:
            self.assertEqual(self.real_reselect(
                self.inst, "old-probe", probe_context=context), "")
        write.assert_not_called()

    async def test_deleted_line_during_probe_cannot_publish_a_reselect_request(self):
        with patch.object(main.cfg, "get_instance", return_value=None):
            await self.recover()
        self.reselect.assert_not_called()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_changed_line_country_during_probe_discards_old_evidence(self):
        def change_line(*_args, **_kwargs):
            self.inst["proxy_country"] = "gb"
            raise egress.EgressError("old country probe failed")
        self.probe.side_effect = change_line
        await self.recover()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)
        self.reselect.assert_not_called()

    async def test_successful_probe_does_not_clear_a_different_configuration_ledger(self):
        main.hub.exit_ledgers[self.inst["id"]] = {
            "node": "node-a", "strikes": 2, "config_revision": "old"}
        self.probe.side_effect = None
        self.probe.return_value = 30
        await self.recover()
        self.assertEqual(main.hub.exit_ledgers[self.inst["id"]]["strikes"], 2)
        self.reselect.assert_not_called()

    async def test_local_binding_fault_does_not_blame_exit_after_probe(self):
        with patch.object(main, "_local_card_fault", return_value="wrong card"):
            for _ in range(3):
                await self.recover()
        self.reselect.assert_not_called()
        self.assertNotIn(self.inst["id"], main.hub.exit_ledgers)

    async def test_native_listener_and_applied_revision_are_required(self):
        if self.transport != "host":
            return
        selected = self.state["exits"]["us"]
        for patch_values in ({"proxy_host": "172.17.0.1"},
                             {"config_revision": "old"},
                             {"proxy_port": "22538"}):
            previous = copy.deepcopy(selected)
            selected.update(patch_values)
            await self.recover()
            selected.clear()
            selected.update(previous)
        self.probe.assert_not_called()
        self.reselect.assert_not_called()

    async def test_native_start_failure_probes_actual_loopback_listener(self):
        if self.transport != "host":
            return
        await self.recover()
        self.probe.assert_called_once_with("127.0.0.1", 22538, timeout=8.0)


class ContainerFailedStartFailoverTests(FailedStartFailoverTests):
    transport = "socks5"


class VerifiedHostReselectTests(unittest.TestCase):
    def prepare(self, directory):
        app = orch.Orchestrator(Path(directory), Path(directory), dry_run=True)
        app.singbox_started_at = 0
        app.rank_and_select = Mock(return_value="exit-us-1")
        app.record_exit_node = Mock()
        revision = "current-assignment"
        state = {"us": {"ready": True, "mode": "subscription", "selection": "managed",
                        "node": "", "config_revision": revision,
                        "_member_names": {"exit-us-0": "node-a", "exit-us-1": "node-b"}}}
        request = {"ts": time.time(), "reason": "health-freeze:egress_unavailable",
                   "node": "node-a", "line": "probe-line",
                   "expected_node": "node-a", "config_revision": revision}
        app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
        app.reselect_path.write_text(json.dumps({"countries": {"us": request}}))
        return app, state, request

    def test_matching_live_selector_consumes_the_verified_request_once(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, request = self.prepare(directory)
            with patch.object(orch.urllib.request, "urlopen",
                              return_value=io.BytesIO(b'{"now":"exit-us-0"}')):
                app.process_reselect_requests(state)
                app.process_reselect_requests(state)
            app.rank_and_select.assert_called_once_with(
                "us", state["us"], prefer="", avoid="node-a")
            self.assertEqual(app.handled_reselect["us"], request["ts"])
            self.assertEqual(state["us"]["node"], "node-b")

    def test_a_new_assignment_discards_old_probe_without_touching_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, request = self.prepare(directory)
            state["us"]["config_revision"] = "replacement-assignment"
            with patch.object(orch.urllib.request, "urlopen") as read:
                app.process_reselect_requests(state)
            read.assert_not_called()
            app.rank_and_select.assert_not_called()
            self.assertEqual(app.handled_reselect["us"], request["ts"])
            restarted = orch.Orchestrator(Path(directory), Path(directory), dry_run=True)
            self.assertEqual(restarted.handled_reselect["us"], request["ts"])

    def test_a_changed_live_node_is_not_cooled_or_reselected(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, request = self.prepare(directory)
            with patch.object(orch.urllib.request, "urlopen",
                              return_value=io.BytesIO(b'{"now":"exit-us-1"}')):
                app.process_reselect_requests(state)
            app.rank_and_select.assert_not_called()
            self.assertEqual(app.handled_reselect["us"], request["ts"])
            self.assertEqual(app.exit_cooldown, {})

    def test_unavailable_selector_leaves_request_pending_until_live_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            app, state, request = self.prepare(directory)
            with patch.object(orch.urllib.request, "urlopen", side_effect=OSError("not ready")):
                app.process_reselect_requests(state)
            app.rank_and_select.assert_not_called()
            self.assertNotIn("us", app.handled_reselect)
            with patch.object(orch.urllib.request, "urlopen",
                              return_value=io.BytesIO(b'{"now":"exit-us-0"}')):
                app.process_reselect_requests(state)
            app.rank_and_select.assert_called_once()
            self.assertEqual(app.handled_reselect["us"], request["ts"])


if __name__ == "__main__":
    unittest.main()
