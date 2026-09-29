"""Card-free coverage of exclusive eSIM access and original-line restoration."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, esim_operations


class LocalLabelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(main, '_ESIM_CACHE_PATH', str(Path(directory) / 'cache.json')))
        self.ses = [{'id': 'one', 'eid': 'fixture-euicc', 'profiles': [
            {'iccid': 'fixture-card', 'profileNickname': 'Card name', 'profileState': 'enabled'}]}]
        main._esim_cache_store(copy.deepcopy(self.ses), '')

    async def test_local_note_never_accesses_card_or_engine_and_survives_fresh_read(self):
        with patch.object(main, '_esim_resolve_reader') as reader, \
                patch.object(main.lpa, 'profile_nickname', new=AsyncMock()) as nickname, \
                patch.object(main.engine, 'stop') as stop:
            before = main._esim_cache_load()['fixture-euicc']['ts']
            await main.api_esim_local_label('fixture-card', {
                'eid': 'fixture-euicc', 'se_id': 'one', 'label': '  Desk card  '})
            self.assertEqual(main._esim_cache_load()['fixture-euicc']['ts'], before)
            fresh = copy.deepcopy(self.ses)
            main._esim_cache_store(fresh, '')
            profile = fresh[0]['profiles'][0]
            self.assertEqual(profile['local_label'], 'Desk card')
            self.assertEqual(profile['profileNickname'], 'Card name')
            reader.assert_not_called(); nickname.assert_not_awaited(); stop.assert_not_called()
        await main.api_esim_local_label('fixture-card', {
            'eid': 'fixture-euicc', 'se_id': 'one', 'label': ''})
        self.assertEqual(main._esim_cache_load()['fixture-euicc']['ses'][0]['profiles'][0]['local_label'], '')

    async def test_target_must_match_euicc_se_and_profile_and_input_is_bounded(self):
        for change in [{'eid': 'other'}, {'se_id': 'other'}, {'label': 'x' * 121},
                       {'label': 'bad\nvalue'}, {'label': 123}]:
            with self.subTest(change=change), self.assertRaises(main.HTTPException):
                await main.api_esim_local_label('fixture-card', {
                    'eid': 'fixture-euicc', 'se_id': 'one', 'label': 'Fixture', **change})
        with self.assertRaises(main.HTTPException):
            await main.api_esim_local_label('unknown-card', {
                'eid': 'fixture-euicc', 'se_id': 'one', 'label': 'Fixture'})


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inst = {'id': '7', 'iccid': 'fixture-card', 'enabled': True}
        self.info = {'name': 'reader', 'iccid': 'fixture-card', 'present': True,
                     'identity_state': 'confirmed', 'generation': 4, 'reader_port': 'fixture-port'}
        self.body = {'reader': 'reader', 'resume_line_id': '7', 'expected_iccid': 'fixture-card',
                     'expected_generation': 4}
        self.enterContext(patch.dict(main.hub.cards, {'reader': self.info}, clear=True))
        for attr in ['lpa_busy', 'hotplug_epochs', 'reader_locks', 'esim_switch_locks']:
            self.enterContext(patch.dict(getattr(main.hub, attr), {}, clear=True))
        self.enterContext(patch.object(main.cfg, 'get_instance', return_value=self.inst))
        self.enterContext(patch.object(main, '_esim_switch_identity', return_value=('reader:fixture', '')))
        self.enterContext(patch.object(main, '_esim_resolve_reader', return_value=('reader', 0)))
        self.enterContext(patch.object(main, '_esim_guard_engine'))
        self.running = self.enterContext(patch.object(main, '_find_running_by_reader', return_value=self.inst))
        self.stop = self.enterContext(patch.object(main, '_stop_instance', new=AsyncMock(side_effect=self.stopped)))
        self.probe = self.enterContext(patch.object(main.sim, 'read_iccid_bounded', return_value='fixture-card'))
        self.refresh = self.enterContext(patch.object(main, '_esim_refresh_card', new=AsyncMock(return_value=self.info)))
        self.start = self.enterContext(patch.object(main, '_start_instance', new=AsyncMock()))
        self.allowed = self.enterContext(patch.object(main, '_line_auto_start_allowed', return_value=(True, '')))
        self.enterContext(patch.object(main.engine, 'is_running', return_value=False))
        self.enterContext(patch.object(main.device_state, 'status', return_value={'devices': {}}))
        self.enterContext(patch.object(main, '_esim_imei_for_reader', return_value=''))
        self.enterContext(patch.object(main, '_esim_cache_store'))
        self.read = self.enterContext(patch.object(main.lpa, 'load_all_ses', new=AsyncMock(return_value={'ses': []})))

    async def stopped(self, iid, reason):
        self.assertEqual(reason, 'esim_maintenance')
        main.hub.hotplug_epochs[iid] = 1

    async def test_read_stops_only_authorized_line_then_verifies_and_restarts(self):
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'started')
        self.stop.assert_awaited_once_with('7', 'esim_maintenance')
        self.refresh.assert_awaited_once()
        self.assertFalse(self.refresh.await_args.kwargs['auto_start'])
        self.start.assert_awaited_once()
        self.assertEqual(self.start.await_args.kwargs['automatic_epoch'], 1)
        self.assertFalse(main.hub.lpa_busy)

    async def test_failed_read_still_recovers_original_line(self):
        self.read.side_effect = main.lpa.LpaError('fixture rejected')
        with self.assertRaises(main.HTTPException) as raised:
            await main.api_esim_read(self.body)
        self.assertEqual(raised.exception.detail['line_recovery'], 'started')
        self.start.assert_awaited_once()

    async def test_previously_stopped_line_is_never_started(self):
        self.running.return_value = None
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'not_needed')
        self.stop.assert_not_awaited(); self.start.assert_not_awaited()

    async def test_replacement_or_wrong_sibling_blocks_before_stop(self):
        for change in [{'expected_iccid': 'other'}, {'expected_generation': 3}, {'resume_line_id': '8'}]:
            with self.subTest(change=change), self.assertRaises(main.HTTPException):
                await main.api_esim_read({**self.body, **change})
        self.running.return_value = {'id': '8'}
        with self.assertRaises(main.HTTPException):
            await main.api_esim_read(self.body)
        self.stop.assert_not_awaited(); self.read.assert_not_awaited()

    async def test_changed_card_after_stop_cannot_run_lpa_or_restart(self):
        self.probe.return_value = 'different-card'
        with self.assertRaises(main.HTTPException):
            await main.api_esim_read(self.body)
        self.read.assert_not_awaited(); self.start.assert_not_awaited()

    async def test_explicit_stop_during_read_cancels_restart(self):
        async def read(*_):
            main.hub.hotplug_epochs['7'] = 2
            return {'ses': []}
        self.read.side_effect = read
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'cancelled')
        self.start.assert_not_awaited()

    async def test_disabled_vowifi_or_unverified_identity_never_restarts(self):
        self.allowed.return_value = (False, 'vowifi_disabled')
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'cancelled')
        self.start.assert_not_awaited()
        self.allowed.return_value = (True, '')
        self.refresh.side_effect = RuntimeError('private hardware detail')
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'failed')
        self.assertNotIn('private', str(result))
        self.start.assert_not_awaited()

    async def test_client_cancellation_does_not_cancel_accepted_read_or_early_release(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(*_):
            entered.set()
            await release.wait()
            return {'ses': []}
        self.read.side_effect = read
        client = asyncio.create_task(main.api_esim_read(self.body))
        await entered.wait()
        client.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await client
        self.assertTrue(main.hub.lpa_busy['reader'])
        self.start.assert_not_awaited()
        release.set()
        await asyncio.gather(*list(main.esim_download_tasks))
        self.start.assert_awaited_once()
        self.assertFalse(main.hub.lpa_busy)

    async def test_modem_recovery_uses_same_usb_generation_and_checks_all_slots(self):
        self.enterContext(patch.object(main, '_esim_switch_identity', return_value=('modem-fixture', 'modem-fixture')))
        self.enterContext(patch.object(main, '_esim_modem_reader_names', return_value=['reader']))
        observed = {'devices': {'modem-fixture': {'present': True, 'usb_generation': 'plug-a'}}}
        self.enterContext(patch.object(main.device_state, 'status', return_value=observed))
        bridge = self.enterContext(patch.object(main, '_esim_restart_modem_bridge', new=AsyncMock()))
        slots = self.enterContext(patch.object(main, '_esim_refresh_modem_readers', new=AsyncMock(return_value=(self.info, ['reader']))))
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'started')
        bridge.assert_awaited_once_with('modem-fixture', 'fixture-card', cellular_refresh=False)
        slots.assert_awaited_once()
        bridge.reset_mock(); self.start.reset_mock()
        async def unplug(*_):
            observed['devices']['modem-fixture']['usb_generation'] = 'plug-b'
            return {'ses': []}
        self.read.side_effect = unplug
        result = await main.api_esim_read(self.body)
        self.assertEqual(result['line_recovery'], 'cancelled')
        bridge.assert_not_awaited(); self.start.assert_not_awaited()

    async def test_download_terminal_result_waits_for_owned_recovery_on_success_and_failure(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        operations = esim_operations.DownloadOperations(str(Path(directory) / 'download.json'))
        self.enterContext(patch.object(main, 'esim_download_operations', operations))
        self.enterContext(patch.object(main, '_esim_resolve_se_owned', new=AsyncMock(return_value={'id': 'one'})))
        self.enterContext(patch.object(main.hub, 'broadcast', new=AsyncMock()))
        download = self.enterContext(patch.object(main.lpa, 'download', new=AsyncMock(return_value={})))
        for failure in [False, True]:
            self.start.reset_mock()
            download.side_effect = main.lpa.LpaError('fixture rejected') if failure else None
            reply = await main.api_esim_download(self.body)
            self.assertEqual(reply['operation']['line_recovery'], 'recovering')
            await asyncio.gather(*list(main.esim_download_tasks))
            record = operations.latest('reader')
            self.assertEqual(record['state'], 'failed' if failure else 'success')
            self.assertEqual(record['line_recovery'], 'started')
            self.start.assert_awaited_once()
            self.assertFalse(main.hub.lpa_busy)

    async def test_owned_work_keeps_lock_until_bounded_thread_finishes_during_shutdown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def work():
            entered.set()
            await release.wait()
        job = asyncio.create_task(main._esim_await_owned(work()))
        await entered.wait()
        job.cancel()
        await asyncio.sleep(0)
        self.assertFalse(job.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await job


if __name__ == '__main__':
    unittest.main()
