"""Do not turn an unreadable subscription into a false PIN terminal state."""
import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge_identity_fixture import verified_bridge
from control.app import esim_recovery, lpa, main, sim


class PinEvidenceTests(unittest.TestCase):
    def test_retry_counter_does_not_prove_pin_needed_on_other_imsi_errors(self):
        for sw, required in (((0x69, 0x85), None), ((0x6A, 0x82), None), ((0x69, 0x82), True)):
            with self.subTest(sw=sw):
                connection = Mock()
                reader = SimpleNamespace(createConnection=lambda: connection)
                def binary(conn, fid, length):
                    if fid == '2fe2':
                        return [], 0x6A, 0x82
                    if fid == '6f07':
                        return [], *sw
                    return [], 0x6A, 0x82
                with patch.object(sim, 'readers', return_value=[reader]), \
                        patch.object(sim.usbreader, 'port_for_index', return_value=None), \
                        patch.object(sim, '_Tx', side_effect=lambda conn: contextlib.nullcontext(conn)), \
                        patch.object(sim, '_transmit', return_value=([], 0x90, 0x00)), \
                        patch.object(sim, '_select_adf_usim', return_value=True), \
                        patch.object(sim, '_pin_tries', return_value=3), \
                        patch.object(sim, '_read_binary', side_effect=binary), \
                        patch.object(sim, '_read_transparent', return_value=None), \
                        patch.object(sim, '_read_smsc', return_value=None):
                    result = sim.read_card(0)
                self.assertIs(result.pin_enabled, required)
                self.assertEqual(result.pin_tries, 3)
                self.assertIsNone(result.imsi)
                connection.transmit.assert_not_called()  # No PIN VERIFY attempt.


class SubscriptionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreadable_subscription_preserves_backoff_instead_of_resetting_it(self):
        card = SimpleNamespace(reader='reader', reader_index=0, iccid='fixture-card',
                               imsi=None, mcc=None, mnc=None, smsc=None, pin_enabled=None,
                               pin_tries=3, error='IMSI read failed sw=6985')
        with patch.object(main.hub, 'cards', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=None), \
                patch.object(main, '_match_instance_by_iccid', return_value=None), \
                patch.object(main, '_modem_identity_for_reader', return_value={}), \
                patch.object(main.sim, 'read_card_bounded', return_value=card):
            for count in range(1, 4):
                await main._on_card_insert_locked('reader', 0, verify=True)
                row = main.hub.cards['reader']
                self.assertEqual(row['identity_state'], 'pending')
                self.assertEqual(row['identity_reason'], 'subscription_unreadable')
                self.assertEqual(row['identity_attempts'], count)


class FailedOutcomeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = esim_recovery.RecoveryStore(str(Path(directory) / 'tasks.json'))
        self.enterContext(patch.object(main, 'esim_recoveries', self.store))
        self.enterContext(patch.object(main.hub, 'reader_locks', {}))
        self.enterContext(patch.object(main.hub, 'esim_switch_locks', {}))
        self.enterContext(patch.object(main.hub, 'lpa_busy', {}))
        self.enterContext(patch.object(main.hub, 'broadcast', new=AsyncMock()))
        self.enterContext(patch.object(main, '_esim_cache_update_profile'))
        self.enterContext(patch.object(main, '_esim_guard_engine'))
        self.enterContext(patch.object(main, '_esim_modem_reader_names', return_value=['reader']))
        self.observed = {'devices': {'modem-a': {'present': True, 'usb_generation': 'generation-a',
                         'desired': {'flight_mode': True}, 'cellular_recovery': {'state': 'waiting_flight_mode'}}}}
        self.enterContext(patch.object(main.device_state, 'status', side_effect=lambda: self.observed))
        self.identity = {**verified_bridge(), 'iccid': 'target-card'}
        self.enterContext(patch.object(main, '_device_identities', side_effect=lambda: {'modem-a': self.identity}))

    def failed(self, code='profile_enable_failed'):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        return self.store.update(task['id'], 'cancelled', phase='profile_enable', error_code=code,
                                 se_id='default')

    async def test_latest_failed_enable_is_reconciled_once_without_replaying_write(self):
        old = self.failed()
        with patch.object(lpa, 'profile_list', new=AsyncMock(return_value=[
                {'iccid': 'target-card', 'profileState': 'enabled'}])) as listing, \
                patch.object(lpa, 'profile_enable', new=AsyncMock()) as write:
            await main._reconcile_failed_profile_outcomes()
            await main._reconcile_failed_profile_outcomes()
        listing.assert_awaited_once()
        write.assert_not_awaited()
        active = self.store.active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['state'], 'profile_enabled')
        self.assertEqual(active[0]['resumed_from'], old['id'])
        self.assertEqual(self.store.update(old['id'])['state'], 'cancelled')

    async def test_user_cancellation_replacement_generation_and_wrong_profile_are_not_recovered(self):
        for case in ('user_cancel', 'new_usb', 'different_card', 'wrong_profile'):
            with self.subTest(case=case):
                old = self.failed('user_network_selection' if case == 'user_cancel' else 'profile_enable_failed')
                self.observed['devices']['modem-a']['usb_generation'] = 'new' if case == 'new_usb' else 'generation-a'
                self.identity['iccid'] = 'other-card' if case == 'different_card' else 'target-card'
                with patch.object(lpa, 'profile_list', new=AsyncMock(return_value=[
                        {'iccid': 'other-card' if case == 'wrong_profile' else 'target-card',
                         'profileState': 'enabled'}])) as listing:
                    await main._reconcile_failed_profile_outcomes()
                self.assertEqual(self.store.latest('modem-a')['id'], old['id'])
                self.assertEqual(self.store.active(), [])
                if case != 'wrong_profile':
                    listing.assert_not_awaited()

    async def test_usb_replacement_during_profile_read_discards_success(self):
        old = self.failed()
        async def listing(*args, **kwargs):
            self.observed['devices']['modem-a']['usb_generation'] = 'new'
            return [{'iccid': 'target-card', 'profileState': 'enabled'}]
        with patch.object(lpa, 'profile_list', new=listing):
            await main._reconcile_failed_profile_outcomes()
        self.assertEqual(self.store.latest('modem-a')['id'], old['id'])
        self.assertEqual(self.store.active(), [])

    async def test_unreadable_subscription_keeps_existing_flight_pause_without_bridge_loop(self):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        self.store.update(task['id'], 'waiting_flight_mode', bridge_request_id='request')
        error = main.HTTPException(409, {'code': 'subscription_unavailable'})
        self.assertTrue(await main._defer_unreadable_subscription(task['id'], error))
        deferred = self.store.latest('modem-a')
        self.assertEqual(deferred['state'], 'waiting_flight_mode')
        self.assertEqual(deferred['deadline_at'], 0)
        with patch.object(main, '_resume_persisted_esim_bridge', new=AsyncMock()) as resume:
            await main._advance_esim_cellular_recovery(deferred)
            await main._advance_esim_cellular_recovery(deferred)
        resume.assert_not_awaited()

    async def test_ready_host_allows_local_identity_recovery_after_flight_is_disabled(self):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        task = self.store.update(task['id'], 'waiting_flight_mode', bridge_request_id='request')
        self.observed['devices']['modem-a'].update(desired={'flight_mode': False},
                                                  cellular_recovery={'state': 'ready'})
        with patch.object(main, '_resume_persisted_esim_bridge', new=AsyncMock()) as resume:
            await main._advance_esim_cellular_recovery(task)
        resume.assert_awaited_once()
