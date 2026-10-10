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
    async def test_verified_resolution_retains_the_original_command_diagnostic(self):
        status = {'state': 'failed', 'updated_at': 10, 'error': {'diagnostic': {'step': 'es10c_enable_profile', 'status_word': '6F00'}}}
        with patch.object(main, '_esim_cache_for_iccid', return_value={'ses': [{'profiles': [
                {'iccid': 'target-card', 'operation_status': status}]}]}), \
                patch.object(main, '_esim_cache_update_profile') as update:
            await main._esim_profile_operation_resolved('target-card')
        saved = update.call_args.kwargs['operation_status']
        self.assertEqual(saved['state'], 'resolved')
        self.assertEqual(saved['error'], status['error'])
        self.assertEqual(saved['failed_at'], 10)
        self.assertEqual(saved['resolution'], 'enabled_profile_verified')

    async def test_enable_confirmation_does_not_resolve_an_unrelated_command_failure(self):
        status = {'state': 'failed', 'error': {'diagnostic': {'step': 'es10c_delete_profile'}}}
        with patch.object(main, '_esim_cache_for_iccid', return_value={'ses': [{'profiles': [
                {'iccid': 'target-card', 'operation_status': status}]}]}), \
                patch.object(main, '_esim_cache_update_profile') as update:
            await main._esim_profile_operation_resolved('target-card')
        update.assert_not_called()

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
                         'desired': {'flight_mode': True}, 'cellular_recovery': {'state': 'waiting_flight_mode', 'operation_id': 'request'}}}}
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
                                                  cellular_recovery={'state': 'ready', 'operation_id': 'request'})
        with patch.object(main, '_resume_persisted_esim_bridge', new=AsyncMock()) as resume:
            await main._advance_esim_cellular_recovery(task)
        resume.assert_awaited_once()


    async def test_exhausted_legacy_read_starts_one_access_repair_without_claiming_enabled(self):
        old = self.failed()
        self.store.update(old['id'], outcome_checks=3)
        with patch.object(lpa, 'profile_list', new=AsyncMock()) as listing, \
                patch.object(lpa, 'profile_enable', new=AsyncMock()) as writing, \
                patch.object(main, '_esim_write_bridge_restart_request', return_value=('request', 'unused')) as request:
            await main._reconcile_failed_profile_outcomes()
            task = self.store.latest('modem-a')
            self.assertEqual(task['state'], 'access_recovery')
            self.assertTrue(task['profile_confirmation_pending'])
            await main._advance_esim_cellular_recovery(task)
            await main._advance_esim_cellular_recovery(self.store.latest('modem-a'))
            await main._reconcile_failed_profile_outcomes()
        request.assert_called_once_with('modem-a', 'target-card')
        writing.assert_not_awaited()
        listing.assert_not_awaited()
        self.assertEqual(self.store.latest('modem-a')['state'], 'waiting_flight_mode')
        self.assertEqual(self.store.latest('modem-a')['deadline_at'], 0)

    async def test_access_repair_rejects_old_host_ready_and_checks_profile_after_current_ready(self):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        task = self.store.update(task['id'], 'waiting_baseband', profile_confirmation_pending=True,
                                 bridge_request_id='request')
        obs = self.observed['devices']['modem-a']
        obs.update(desired={'flight_mode': False}, cellular_recovery={'state': 'ready', 'operation_id': 'old'})
        async def read_owned(*_args, **_kwargs):
            self.assertTrue(main.capability_lock.locked())
            self.assertTrue(main.hub.esim_switch_lock('modem-a').locked())
            self.assertTrue(main.hub.reader_lock('reader').locked())
            return [{'iccid': 'target-card', 'profileState': 'enabled'}]
        with patch.object(lpa, 'profile_list', new=AsyncMock(side_effect=read_owned)) as listing:
            await main._advance_esim_cellular_recovery(task)
            listing.assert_not_awaited()
            obs['cellular_recovery']['operation_id'] = 'request'
            await main._advance_esim_cellular_recovery(self.store.latest('modem-a'))
        listing.assert_awaited_once()
        result = self.store.latest('modem-a')
        self.assertEqual(result['state'], 'profile_enabled')
        self.assertFalse(result['profile_confirmation_pending'])

    async def test_observed_different_profile_never_turns_into_access_repair(self):
        old = self.failed()
        with patch.object(lpa, 'profile_list', new=AsyncMock(return_value=[
                {'iccid': 'other-card', 'profileState': 'enabled'}])) as listing:
            await main._reconcile_failed_profile_outcomes()
            self.store.update(old['id'], outcome_checked_at=0, outcome_checks=3)
            await main._reconcile_failed_profile_outcomes()
        listing.assert_awaited_once()
        self.assertEqual(self.store.latest('modem-a')['id'], old['id'])
        self.assertEqual(self.store.active(), [])

    async def test_current_host_failure_is_reported_even_while_bridge_is_absent(self):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        task = self.store.update(task['id'], 'waiting_baseband', profile_confirmation_pending=True,
                                 bridge_request_id='request')
        self.identity.clear()
        self.observed['devices']['modem-a']['cellular_recovery'] = {
            'state': 'failed', 'operation_id': 'request', 'error_code': 'sim_power_cycle_failed'}
        await main._advance_esim_cellular_recovery(task)
        self.assertEqual(self.store.latest('modem-a')['state'], 'failed')
        self.assertEqual(self.store.latest('modem-a')['error_code'], 'sim_power_cycle_failed')

    async def test_passive_access_recovery_never_blocks_switches_behind_a_card_probe(self):
        self.enterContext(patch.object(main, 'capability_lock', asyncio.Lock()))
        obs = self.observed['devices']['modem-a']
        cases = (
            (True, 'waiting_flight_mode', 'request', 0, 'waiting_flight_mode'),
            (False, 'waiting_identity', 'request', 0, 'waiting_baseband'),
            (False, 'ready', 'old-request', 0, 'waiting_baseband'),
            (False, 'ready', 'request', main.time.time(), 'waiting_baseband'),
            (True, 'failed', 'request', 0, 'failed'),
        )
        with patch.object(lpa, 'profile_list', new=AsyncMock()) as listing, \
                patch.object(main, '_esim_write_bridge_restart_request') as restart:
            for flight, state, request_id, checked, expected in cases:
                with self.subTest(flight=flight, state=state, request_id=request_id):
                    task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
                    task = self.store.update(task['id'], 'waiting_baseband',
                                             profile_confirmation_pending=True,
                                             bridge_request_id='request', profile_checked_at=checked)
                    obs.update(desired={'flight_mode': flight}, cellular_recovery={
                        'state': state, 'operation_id': request_id,
                        'error_code': 'sim_power_cycle_failed'})
                    async with main.hub.reader_lock('reader'):
                        worker = asyncio.create_task(main._advance_esim_cellular_recovery(task))
                        try:
                            await asyncio.sleep(0)
                            self.assertFalse(main.capability_lock.locked(),
                                             'passive host waiting must allow flight-mode changes')
                            await asyncio.wait_for(worker, 1)
                        finally:
                            worker.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await worker
                    self.assertEqual(self.store.latest('modem-a')['state'], expected)
        listing.assert_not_awaited()
        restart.assert_not_called()
        main._esim_guard_engine.assert_not_called()

    async def test_waiting_access_recovery_still_rejects_a_new_usb_generation(self):
        task = self.store.schedule('modem-a', 'target-card', 'reader', 'generation-a')
        task = self.store.update(task['id'], 'waiting_flight_mode',
                                 profile_confirmation_pending=True, bridge_request_id='request')
        self.observed['devices']['modem-a']['usb_generation'] = 'generation-b'
        with patch.object(lpa, 'profile_list', new=AsyncMock()) as listing:
            await main._advance_esim_cellular_recovery(task)
        self.assertEqual(self.store.latest('modem-a')['state'], 'cancelled')
        listing.assert_not_awaited()
