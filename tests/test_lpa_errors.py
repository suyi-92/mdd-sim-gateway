import unittest
from unittest.mock import ANY, AsyncMock, patch

from control.app import lpa
from control.app.lpa import LpaError


class LpaErrorMessageTests(unittest.TestCase):
    def test_pcsc_sharing_violation_is_not_misreported_as_non_euicc(self):
        message = LpaError("euicc_init", detail="SCardConnect() failed: 8010000B").user_message()
        self.assertIn("temporarily busy", message)
        self.assertNotIn("not appear to be an eUICC", message)

    def test_duplicate_profile_is_reported_as_already_installed(self):
        error = LpaError(
            "es10b_load_bound_profile_package",
            detail="store_metadata,install_failed_due_to_iccid_already_exists_on_euicc",
        )

        message = error.user_message()

        self.assertIn("already installed", message)
        self.assertIn("Refresh the profile list", message)
        self.assertNotIn("store_metadata", message)


class NotificationProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_auto_process_runs_the_cache_callback(self):
        callback = AsyncMock()
        with patch.object(lpa, "auto_process_notifications", return_value=True), \
                patch.object(lpa, "notification_process", new=AsyncMock()) as process:
            result = await lpa.maybe_process_notifications(
                "reader", aid="aid", on_processed=callback)

        self.assertTrue(result)
        process.assert_awaited_once_with(
            "reader", all_notifications=True, autoremove=True, aid="aid", timeout=ANY)
        self.assertTrue(0 < process.await_args.kwargs["timeout"] <= 45)
        callback.assert_awaited_once_with()

    async def test_failed_auto_process_keeps_the_cached_notification(self):
        callback = AsyncMock()
        failure = LpaError("es9p_handle_notification", detail="network unavailable")
        with patch.object(lpa, "auto_process_notifications", return_value=True), \
                patch.object(lpa, "notification_process",
                             new=AsyncMock(side_effect=failure)):
            result = await lpa.maybe_process_notifications(
                "reader", on_processed=callback)

        self.assertFalse(result)
        callback.assert_not_awaited()

    async def test_busy_reader_retries_then_reports_processed(self):
        busy = LpaError("euicc_init", detail="Unknown error: 0xFFFFFFFF8010000B")
        callback, status = AsyncMock(), AsyncMock()
        with patch.object(lpa, "auto_process_notifications", return_value=True), \
                patch.object(lpa.asyncio, "sleep", new=AsyncMock()), \
                patch.object(lpa, "notification_process", new=AsyncMock(side_effect=[busy, busy, None])) as process:
            self.assertTrue(await lpa.maybe_process_notifications(
                "reader", on_processed=callback, on_status=status))
        self.assertEqual(process.await_count, 3)
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 45 for call in process.await_args_list))
        callback.assert_awaited_once()
        self.assertEqual(status.await_args.args[0], {"state": "processed", "attempts": 3, "reason_code": ""})

    async def test_failure_is_bounded_and_classified_without_raw_error(self):
        for detail, count, code in (("SCARD_E_SHARING_VIOLATION", 3, "reader_busy"),
                                    ("private endpoint unavailable", 1, "notification_failed")):
            with self.subTest(code=code):
                callback, status = AsyncMock(), AsyncMock()
                with patch.object(lpa, "auto_process_notifications", return_value=True), \
                        patch.object(lpa.asyncio, "sleep", new=AsyncMock()), \
                        patch.object(lpa, "notification_process", new=AsyncMock(
                            side_effect=LpaError("euicc_init", detail=detail))) as process:
                    self.assertFalse(await lpa.maybe_process_notifications(
                        "reader", on_processed=callback, on_status=status))
                self.assertEqual(process.await_count, count)
                callback.assert_not_awaited()
                self.assertEqual(status.await_args.args[0], {
                    "state": "failed", "attempts": count, "reason_code": code})

    async def test_deferred_enable_does_not_touch_notification_reader(self):
        with patch.object(lpa, "run_lpac", new=AsyncMock(return_value=lpa.LpaResult(data={}))), \
                patch.object(lpa, "maybe_process_notifications", new=AsyncMock()) as process:
            await lpa.profile_enable("reader", "fixture-card", process_notifications=False)
        process.assert_not_awaited()

    async def test_timeout_and_disabled_notifications_have_explicit_outcomes(self):
        status = AsyncMock()
        with patch.object(lpa, "auto_process_notifications", return_value=True), \
                patch.object(lpa, "notification_process", new=AsyncMock(
                    side_effect=LpaError("lpac timed out"))) as process:
            self.assertFalse(await lpa.maybe_process_notifications("reader", on_status=status))
        self.assertEqual(status.await_args.args[0]["reason_code"], "notification_timeout")
        process.assert_awaited_once()
        with patch.object(lpa, "auto_process_notifications", return_value=False), \
                patch.object(lpa, "notification_process", new=AsyncMock()) as process:
            self.assertFalse(await lpa.maybe_process_notifications("reader", on_status=status))
        process.assert_not_awaited()
        self.assertEqual(status.await_args.args[0]["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
