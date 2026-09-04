import unittest
from unittest.mock import AsyncMock, patch

from control.app import lpa
from control.app.lpa import LpaError


class LpaErrorMessageTests(unittest.TestCase):
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
            "reader", all_notifications=True, autoremove=True, aid="aid")
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


if __name__ == "__main__":
    unittest.main()
