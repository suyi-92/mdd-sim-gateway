"""Durable eSIM cellular recovery tests; no hardware or production state is used."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import esim_recovery, main


class RecoveryStoreTests(unittest.TestCase):
    def test_flight_pause_survives_restart_and_resumes_with_one_bounded_budget(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(esim_recovery.time, 'time', return_value=1000) as clock:
            path = str(Path(temp) / 'recovery.json')
            store = esim_recovery.RecoveryStore(path, timeout=90)
            task = store.schedule('modem-a', 'fixture-card', 'reader', 'generation-a')
            store.update(task['id'], 'waiting_flight_mode')
            clock.return_value = 87400
            restarted = esim_recovery.RecoveryStore(path, timeout=90)
            paused = restarted.active()
            self.assertEqual([item['id'] for item in paused], [task['id']])
            self.assertEqual(paused[0]['deadline_at'], 0)
            self.assertEqual(paused[0]['hardware_generation'], 'generation-a')
            resumed = restarted.update(task['id'], 'waiting_baseband')
            self.assertEqual(resumed['deadline_at'], 87490)
            clock.return_value = 87480
            self.assertEqual(restarted.update(task['id'], 'registering')['deadline_at'], 87490)
            clock.return_value = 87491
            self.assertEqual(restarted.active(), [])
            self.assertEqual(restarted.latest('modem-a')['error_code'], 'recovery_timeout')

    def test_new_switch_supersedes_old_and_restart_keeps_current_task(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "recovery.json")
            store = esim_recovery.RecoveryStore(path)
            old = store.schedule("modem-a", "fixture-old", "reader")
            new = store.schedule("modem-a", "fixture-new", "reader")
            restarted = esim_recovery.RecoveryStore(path)
            self.assertEqual([task["id"] for task in restarted.active()], [new["id"]])
            self.assertEqual(store.update(old["id"])["state"], "cancelled")

    def test_user_selection_cancels_pending_automatic_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            store = esim_recovery.RecoveryStore(str(Path(temp) / "recovery.json"))
            task = store.schedule("modem-a", "fixture-card", "reader")
            self.assertTrue(store.cancel_device("modem-a", "user_network_selection"))
            value = store.update(task["id"])
            self.assertEqual(value["state"], "cancelled")
            self.assertEqual(value["error_code"], "user_network_selection")


class RecoveryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = esim_recovery.RecoveryStore(str(Path(temp) / "recovery.json"))
        self.enterContext(patch.object(main, "esim_recoveries", self.store))
        self.enterContext(patch.object(main.hub, "broadcast", new=AsyncMock()))
        self.enterContext(patch.object(main, "_esim_cache_update_profile"))

    @staticmethod
    def observed():
        return {"devices": {"modem-a": {
            "present": True, "usb_generation": "generation-a",
            "desired": {"flight_mode": False},
            "cellular_recovery": {"state": "ready"},
            "cellular": {"sim_iccid": "fixture-card"},
        }}}

    async def test_ready_baseband_starts_tracked_automatic_selection(self):
        task = self.store.schedule(
            "modem-a", "fixture-card", "reader", "generation-a")
        task = self.store.update(task["id"], "waiting_baseband",
                                 local_identity_verified=True)
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_device_identities", return_value={}), \
                patch.object(main.capability_lock, "locked", return_value=False), \
                patch.object(main.network_operations, "busy", return_value=False), \
                patch.object(main, "_esim_restore_cellular_selection",
                             new=AsyncMock(return_value={"operation": {"state": "running"}})) as start:
            await main._advance_esim_cellular_recovery(task)
        start.assert_awaited_once_with("modem-a", "fixture-card")
        self.assertEqual(self.store.update(task["id"])["state"], "registering")

    async def test_reason_seven_is_terminal_and_keeps_sim_readability_separate(self):
        task = self.store.schedule(
            "modem-a", "fixture-card", "reader", "generation-a")
        task = self.store.update(task["id"], "registering",
                                 local_identity_verified=True)
        operation = {"state": "failed", "error": {
            "code": "network_rejected", "network_reject": {
                "cause_code": 7, "cause": "ps-services-not-allowed",
                "rat": "lte", "service_domain": "ps", "operator_id": "",
                "observed_at": time.time(),
            }}}
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_device_identities", return_value={}), \
                patch.object(main, "_match_instance_by_iccid", return_value={
                    "id": "line", "iccid": "fixture-card"}), \
                patch.object(main.network_operations, "view", return_value={
                    "operation": operation}):
            await main._advance_esim_cellular_recovery(task)
        value = self.store.update(task["id"])
        self.assertEqual(value["state"], "network_rejected")
        self.assertEqual(value["network_reject"]["cause_code"], 7)

    async def test_current_bridge_for_another_profile_cancels_old_task(self):
        task = self.store.schedule(
            "modem-a", "fixture-card", "reader", "generation-a")
        task = self.store.update(task["id"], "waiting_baseband",
                                 local_identity_verified=True)
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_device_identities", return_value={
                    "modem-a": {"iccid": "replacement-card"}}), \
                patch.object(main, "_bridge_card_evidence", return_value=True):
            await main._advance_esim_cellular_recovery(task)
        self.assertEqual(self.store.update(task["id"])["error_code"], "profile_changed")

    async def test_paused_task_keeps_usb_generation_guard_after_old_deadline(self):
        with patch.object(esim_recovery.time, 'time', return_value=1000) as clock:
            task = self.store.schedule(
                'modem-a', 'fixture-card', 'reader', 'generation-a')
            self.store.update(task['id'], 'waiting_flight_mode',
                              local_identity_verified=True)
            clock.return_value = 87400
            task = self.store.active()[0]
            observed = self.observed()
            observed['devices']['modem-a']['usb_generation'] = 'generation-b'
            with patch.object(main.device_state, 'status', return_value=observed), \
                    patch.object(main, '_esim_restore_cellular_selection',
                                 new=AsyncMock()) as start:
                await main._advance_esim_cellular_recovery(task)
            start.assert_not_awaited()
            self.assertEqual(self.store.latest('modem-a')['error_code'],
                             'device_generation_changed')

    async def test_paused_task_resumes_with_new_budget_after_radio_is_allowed(self):
        with patch.object(esim_recovery.time, 'time', return_value=1000) as clock:
            task = self.store.schedule(
                'modem-a', 'fixture-card', 'reader', 'generation-a')
            self.store.update(task['id'], 'waiting_flight_mode',
                              local_identity_verified=True)
            clock.return_value = 87400
            with patch.object(main.device_state, 'status', return_value=self.observed()), \
                    patch.object(main, '_device_identities', return_value={}), \
                    patch.object(main.capability_lock, 'locked', return_value=False), \
                    patch.object(main.network_operations, 'busy', return_value=False), \
                    patch.object(main, '_esim_restore_cellular_selection', new=AsyncMock(
                        return_value={'operation': {'state': 'running'}})) as start:
                await main._advance_esim_cellular_recovery(self.store.active()[0])
            start.assert_awaited_once_with('modem-a', 'fixture-card')
            resumed = self.store.latest('modem-a')
            self.assertEqual(resumed['state'], 'registering')
            self.assertEqual(resumed['deadline_at'], 87400 + self.store.timeout)


if __name__ == "__main__":
    unittest.main()
