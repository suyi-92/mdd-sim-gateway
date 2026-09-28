"""Regression coverage for committed writes with lost replies and shared-card ownership."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import esim_operations, lpa, main, usbreader


class EnableOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_reply_with_fresh_target_enabled_continues_without_replaying_write(self):
        error = lpa.LpaError('es10c_enable_profile')
        with patch.object(lpa, 'profile_enable', new=AsyncMock(side_effect=error)) as enable, \
                patch.object(lpa, 'profile_list', new=AsyncMock(side_effect=[
                    lpa.LpaError('euicc_init'),
                    [{'iccid': 'target-card', 'profileState': 'enabled'}]])) as listing, \
                patch.object(main, 'ESIM_CARD_REFRESH_INTERVAL', 0), \
                patch.object(lpa, 'maybe_process_notifications', new=AsyncMock()) as notify:
            await main._esim_enable_verified('reader', 'target-card', aid='fixture-aid',
                                             process_notifications=False)
        enable.assert_awaited_once()
        self.assertEqual(listing.await_count, 2)
        self.assertEqual(listing.await_args.kwargs, {'aid': 'fixture-aid', 'timeout': 10})
        notify.assert_not_awaited()

    async def test_unknown_result_and_other_card_never_restore_old_state(self):
        for profiles in ([], ['malformed'], [{'iccid': 'third-card', 'profileState': 'enabled'}]):
            with self.subTest(profiles=profiles), \
                    patch.object(main.hub, 'cards', {'reader': {'iccid': 'old-card'}}), \
                    patch.object(lpa, 'profile_enable', new=AsyncMock(side_effect=lpa.LpaError('lost reply'))), \
                    patch.object(lpa, 'profile_list', new=AsyncMock(return_value=profiles)), \
                    patch.object(main, 'ESIM_CARD_REFRESH_INTERVAL', 0):
                with self.assertRaises(main.HTTPException) as caught:
                    await main._esim_enable_verified('reader', 'target-card')
                self.assertEqual(caught.exception.detail['code'], 'profile_enable_unconfirmed')

    async def test_only_verified_unchanged_profile_returns_original_failure(self):
        original = lpa.LpaError('euicc_init', detail='SCARD_E_SHARING_VIOLATION')
        with patch.object(main.hub, 'cards', {'reader': {'iccid': 'old-card'}}), \
                patch.object(lpa, 'profile_enable', new=AsyncMock(side_effect=original)), \
                patch.object(lpa, 'profile_list', new=AsyncMock(return_value=[
                    {'iccid': 'old-card', 'profileState': 'enabled'}])), \
                patch.object(main, 'ESIM_CARD_REFRESH_INTERVAL', 0):
            with self.assertRaises(lpa.LpaError) as caught:
                await main._esim_enable_verified('reader', 'target-card')
        self.assertIs(caught.exception, original)

    async def test_uncertain_modem_enable_keeps_recovery_visible_and_old_line_stopped(self):
        updates = AsyncMock()
        error = main.HTTPException(409, {'code': 'profile_enable_unconfirmed'})
        with patch.object(main, '_esim_resolve_reader', return_value=('reader', 0)), \
                patch.object(main, '_esim_switch_identity', return_value=('modem-1', 'modem-1')), \
                patch.object(main, '_esim_prepare_profile_switch', new=AsyncMock(return_value={'1': {}})), \
                patch.object(main, '_esim_modem_reader_names', return_value=['reader']), \
                patch.object(main, '_esim_resolve_se_owned', new=AsyncMock(return_value={'id': 'default'})), \
                patch.object(main, '_esim_prewarm_target_egress'), \
                patch.object(main, '_esim_enable_verified', new=lambda *a, **k: object()), \
                patch.object(main, '_esim_run', new=AsyncMock(side_effect=error)), \
                patch.object(main, '_esim_profile_event', new=AsyncMock()), \
                patch.object(main, '_publish_esim_recovery', new=AsyncMock()), \
                patch.object(main, '_update_esim_recovery', new=updates), \
                patch.object(main.esim_recoveries, 'active', return_value=[]), \
                patch.object(main.esim_recoveries, 'schedule', return_value={'id': 'fixture'}), \
                patch.object(main, '_esim_restore_profile_switch', new=AsyncMock()) as restore:
            with self.assertRaises(main.HTTPException):
                await main._enable_esim_profile('target-card')
        restore.assert_not_awaited()
        self.assertEqual(updates.await_args.args, ('fixture', 'failed'))
        self.assertEqual(updates.await_args.kwargs['error_code'], 'profile_enable_unconfirmed')


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_orphaned_reading_state_becomes_retryable_but_owned_read_does_not(self):
        info = {'name': 'reader', 'identity_state': 'reading'}
        future = asyncio.get_running_loop().create_future()
        with patch.object(main.hub, 'card_probes', {'reader': future}):
            self.assertFalse(main._identity_retry_due(info))
            self.assertEqual(info['identity_state'], 'reading')
            future.set_result(False)
            self.assertFalse(main._identity_retry_due(info))
            self.assertEqual(info['identity_state'], 'pending')
            self.assertEqual(info['identity_reason'], 'read_interrupted')
            info['identity_retry_at'] = 0
            self.assertTrue(main._identity_retry_due(info))

    async def test_soft_timeout_completion_releases_stale_reading_state(self):
        finish = asyncio.Event()
        info = {'name': 'reader', 'present': True, 'identity_state': 'pending'}
        async def read(*args, **kwargs):
            await finish.wait()  # Model a coordinator that exits without a terminal result.
        with patch.object(main.hub, 'cards', {'reader': info}), \
                patch.object(main.hub, 'card_probes', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'broadcast', new=AsyncMock()), \
                patch.object(main, '_client_cards', return_value=[]), \
                patch.object(main, 'CARD_PROBE_TIMEOUT_SECONDS', 0.01), \
                patch.object(main, '_on_card_insert_locked', new=read):
            self.assertFalse(await main._on_card_insert('reader', 0))
            self.assertEqual(info['identity_state'], 'reading')
            self.assertTrue(main.hub.reader_lock('reader').locked())
            worker = main.hub.card_probes['reader']
            finish.set()
            await worker
            await asyncio.sleep(0)
            self.assertEqual(info['identity_state'], 'pending')
            self.assertFalse(main.hub.reader_lock('reader').locked())

    async def test_se_discovery_waits_for_sibling_reader_owner(self):
        first, second = 'VoWiFi Modem modem-1 00 00', 'VoWiFi Modem modem-1 00 01'
        with patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main, '_esim_modem_reader_names', return_value=[first, second]), \
                patch.object(main, '_esim_guard_engine'), \
                patch.object(main, '_esim_resolve_se', return_value={'id': 'default'}) as resolve:
            lock = main.hub.reader_lock(first)
            await lock.acquire()
            task = asyncio.create_task(main._esim_resolve_se_owned(second, 1))
            await asyncio.sleep(0.01)
            resolve.assert_not_called()
            lock.release()
            await task
            resolve.assert_called_once()

    async def test_se_discovery_does_not_enter_between_switch_recovery_steps(self):
        with patch.object(main.hub, 'esim_switch_locks', {}), \
                patch.object(main, '_esim_switch_identity', return_value=('modem-1', 'modem-1')), \
                patch.object(main, '_esim_guard_engine'), \
                patch.object(main, '_esim_resolve_se', return_value={'id': 'default'}) as resolve:
            lock = main.hub.esim_switch_lock('modem-1')
            await lock.acquire()
            task = asyncio.create_task(main._esim_resolve_se_owned('reader', 0))
            await asyncio.sleep(0.01)
            resolve.assert_not_called()
            lock.release()
            await task
            resolve.assert_called_once()

    async def test_download_reserves_all_slots_before_discovery(self):
        first, second = 'VoWiFi Modem modem-1 00 00', 'VoWiFi Modem modem-1 00 01'
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover(*args, **kwargs):
            entered.set()
            await release.wait()
        with patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.hub, 'esim_switch_locks', {}), \
                patch.object(main, '_esim_resolve_reader', side_effect=lambda i, n: (n, i)), \
                patch.object(main, '_esim_switch_identity', return_value=('modem-1', 'modem-1')), \
                patch.object(main, '_esim_modem_reader_names', return_value=[first, second]), \
                patch.object(main, '_esim_resolve_se_owned', new=discover):
            request = asyncio.create_task(main.api_esim_download({'reader': first}))
            await entered.wait()
            with self.assertRaises(main.HTTPException) as caught:
                await main.api_esim_download({'reader': second})
            self.assertEqual(caught.exception.status_code, 409)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertEqual(main.hub.lpa_busy, {})
            self.assertFalse(main.hub.esim_switch_lock('modem-1').locked())


class UsbLookupTests(unittest.TestCase):
    def test_pcsc_temporarily_unavailable_has_no_usb_binding(self):
        with patch.object(usbreader, 'readers', side_effect=RuntimeError('pcsc unavailable')):
            self.assertIsNone(usbreader.port_for_index(0))

    def test_native_port_lookup_does_not_connect_to_other_readers(self):
        with patch.object(usbreader, 'readers', return_value=['native', 'other']), \
                patch.object(usbreader, '_channel_id_bus_dev', return_value=(1, 2)) as channel, \
                patch.object(usbreader, '_port_path_for', return_value='1-2'):
            self.assertEqual(usbreader.port_for_index(0), '1-2')
        channel.assert_called_once_with('native')

    def test_virtual_slots_never_open_direct_usb_mapping_handles(self):
        with patch.object(usbreader, 'SCardEstablishContext') as context:
            self.assertIsNone(usbreader._channel_id_bus_dev('VoWiFi Modem modem-1 00 00'))
        context.assert_not_called()

    def test_download_busy_is_not_classified_as_not_euicc(self):
        for detail, expected in (('SCARD_E_SHARING_VIOLATION', 'reader_busy'),
                                 ('timed out', 'download_timeout'), ('cancelled', 'interrupted')):
            error = lpa.LpaError('euicc_init', detail=detail)
            self.assertEqual(esim_operations.error_code(error, lpa.classify_lpa_error(error)), expected)


class ProcessLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_reaps_process_even_when_stdout_closed_and_sigint_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'lpac-fixture'
            pidfile = Path(temporary) / 'pid'
            binary.write_text('#!/usr/bin/python3\nimport os, signal, time\n'
                              'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                              f'open({str(pidfile)!r}, "w").write(str(os.getpid()))\n'
                              'os.close(1)\ntime.sleep(30)\n')
            binary.chmod(0o700)
            with patch.object(lpa, 'lpac_bin', return_value=str(binary)):
                with self.assertRaises(lpa.LpaError):
                    await lpa.run_lpac('profile', 'list', timeout=0.2, track_key='reader')
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertNotIn('reader', lpa._active)
