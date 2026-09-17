import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from control.app import cellular_operations, main


KEY = ("modem-fixture", "line-fixture", "private-card-fixture")
NETWORK = {"operator_id": "00101", "name": "Fixture Mobile",
           "access_technology": "lte", "status": "available"}


class NetworkOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_has_a_budget_and_esim_reset_rejects_live_jobs(self):
        state = cellular_operations.NetworkOperations()
        finish = asyncio.Event()
        async def worker():
            await finish.wait()
            return {}
        first = state.start(KEY, "apply", {"mode": "manual", "operator_id": "00101"}, worker)
        operation_id = first["operation"]["id"]
        state.progress(KEY, operation_id, "restoring")
        view = state.view(KEY)
        self.assertEqual(view["operation"]["phase"], "restoring")
        self.assertLessEqual(view["operation"]["remaining_seconds"], 180)
        with self.assertRaises(cellular_operations.OperationBusy):
            state.reset(KEY)
        finish.set()
        await asyncio.gather(*state.tasks)
        fresh = state.reset(KEY)
        self.assertNotEqual(fresh["context"], first["context"])
        self.assertTrue(fresh["selection_reset"])
        self.assertIsNone(fresh["operation"])
        state.progress(KEY, operation_id, "registering")
        self.assertIsNone(state.view(KEY)["operation"])

    async def test_job_outlives_request_and_keeps_names_for_later_readers(self):
        state = cellular_operations.NetworkOperations()
        done = asyncio.Event()

        async def worker():
            await done.wait()
            return {"networks": [NETWORK]}

        reply = state.start(KEY, "scan", {}, worker)
        self.assertEqual(reply["operation"]["state"], "running")
        self.assertNotIn(KEY[2], json.dumps(reply))
        with self.assertRaises(cellular_operations.OperationBusy):
            state.start(KEY, "apply", {}, worker)
        self.assertTrue(state.view(("other", "line", "card"))["blocked"])
        done.set()
        await asyncio.gather(*state.tasks)
        result = state.view(KEY)
        self.assertEqual(result["networks"], [NETWORK])
        self.assertEqual(result["operation"]["state"], "success")
        self.assertFalse(state.busy())

    async def test_partial_scan_keeps_results_and_recovery_warning(self):
        state = cellular_operations.NetworkOperations()

        async def worker():
            raise main.HTTPException(503, {"code": "scan_recovery", "networks": [NETWORK],
                                           "recovery": {"state": "pending"}})

        state.start(KEY, "scan", {}, worker)
        await asyncio.gather(*state.tasks)
        result = state.view(KEY)
        self.assertEqual(result["operation"]["state"], "partial")
        self.assertEqual(result["networks"], [NETWORK])
        other = state.view((KEY[0], "replacement", "new-card"))
        self.assertNotEqual(other["context"], result["context"])
        self.assertEqual(other["networks"], [])

    async def test_unexpected_failure_is_redacted_and_releases_admission(self):
        state = cellular_operations.NetworkOperations()

        async def worker():
            raise RuntimeError("private diagnostic payload")

        state.start(KEY, "scan", {}, worker)
        await asyncio.gather(*state.tasks)
        result = state.view(KEY)
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertEqual(result["operation"]["error"], {"code": "scan_failed"})
        self.assertFalse(state.busy())

    async def test_cache_is_bounded_without_evicting_running_job(self):
        state = cellular_operations.NetworkOperations(limit=2)
        done = asyncio.Event()

        async def worker():
            await done.wait()
            return {}

        state.start(KEY, "apply", {}, worker)
        for number in range(10):
            state.view((str(number), "line", "card"))
        self.assertEqual(len(state.records), 2)
        self.assertIn(KEY, state.records)
        done.set()
        await asyncio.gather(*state.tasks)


class NetworkOperationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_api_returns_before_radio_and_rejects_rescan(self):
        state = cellular_operations.NetworkOperations()
        done = asyncio.Event()
        observed = {"present": True, "cellular": {"sim_iccid": KEY[2]}}

        async def worker(device_id):
            await done.wait()
            return {"device_id": device_id, "networks": [NETWORK]}

        with patch.object(main, "network_operations", state), \
                patch.object(main, "_cellular_network_target", return_value=(observed, {"id": KEY[1]}, "/modem")), \
                patch.object(main, "_scan_cellular_network", worker), \
                patch.object(main.operations, "request_device_rescan") as rescan:
            result = await main.api_device_cellular_network_scan(KEY[0], background=True)
            self.assertEqual(result["operation"]["state"], "running")
            for action in [lambda: main.api_device_cellular_network_scan(KEY[0], background=True),
                           lambda: main.api_device_cellular_network_scan(KEY[0]),
                           lambda: main.api_devices_rescan()]:
                with self.assertRaises(main.HTTPException) as caught:
                    await action()
                self.assertEqual(caught.exception.status_code, 409)
            rescan.assert_not_called()
            done.set()
            await asyncio.gather(*state.tasks)
        self.assertEqual(state.view(KEY)["networks"], [NETWORK])

    async def test_binding_change_prevents_late_configuration_write(self):
        modem = "/org/freedesktop/ModemManager1/Modem/7"
        observed = {"present": True, "mm_object": modem,
                    "cellular": {"available": True, "sim_present": True, "sim_iccid": "old-card"}}
        changed = {**observed, "cellular": {**observed["cellular"], "sim_iccid": "new-card"}}
        with patch.object(main, "network_operations", cellular_operations.NetworkOperations()), \
                patch.object(main.operations, "device_rescan_status", return_value={}), \
                patch.object(main.device_state, "status", side_effect=[
                    {"devices": {KEY[0]: observed}}, {"devices": {KEY[0]: changed}}]), \
                patch.object(main, "_match_instance_by_iccid", return_value={"id": "line"}), \
                patch.object(main.cellular_network, "register", return_value={"mode": "manual", "operator_id": "00101"}), \
                patch.object(main.cfg, "upsert_instance") as save:
            with self.assertRaises(main.HTTPException) as caught:
                await main.api_device_cellular_network_select(KEY[0], {"mode": "manual", "operator_id": "00101"})
            self.assertEqual(caught.exception.detail, {"code": "device_changed"})
            save.assert_not_called()

    async def test_success_keeps_scanned_name_with_the_saved_operator(self):
        state = cellular_operations.NetworkOperations()
        observed = {"present": True, "cellular": {"sim_iccid": KEY[2]}}
        state._record(KEY)["networks"] = [NETWORK]
        with patch.object(main, "network_operations", state), \
                patch.object(main, "_cellular_network_target", return_value=(observed, {"id": KEY[1]}, "/modem")), \
                patch.object(main.device_state, "status", return_value={"devices": {KEY[0]: observed}}), \
                patch.object(main.cellular_network, "register", return_value={"mode": "manual", "operator_id": "00101"}), \
                patch.object(main.cfg, "upsert_instance") as save, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main.api_device_cellular_network_select(KEY[0], {"mode": "manual", "operator_id": "00101"})
        self.assertEqual(save.call_args.args[0]["cellular_operator_name"], "Fixture Mobile")
        self.assertEqual(save.call_args.args[0]["cellular_operator_technology"], "lte")

    async def test_transient_missing_card_snapshot_does_not_hide_running_job(self):
        state = cellular_operations.NetworkOperations()
        done = asyncio.Event()
        async def worker():
            await done.wait()
            return {}
        original = state.start(KEY, "scan", {}, worker)
        with patch.object(main, "network_operations", state), \
                patch.object(main.device_state, "status", return_value={"devices": {KEY[0]: {"present": True}}}):
            view = await main.api_device_cellular_network_operation(KEY[0])
        self.assertEqual(view["context"], original["context"])
        self.assertEqual(view["operation"]["state"], "running")
        done.set()
        await asyncio.gather(*state.tasks)
