"""Profile-switch notification and identity handoff regressions, without hardware."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_identity_fixture import verified_bridge
from control.app import main
from control.app.sim import CardInfo


class NotificationCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(main, '_ESIM_CACHE_PATH', str(Path(temp) / 'chip.json')))
        self.enterContext(patch.object(main.hub, 'broadcast', new=AsyncMock()))
        main._esim_cache_store([{'id': 'one', 'eid': 'fixture-euicc', 'profiles': [
            {'iccid': 'fixture-card', 'profileState': 'enabled'}]},
            {'id': 'two', 'eid': 'fixture-second', 'profiles': [{'iccid': 'other-card'}]}], '')

    async def test_failure_survives_live_reload_and_card_generation_change(self):
        status = await main._esim_notification_status_callback('reader', 'fixture-card')({
            'state': 'failed', 'reason_code': 'reader_busy', 'attempts': 3,
            'detail': 'private-endpoint'})
        event = main.hub.broadcast.await_args.args[0]
        self.assertNotIn('generation', event)
        self.assertNotIn('private-endpoint', str(event))
        fresh = [{'id': 'one', 'eid': 'fixture-euicc', 'profiles': [
            {'iccid': 'fixture-card', 'profileState': 'enabled'}]}]
        main._esim_cache_store(fresh, '')
        self.assertEqual(fresh[0]['profiles'][0]['notification_status'], status)
        with patch.object(main, '_esim_resolve_reader', return_value=('reader', 0)), \
                patch.object(main, '_current_reader_iccid', return_value='fixture-card'), \
                patch.object(main.hub, 'cards', {'reader': {'generation': 4}}):
            cached = await main.api_esim_chip_cached(reader='reader')
        self.assertEqual(cached['ses'][0]['profiles'][0]['notification_status'], status)

    async def test_processing_all_clears_warning_only_on_the_selected_se(self):
        for iccid in ['fixture-card', 'other-card']:
            await main._esim_notification_status_callback('reader', iccid)({'state': 'failed'})
        main._esim_cache_remove_notifications('fixture-card', 'one', seq=1)
        self.assertIn('notification_status', main._esim_cache_load()['fixture-euicc']['ses'][0]['profiles'][0])
        main._esim_cache_remove_notifications('fixture-card', 'one')
        ses = main._esim_cache_load()['fixture-euicc']['ses']
        self.assertNotIn('notification_status', ses[0]['profiles'][0])
        self.assertEqual(ses[1]['profiles'][0]['notification_status']['state'], 'failed')

    def test_interrupted_old_attempt_does_not_remain_processing(self):
        main._esim_cache_update_profile('fixture-card', notification_status={
            'state': 'processing', 'updated_at': main.time.time() - 181})
        status = main._esim_cache_for_iccid('fixture-card')['ses'][0]['profiles'][0]['notification_status']
        self.assertEqual(status['state'], 'failed')

    async def test_only_a_confirmed_empty_live_list_replaces_an_old_failure(self):
        for loaded, notes, expected in [(False, [], 'failed'),
                                        (True, [{'seqNumber': 1}], 'failed'),
                                        (True, [], 'empty')]:
            with self.subTest(loaded=loaded, notes=notes):
                await main._esim_notification_status_callback('reader', 'fixture-card')({
                    'state': 'failed', 'reason_code': 'reader_unavailable', 'attempts': 0})
                await main._esim_notification_status_callback('reader', 'other-card')({
                    'state': 'failed', 'reason_code': 'network_transport', 'attempts': 1})
                fresh = [{'id': 'one', 'eid': 'fixture-euicc',
                          'notifications_loaded': loaded, 'notifications': notes,
                          'notifications_checked_at': main.time.time(),
                          'profiles': [{'iccid': 'fixture-card'}]},
                         {'id': 'two', 'eid': 'fixture-second',
                          'notifications_loaded': False, 'notifications': [],
                          'profiles': [{'iccid': 'other-card'}]}]
                main._esim_cache_store(fresh, '')
                self.assertEqual(fresh[0]['profiles'][0]['notification_status']['state'], expected)
                self.assertEqual(fresh[1]['profiles'][0]['notification_status']['state'], 'failed')
                stored = main._esim_cache_for_iccid('fixture-card')['ses'][0]
                self.assertEqual(stored['notifications'], [])
                self.assertFalse(stored['notifications_loaded'])
                self.assertGreater(fresh[0]['profiles'][0]['notification_status']['updated_at'], 0)

    async def test_a_new_attempt_after_the_list_read_is_not_cleared_by_that_old_read(self):
        fresh = [{'id': 'one', 'eid': 'fixture-euicc', 'notifications_loaded': True,
                  'notifications': [], 'notifications_checked_at': main.time.time() - 1,
                  'profiles': [{'iccid': 'fixture-card'}]}]
        status = await main._esim_notification_status_callback('reader', 'fixture-card')({
            'state': 'processing', 'attempts': 1})
        main._esim_cache_store(fresh, '')
        self.assertEqual(fresh[0]['profiles'][0]['notification_status'], status)


class SwitchOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_rebuild_notify_verify_in_that_order_before_line_start(self):
        name = 'VoWiFi Modem modem-a 00 00'
        order = []
        target = {'id': 'new', 'iccid': 'fixture-card', 'enabled': False}
        identity = {**verified_bridge(), 'iccid': 'fixture-card'}
        async def lpac(*args, **kwargs):
            order.append(args[:2])
            self.assertTrue(main.hub.lpa_busy[name])
            return main.lpa.LpaResult(data={})
        async def bridge(*args):
            order.append('bridge')
            return {'state': 'channels_ready'}
        async def verify(*args):
            order.append('verify')
            self.assertTrue(main.hub.lpa_busy[name])
            # Simulate the monitor observing lpa_busy after the final successful read.
            info = {'iccid': 'fixture-card', 'present': True, 'verified_at': main.time.time(),
                    'esim_verified_bridge': identity['bridge_generation'],
                    'bridge_generation': identity['bridge_generation'],
                    'esim_verified_maintenance': 1, 'identity_state': 'pending',
                    'identity_reason': 'lpa_busy'}
            main.hub.cards[name] = info
            return info, [name]
        async def selection(*args):
            order.append('automatic')
        self.enterContext(patch.object(main, '_esim_resolve_reader', return_value=(name, 0)))
        self.enterContext(patch.object(main, '_esim_switch_identity', return_value=('modem-a', 'modem-a')))
        self.enterContext(patch.object(main, '_esim_prepare_profile_switch', new=AsyncMock(return_value={})))
        self.enterContext(patch.object(main, '_esim_modem_reader_names', return_value=[name]))
        self.enterContext(patch.object(main, '_esim_resolve_se', return_value={'id': 'one', 'aid': 'aid'}))
        self.enterContext(patch.object(main, '_modem_identity_for_reader', return_value=identity))
        self.enterContext(patch.object(main, '_pcsc_maintenance_epoch', return_value=1))
        with patch.object(main, '_esim_guard_engine'), \
                patch.object(main, '_esim_cache_update_profile'), \
                patch.object(main, '_esim_cache_remove_notifications'), \
                patch.object(main, '_esim_vowifi_requested', return_value=False), \
                patch.object(main, '_esim_restart_modem_bridge', new=bridge), \
                patch.object(main, '_esim_refresh_modem_readers', new=verify), \
                patch.object(main, '_esim_restore_cellular_selection', new=selection), \
                patch.object(main, '_match_instance_by_iccid', return_value=target), \
                patch.object(main, '_refresh_instance_reader_binding', return_value=target), \
                patch.object(main.cfg, 'upsert_instance', side_effect=lambda value: value), \
                patch.object(main.egress, 'publish'), \
                patch.object(main.lpa, 'run_lpac', new=lpac), \
                patch.object(main.lpa, 'auto_process_notifications', return_value=True), \
                patch.object(main.hub, 'cards', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'broadcast', new=AsyncMock()):
            result = await main._enable_esim_profile('fixture-card')
            await asyncio.sleep(0)
            self.assertFalse(main.hub.lpa_busy)
            self.assertEqual(main.hub.cards[name]['identity_state'], 'confirmed')
        self.assertEqual(order, [('profile', 'enable'), 'bridge', ('notification', 'process'), 'verify'])
        self.assertEqual(result['notification_status']['state'], 'processed')
        self.assertEqual(result['recovery_skipped'], 'vowifi_disabled')


class VerifiedHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_slots_keep_the_new_bridge_generation(self):
        names = [f'VoWiFi Modem modem-a 00 0{i}' for i in range(3)]
        bridge = {**verified_bridge(), 'iccid': 'fixture-card', 'hardware_id': 'modem-a',
                  'channel_allocated': 3}
        card = CardInfo(names[0], 0, True, iccid='fixture-card', imsi='001010000000001',
                        mcc='001', mnc='01')
        with patch.object(main.hub, 'cards', {}), \
                patch.object(main.hub, 'broadcast', new=AsyncMock()), \
                patch.object(main, '_modem_identity_for_reader', return_value=bridge), \
                patch.object(main, '_pcsc_maintenance_epoch', return_value=1), \
                patch.object(main.sim, 'list_readers', return_value=names), \
                patch.object(main.sim, 'read_card', return_value=card) as read, \
                patch.object(main.sim, 'read_iccid', return_value='fixture-card') as iccid, \
                patch.object(main, '_match_instance_by_iccid', return_value=None), \
                patch.object(main.cfg, 'card_auto_create_suppressed', return_value=True):
            await main._esim_refresh_modem_readers(names[0], 'modem-a', 'fixture-card')
            for name in names:
                info = main.hub.cards[name]
                self.assertEqual(info['bridge_generation'], bridge['bridge_generation'])
                self.assertTrue(main._esim_verified_reader_current(name, info, 1))
                for changed in ({'present': False}, {'iccid': 'replacement'},
                                {'esim_verified_bridge': 'old'}, {'verified_at': 0}):
                    self.assertFalse(main._esim_verified_reader_current(name, {**info, **changed}, 1))
                self.assertFalse(main._esim_verified_reader_current(name, info, 2))
                self.assertNotIn('esim_verified_bridge', main._client_card_info(info))
            read.assert_called_once_with(0)
            self.assertEqual(iccid.call_count, 2)

    async def test_new_lpa_invalidates_previous_proof_even_when_it_fails(self):
        names = [f'VoWiFi Modem modem-a 00 0{i}' for i in range(3)]
        cards = {name: {'esim_verified_bridge': 'old'} for name in names}
        async def failed():
            raise main.lpa.LpaError('fixture failure')
        with patch.object(main.hub, 'cards', cards), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main, '_esim_guard_engine'), \
                patch.object(main, '_esim_modem_reader_names', return_value=names):
            with self.assertRaises(main.HTTPException):
                await main._esim_run(names[0], 0, failed())
        self.assertTrue(all('esim_verified_bridge' not in value for value in cards.values()))

    async def test_maintenance_skips_only_current_verified_proof_and_rescan_still_probes(self):
        for invalidation in ('none', 'new_bridge', 'new_maintenance', 'removed', 'rescan'):
            with self.subTest(invalidation=invalidation):
                name = 'VoWiFi Modem modem-a 00 00'
                clock = [101.0]
                bridge = {**verified_bridge(), 'updated_at': 101, 'iccid': 'fixture-card'}
                row = {'name': name, 'index': 0, 'present': True, 'iccid': 'fixture-card',
                       'identity_state': 'confirmed', 'verified_at': 101,
                       'esim_verified_maintenance': 100, 'esim_verified_bridge': 'fixture-session',
                       'bridge_generation': 'fixture-session'}
                async def advance(_seconds):
                    clock[0] = 161.0
                    if invalidation == 'new_bridge':
                        bridge['bridge_generation'] = 'replacement'
                def states():
                    return [{'name': name, 'index': 0,
                             'present': not (invalidation == 'removed' and clock[0] == 101)}]
                def rescan():
                    return {'state': 'success', 'operation_id': 'fixture-rescan'} if (
                        invalidation == 'rescan' and clock[0] == 161) else {}
                with patch.object(main.hub, 'cards', {name: row}), \
                        patch.object(main.hub, 'lpa_busy', {}), \
                        patch.object(main.hub, 'reader_locks', {}), \
                        patch.object(main.hub, 'device_rescan_applied', ''), \
                        patch.object(main.hub, 'broadcast', new=AsyncMock()), \
                        patch.object(main, '_modem_identity_for_reader', return_value=bridge), \
                        patch.object(main, '_pcsc_maintenance_epoch', side_effect=lambda:
                            102 if invalidation == 'new_maintenance' and clock[0] == 161 else 100), \
                        patch.object(main.time, 'time', side_effect=lambda: clock[0]), \
                        patch.object(main.asyncio, 'sleep', new=advance), \
                        patch.object(main.operations, 'device_rescan_status', side_effect=rescan), \
                        patch.object(main.card, 'reader_states', side_effect=states), \
                        patch.object(main.card, 'wait_for_change', side_effect=asyncio.CancelledError), \
                        patch.object(main, '_on_card_insert', new=AsyncMock()) as read:
                    with self.assertRaises(asyncio.CancelledError):
                        await main.card_monitor()
                if invalidation == 'none':
                    read.assert_not_awaited()
                elif invalidation == 'rescan':
                    read.assert_awaited_once_with(
                        name, 0, verify=True, operation_id='fixture-rescan')
                else:
                    read.assert_awaited_once_with(name, 0, verify=True)
