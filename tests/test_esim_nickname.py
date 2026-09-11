import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import main


class EsimNicknameRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.names = [f"reader-{n}" for n in range(3)]
        self.steps = []
        self.usim_ready = True
        self.enterContext(patch.dict(main.hub.lpa_busy, {}, clear=True))
        self.enterContext(patch.dict(main.hub.esim_switch_locks, {}, clear=True))
        self.enterContext(patch.object(main, "_esim_resolve_reader", return_value=(self.names[0], 0)))
        self.identity = self.enterContext(patch.object(main, "_esim_switch_identity", return_value=("modem-fixture", "modem-fixture")))
        self.readers = self.enterContext(patch.object(main, "_esim_modem_reader_names", return_value=self.names))
        self.guard = self.enterContext(patch.object(main, "_esim_guard_engine"))
        self.card = self.enterContext(patch.object(main.sim, "read_iccid", return_value="active-profile"))
        self.resolve = self.enterContext(patch.object(main, "_esim_resolve_se", return_value={"id": "default", "aid": None}))
        self.nickname = self.enterContext(patch.object(main.lpa, "profile_nickname", new=AsyncMock(side_effect=self.write_nickname)))
        self.cache = self.enterContext(patch.object(main, "_esim_cache_update_profile", side_effect=lambda *a, **k: self.steps.append("cache")))
        self.bridge = self.enterContext(patch.object(main, "_esim_restart_modem_bridge", new=AsyncMock(side_effect=self.rebuild)))
        self.refresh = self.enterContext(patch.object(main, "_esim_refresh_modem_readers", new=AsyncMock(side_effect=self.verify)))
        self.enable = self.enterContext(patch.object(main.cfg, "upsert_instance"))

    async def write_nickname(self, *_args, **_kwargs):
        self.assertTrue(all(main.hub.lpa_busy.get(name) for name in self.names))
        self.usim_ready = False  # ISD-R remains selected after lpac's logical close.
        self.steps.append("nickname")

    async def rebuild(self, hardware, active):
        self.assertEqual((hardware, active), ("modem-fixture", "active-profile"))
        self.assertTrue(all(main.hub.lpa_busy.get(name) for name in self.names))
        self.steps.append("bridge")
        self.usim_ready = True

    async def verify(self, name, hardware, active):
        self.assertTrue(self.usim_ready)
        self.assertEqual(active, "active-profile")
        self.steps.append("verify_all_slots")
        return {}, self.names

    async def rename(self):
        return await main.api_esim_nickname("inactive-profile", {"reader": self.names[0], "nickname": "Fixture"})

    async def test_inactive_profile_rename_restores_active_usim_before_reply(self):
        result = await self.rename()
        self.assertEqual(self.steps, ["nickname", "cache", "bridge", "verify_all_slots"])
        self.assertTrue(result["reader_ready"])
        self.assertTrue(self.usim_ready)
        self.enable.assert_not_called()  # Do not enable the profile being renamed.
        self.cache.assert_called_once_with("inactive-profile", nickname="Fixture")
        self.assertEqual(main.hub.lpa_busy, {})

    async def test_lpa_error_still_recovers_channels_before_original_error(self):
        async def fail(*args, **kwargs):
            await self.write_nickname(*args, **kwargs)
            raise main.lpa.LpaError("fixture rejected")
        self.nickname.side_effect = fail
        with self.assertRaises(main.HTTPException) as error:
            await self.rename()
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.steps, ["nickname", "bridge", "verify_all_slots"])
        self.cache.assert_not_called()
        self.assertEqual(main.hub.lpa_busy, {})

    async def test_discovery_failure_also_recovers_its_reader_session(self):
        self.resolve.side_effect = main.HTTPException(400, "invalid SE")
        with self.assertRaises(main.HTTPException):
            await self.rename()
        self.assertEqual(self.steps, ["bridge", "verify_all_slots"])
        self.nickname.assert_not_awaited()

    async def test_recovery_failure_reports_saved_name_without_permitting_start(self):
        self.bridge.side_effect = RuntimeError("private generated configuration")
        result = await self.rename()
        self.assertTrue(result["ok"])
        self.assertEqual(result["nickname"], "Fixture")
        self.assertFalse(result["reader_ready"])
        self.assertIn("recovery_error", result)
        self.assertNotIn("private generated configuration", str(result))
        self.refresh.assert_not_awaited()
        self.assertEqual(main.hub.lpa_busy, {})

    async def test_wrong_identity_on_any_rebuilt_slot_keeps_start_blocked(self):
        self.refresh.side_effect = main.HTTPException(503, "wrong slot identity")
        result = await self.rename()
        self.assertFalse(result["reader_ready"])
        self.assertIn("recovery_error", result)
        self.enable.assert_not_called()

    async def test_failed_write_and_failed_recovery_explicitly_block_start(self):
        self.nickname.side_effect = main.lpa.LpaError("fixture rejected")
        self.bridge.side_effect = RuntimeError("private generated configuration")
        with self.assertRaises(main.HTTPException) as error:
            await self.rename()
        self.assertTrue(error.exception.detail["reader_recovery_failed"])
        self.assertEqual(error.exception.status_code, 503)
        self.cache.assert_not_called()
        self.assertEqual(main.hub.lpa_busy, {})

    async def test_running_sibling_blocks_all_card_access(self):
        def guard(name):
            if name == self.names[1]:
                raise main.HTTPException(409, "running sibling")
        self.guard.side_effect = guard
        with self.assertRaises(main.HTTPException):
            await self.rename()
        self.card.assert_not_called()
        self.resolve.assert_not_called()
        self.nickname.assert_not_awaited()
        self.bridge.assert_not_awaited()

    async def test_busy_sibling_preserves_existing_operation(self):
        main.hub.lpa_busy[self.names[2]] = True
        with self.assertRaises(main.HTTPException):
            await self.rename()
        self.assertEqual(main.hub.lpa_busy, {self.names[2]: True})
        self.card.assert_not_called()

    async def test_unreadable_active_identity_is_rejected_before_lpa(self):
        self.card.return_value = ""
        with self.assertRaises(main.HTTPException):
            await self.rename()
        self.nickname.assert_not_awaited()
        self.bridge.assert_not_awaited()
        self.assertEqual(main.hub.lpa_busy, {})

    async def test_native_reader_does_not_rebuild_a_modem(self):
        self.identity.return_value = ("native-reader", "")
        self.readers.return_value = self.names[:1]
        self.nickname.side_effect = None
        result = await self.rename()
        self.assertTrue(result["reader_ready"])
        self.card.assert_not_called()
        self.bridge.assert_not_awaited()

    async def test_second_rename_waits_until_all_slots_are_restored(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bridge.side_effect
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        self.bridge.side_effect = delayed
        first = asyncio.create_task(self.rename())
        await entered.wait()
        second = asyncio.create_task(self.rename())
        await asyncio.sleep(0)
        self.assertEqual(self.nickname.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.steps, ["nickname", "cache", "bridge", "verify_all_slots"] * 2)
