import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import esim_operations, main


class DownloadOperationStoreTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "operations.json"
        self.store = esim_operations.DownloadOperations(str(self.path))

    def test_reader_progress_survives_reload_without_raw_identity_or_data(self):
        operation = self.store.start("Fixture private reader", 3)
        self.store.update(operation["operation_id"], "progress",
                          step="es10b_prepare_download:result")
        reopened = esim_operations.DownloadOperations(str(self.path))
        self.assertEqual(reopened.latest("Fixture private reader")["step"],
                         "es10b_prepare_download")
        self.assertIsNone(reopened.latest("different reader"))
        self.assertNotIn("Fixture private reader", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store.update(operation["operation_id"], "progress", step="private activation secret")
        self.store.update(operation["operation_id"], "error", error_code="private error detail")
        self.assertNotIn("private", self.path.read_text())
        self.assertEqual(self.store.latest("Fixture private reader")["error_code"], "unknown_error")

    def test_restart_marks_unfinished_download_interrupted_but_keeps_completed(self):
        pending = self.store.start("reader-a")
        complete = self.store.start("reader-b")
        self.store.update(complete["operation_id"], "completed")
        reopened = esim_operations.DownloadOperations(str(self.path))
        reopened.interrupt_running()
        self.assertEqual(reopened.latest("reader-a")["operation_id"], pending["operation_id"])
        self.assertEqual(reopened.latest("reader-a")["state"], "failed")
        self.assertEqual(reopened.latest("reader-a")["error_code"], "interrupted")
        self.assertEqual(reopened.latest("reader-b")["state"], "success")

    def test_cancel_pending_does_not_become_running_again_and_old_result_cannot_overwrite(self):
        operation = self.store.start("reader")
        with self.assertRaises(esim_operations.DownloadBusy):
            self.store.start("reader")
        self.store.update(operation["operation_id"], "cancelling")
        self.store.update(operation["operation_id"], "progress", step="es10b_prepare_download")
        self.assertEqual(self.store.latest("reader")["state"], "cancelling")
        self.store.update(operation["operation_id"], "error", error_code="interrupted")
        self.assertEqual(self.store.latest("reader")["state"], "cancelled")
        self.assertEqual(self.store.latest("reader")["error_code"], "cancelled")
        newer = self.store.start("reader")
        self.assertIsNone(self.store.update(operation["operation_id"], "completed"))
        self.assertEqual(self.store.latest("reader")["operation_id"], newer["operation_id"])

    def test_loaded_records_are_filtered_before_query(self):
        operation = self.store.start("reader")
        document = json.loads(self.path.read_text())
        record = next(iter(document["records"].values()))
        record.update(activation_code="fixture activation", metadata={"private": "fixture"},
                      error_code=["malformed"], step={"malformed": True})
        self.path.write_text(json.dumps(document))
        result = self.store.latest("reader")
        self.assertEqual(result["operation_id"], operation["operation_id"])
        self.assertNotIn("activation_code", result)
        self.assertNotIn("metadata", result)
        self.assertNotIn("error_code", result)
        self.assertNotIn("step", result)

    def test_confirmed_refresh_can_advance_the_safe_card_generation(self):
        operation = self.store.start("reader", 7)
        finished = self.store.update(
            operation["operation_id"], "completed", generation=8)
        self.assertEqual(finished["generation"], 8)
        self.assertEqual(self.store.latest("reader")["generation"], 8)

    def test_private_card_fence_survives_restart_and_rejects_a_replacement(self):
        operation = self.store.start("reader", 7, "fixture-card-a")
        reopened = esim_operations.DownloadOperations(str(self.path))
        self.assertEqual(
            reopened.latest("reader", "fixture-card-a", True)["operation_id"],
            operation["operation_id"])
        self.assertIsNone(reopened.latest("reader", "fixture-card-b", True))
        stored = self.path.read_text()
        self.assertNotIn("fixture-card-a", stored)
        self.assertNotIn("card_key", str(reopened.latest("reader", "fixture-card-a")))

        legacy = self.store.start("reader-without-identity", 1)
        self.assertIsNotNone(legacy)
        self.assertIsNone(self.store.latest(
            "reader-without-identity", "fixture-card-a", True))


class DownloadOperationApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "operations.json"
        self.store = esim_operations.DownloadOperations(str(self.path))
        self.enterContext(patch.object(main, "esim_download_operations", self.store))
        self.enterContext(patch.dict(main.hub.lpa_busy, {}, clear=True))
        self.enterContext(patch.dict(main.hub.cards, {
            "fixture reader": {"generation": 7, "iccid": "fixture-card"}}, clear=True))
        self.enterContext(patch.object(main, "_esim_resolve_reader", return_value=("fixture reader", 0)))
        self.enterContext(patch.object(main, "_esim_resolve_se", return_value={"id": "default"}))
        self.enterContext(patch.object(main, "_esim_guard_engine"))
        self.enterContext(patch.object(main, "_esim_imei_for_reader", return_value=""))
        self.enterContext(patch.object(main, "_esim_refresh_card", new=AsyncMock()))
        self.broadcast = self.enterContext(patch.object(main.hub, "broadcast", new=AsyncMock()))

    async def asyncTearDown(self):
        pending = list(main.esim_download_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def test_download_returns_id_and_query_replays_progress_and_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            await kwargs["on_progress"]({"step": "es8p_metadata_parse",
                                         "data": {"private": "fixture metadata"}})
            entered.set()
            await release.wait()
            return {"private": "fixture result"}

        with patch.object(main.lpa, "download", side_effect=download):
            reply = await main.api_esim_download({"activation_code": "fixture activation",
                                                  "confirmation_code": "fixture confirmation"})
            await asyncio.wait_for(entered.wait(), 2)
            progress = (await main.api_esim_download_operation())["operation"]
            self.assertEqual(progress["operation_id"], reply["operation_id"])
            self.assertEqual(progress["step"], "es8p_metadata_parse")
            self.assertEqual(progress["state"], "running")
            release.set()
            await asyncio.gather(*list(main.esim_download_tasks))
        self.assertEqual((await main.api_esim_download_operation())["operation"]["state"], "success")
        stored = self.path.read_text()
        for secret in ("fixture reader", "fixture activation", "fixture confirmation",
                       "fixture metadata", "fixture result"):
            self.assertNotIn(secret, stored)
        download_events = [call.args[0] for call in self.broadcast.await_args_list]
        self.assertTrue(all(event["operation_id"] == reply["operation_id"]
                            for event in download_events))

    async def test_cancel_awaits_lpac_signal_and_records_cancelling(self):
        operation = self.store.start("fixture reader")
        with patch.object(main.lpa, "cancel_download", new=AsyncMock(return_value=True)) as cancel:
            result = await main.api_esim_download_cancel({"reader": "fixture reader"})
        cancel.assert_awaited_once_with("fixture reader")
        self.assertIs(result["cancelled"], True)
        self.assertEqual(self.store.latest("fixture reader")["state"], "cancelling")
        self.assertEqual(self.broadcast.await_args.args[0]["operation_id"], operation["operation_id"])

    async def test_success_tracks_the_confirmed_post_refresh_generation(self):
        async def refresh(_name, _idx):
            main.hub.cards["fixture reader"]["generation"] = 8

        with patch.object(main, "_esim_refresh_card", new=AsyncMock(side_effect=refresh)), \
                patch.object(main.lpa, "download", new=AsyncMock(return_value={})):
            await main.api_esim_download({})
            await asyncio.gather(*list(main.esim_download_tasks))

        operation = (await main.api_esim_download_operation())[
            "operation"]
        self.assertEqual(operation["state"], "success")
        self.assertEqual(operation["generation"], 8)

    async def test_cancel_without_process_does_not_change_completed_result(self):
        operation = self.store.start("fixture reader")
        self.store.update(operation["operation_id"], "completed")
        with patch.object(main.lpa, "cancel_download", new=AsyncMock(return_value=False)):
            result = await main.api_esim_download_cancel()
        self.assertIs(result["cancelled"], False)
        self.assertEqual(self.store.latest("fixture reader")["state"], "success")
        self.broadcast.assert_not_awaited()

    async def test_error_is_replayed_as_closed_code_without_original_message(self):
        error = main.lpa.LpaError("es9p_authenticate_client", detail="private fixture transport URL",
                                  category="network_transport")
        with patch.object(main.lpa, "download", new=AsyncMock(side_effect=error)):
            await main.api_esim_download({})
            await asyncio.gather(*list(main.esim_download_tasks))
        operation = (await main.api_esim_download_operation())["operation"]
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error_code"], "network_transport")
        self.assertNotIn("private fixture", self.path.read_text())

    async def test_worker_cancellation_is_terminal_and_releases_reader(self):
        entered = asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(main.lpa, "download", side_effect=download):
            await main.api_esim_download({})
            await asyncio.wait_for(entered.wait(), 2)
            tasks = list(main.esim_download_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        operation = (await main.api_esim_download_operation())["operation"]
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error_code"], "interrupted")
        self.assertNotIn("fixture reader", main.hub.lpa_busy)


if __name__ == "__main__":
    unittest.main()
