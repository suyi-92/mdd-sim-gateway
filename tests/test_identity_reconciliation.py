"""Recovery regressions: fixtures never contact PC/SC, production or Docker."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from control.app import main
from host import vpcd_modem_bridge as bridge


class IdentityEvidenceTests(unittest.TestCase):
    def test_baseband_fallback_is_labelled_unverified(self):
        modem = object.__new__(bridge.ModemCard)
        with patch.object(modem, '_iccid_from_card', return_value=''), \
                patch.object(modem, '_at', return_value=b'+CCID: 89000000000000000001\r\nOK'):
            value = modem.identity()
        self.assertEqual(value['iccid_source'], 'baseband_cache')
        self.assertFalse(value['iccid_verified'])

    def test_verified_subscription_updates_country_and_clears_learned_number(self):
        old = {'iccid': 'fixture', 'imsi': 'old', 'mcc': '262', 'mnc': '01',
               'msisdn': 'learned', 'msisdn_source': 'ims'}
        observed = {'iccid': 'fixture', 'imsi': 'new', 'mcc': '515', 'mnc': '02',
                    'mnc_len': 2, 'smsc': None, 'carrier_identity': {}}
        result = main._verified_subscription_update(old, observed)
        self.assertEqual(result['mcc'], '515')
        self.assertEqual(result['imsi'], 'new')
        self.assertEqual(result['msisdn'], '')
        self.assertNotIn('pin', result)
        self.assertNotIn('name', result)
        self.assertNotIn('proxy_country', result)

    def test_partial_identity_cannot_overwrite_saved_subscription(self):
        self.assertEqual(main._verified_subscription_update(
            {'iccid': 'fixture'}, {'iccid': 'fixture', 'imsi': None}), {})


class RefreshFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_refresh_does_not_start_old_card(self):
        with patch.object(main.hub, 'cards', {'reader': {
                'iccid': 'old', 'matched': 'old-line', 'present': True}}), \
                patch.object(main, '_esim_probe_card', new=AsyncMock(side_effect=RuntimeError())), \
                patch.object(main.hub, 'broadcast', new=AsyncMock()), \
                patch.object(main, '_auto_start_hotplugged_line', new=AsyncMock()) as start:
            info = await main._esim_refresh_card('reader', 0)
            self.assertEqual(info['identity_state'], 'pending')
            start.assert_not_called()


class BridgeFreshnessTests(unittest.TestCase):
    def test_only_fresh_current_process_direct_card_evidence_is_accepted(self):
        from bridge_identity_fixture import verified_bridge
        identity = {**verified_bridge(), 'iccid': 'fixture'}
        self.assertTrue(main._bridge_card_evidence(identity))
        for changed in ({'updated_at': 0}, {'updated_at': main.time.time() + 60},
                        {'bridge_pid': 0}, {'bridge_start': 'wrong-generation'},
                        {'iccid_verified': False}, {'iccid_source': 'baseband_cache'},
                        {'channel_status': 'allocating'}, {'iccid': ''}):
            with self.subTest(changed=changed):
                self.assertFalse(main._bridge_card_evidence({**identity, **changed}))


class CardRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_first_read_retries_without_repeated_usb_reset(self):
        from control.app.sim import CardInfo
        name = 'fixture-reader'
        good = CardInfo(name, 0, True, iccid='fixture', imsi='001010000000001', mcc='001', mnc='01')
        with patch.object(main.hub, 'cards', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=None), \
                patch.object(main, '_probe_inserted_card', new=AsyncMock(side_effect=RuntimeError())) as cold, \
                patch.object(main.sim, 'read_card', return_value=good) as retry, \
                patch.object(main, '_match_instance_by_iccid', return_value=None), \
                patch.object(main.cfg, 'card_auto_create_suppressed', return_value=True):
            await main._on_card_insert(name, 0)
            first = dict(main.hub.cards[name])
            self.assertEqual(first['identity_state'], 'pending')
            self.assertGreater(first['identity_retry_at'], main.time.monotonic())
            await main._on_card_insert(name, 0, verify=True)
            self.assertEqual(main.hub.cards[name]['identity_state'], 'confirmed')
            cold.assert_awaited_once()
            retry.assert_called_once_with(0)

    async def test_lock_contention_retains_history_but_blocks_start(self):
        import asyncio
        name = 'fixture-reader'
        lock = asyncio.Lock()
        await lock.acquire()
        row = {'name': name, 'present': True, 'iccid': 'fixture', 'matched': 'line'}
        with patch.object(main.hub, 'cards', {name: row}), \
                patch.object(main.hub, 'reader_locks', {name: lock}), \
                patch.object(main.hub, 'cards_list', return_value=[row]):
            await main._on_card_insert(name, 0)
            self.assertEqual(row['iccid'], 'fixture')
            self.assertEqual(main._line_auto_start_allowed({'id': 'line', 'iccid': 'fixture'}),
                             (False, 'identity_pending'))
        lock.release()

    async def test_forced_verify_reuses_running_engine_identity_without_apdu(self):
        import asyncio
        name = 'fixture-reader'
        inst = {'id': 'line', 'iccid': 'fixture-card', 'imsi': '001010000000001',
                'mcc': '001', 'mnc': '01', 'smsc': '', 'carrier_identity': {}}
        previous = {'name': name, 'index': 0, 'present': True, 'iccid': 'fixture-card',
                    'matched': 'line', 'identity_state': 'confirmed', 'generation': 1}
        with patch.object(main.hub, 'cards', {name: previous}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=inst), \
                patch.object(main.engine, 'read_run_json', return_value={
                    'state': 'PIN_DISABLED', 'reader': name, 'iccid': 'fixture-card'}), \
                patch.object(main.sim, 'read_card') as read, \
                patch.object(main, '_probe_inserted_card', new=AsyncMock()) as cold, \
                patch.object(main, '_auto_start_hotplugged_line', new=AsyncMock()) as start:
            await main._on_card_insert_locked(name, 0, verify=True, retire=[])
            await asyncio.sleep(0)
            observed = dict(main.hub.cards[name])
        read.assert_not_called()
        cold.assert_not_awaited()
        start.assert_awaited_once_with('line')
        self.assertEqual(observed['identity_state'], 'confirmed')
        self.assertEqual(observed['identity_source'], 'running_session')

    async def test_running_identity_change_stops_owner_before_idle_reprobe(self):
        name = 'fixture-reader'
        inst = {'id': 'line', 'iccid': 'old-card', 'imsi': '001010000000001'}
        previous = {'name': name, 'index': 0, 'present': True, 'iccid': 'old-card',
                    'matched': 'line', 'identity_state': 'confirmed', 'generation': 1}
        with patch.object(main.hub, 'cards', {name: previous}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'card_probes', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=inst), \
                patch.object(main.engine, 'read_run_json', return_value={
                    'state': 'WRONG_CARD', 'reader': name, 'iccid': 'replacement-card'}), \
                patch.object(main.sim, 'read_card') as read, \
                patch.object(main, '_probe_inserted_card', new=AsyncMock()) as cold, \
                patch.object(main, '_stop_instance', new=AsyncMock()) as stop:
            await main._on_card_insert(name, 0, verify=True)
            observed = dict(main.hub.cards[name])
        read.assert_not_called()
        cold.assert_not_awaited()
        stop.assert_awaited_once_with('line', 'card_identity_changed')
        self.assertEqual(observed['identity_state'], 'pending')
        self.assertEqual(observed['identity_reason'], 'running_identity_changed')

    async def test_missing_running_status_waits_without_falling_through_to_apdu(self):
        name = 'fixture-reader'
        inst = {'id': 'line', 'iccid': 'fixture-card', 'imsi': '001010000000001'}
        previous = {'name': name, 'index': 0, 'present': True, 'iccid': 'fixture-card',
                    'matched': 'line', 'identity_state': 'confirmed', 'generation': 1}
        with patch.object(main.hub, 'cards', {name: previous}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=inst), \
                patch.object(main.engine, 'read_run_json', return_value=None), \
                patch.object(main.sim, 'read_card') as read, \
                patch.object(main, '_probe_inserted_card', new=AsyncMock()) as cold:
            await main._on_card_insert_locked(name, 0, verify=True, retire=[])
            observed = dict(main.hub.cards[name])
        read.assert_not_called()
        cold.assert_not_awaited()
        self.assertEqual(observed['identity_state'], 'pending')
        self.assertEqual(observed['identity_reason'], 'running_card_unavailable')

    def test_confirmed_identity_has_no_periodic_apdu_retry(self):
        with patch.object(main.time, 'monotonic', return_value=100):
            self.assertFalse(main._identity_retry_due({
                'identity_state': 'confirmed', 'identity_retry_at': 0,
                'verified_at': main.time.time() - 3600,
            }))
            self.assertFalse(main._identity_retry_due({
                'identity_state': 'pending', 'identity_retry_at': 101,
            }))
            self.assertTrue(main._identity_retry_due({
                'identity_state': 'pending', 'identity_retry_at': 99,
            }))


class CachedProfileEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_card_calibrates_only_its_unique_se(self):
        cached = {'ses': [
            {'id': 'one', 'eid': 'fixture-euicc', 'profiles': [
                {'iccid': 'old', 'profileState': 'enabled'},
                {'iccid': 'current', 'profileState': 'disabled'}]},
            {'id': 'two', 'eid': 'fixture-other', 'profiles': [
                {'iccid': 'other', 'profileState': 'enabled'}]},
        ]}
        with patch.object(main, '_esim_resolve_reader', return_value=('reader', 0)), \
                patch.object(main, '_current_reader_iccid', return_value='current'), \
                patch.object(main, '_esim_cache_for_iccid', return_value=cached):
            result = await main.api_esim_chip_cached(reader='reader')
        self.assertEqual([p['profileState'] for p in result['ses'][0]['profiles']], ['disabled', 'enabled'])
        self.assertEqual(result['ses'][1]['profiles'][0]['profileState'], 'unknown')
        self.assertEqual(cached['ses'][0]['profiles'][0]['profileState'], 'enabled')


class MaintenanceReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_name_present_native_card_is_reprobed_after_maintenance(self):
        import asyncio
        from control.app.sim import CardInfo
        name = 'fixture-reader'
        observed = asyncio.Event()
        cards = {name: {'name': name, 'index': 0, 'present': True, 'iccid': 'old',
                        'identity_state': 'confirmed', 'generation': 1}}
        good = CardInfo(name, 0, True, iccid='replacement', imsi='001010000000001', mcc='001', mnc='01')
        async def broadcast(_message):
            if cards[name].get('iccid') == 'replacement':
                observed.set()
        with patch.object(main.hub, 'cards', cards), \
                patch.object(main.hub, 'card_probes', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.hub, 'broadcast', new=AsyncMock(side_effect=broadcast)), \
                patch.object(main.card, 'reader_states', return_value=[{'name': name, 'index': 0, 'present': True}]), \
                patch.object(main.card, 'wait_for_change', return_value=None), \
                patch.object(main.os.path, 'getmtime', side_effect=[main.time.time(), 0, 0, 0]), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=None), \
                patch.object(main.sim, 'read_card', return_value=good) as read, \
                patch.object(main, '_match_instance_by_iccid', return_value=None), \
                patch.object(main.cfg, 'card_auto_create_suppressed', return_value=True):
            monitor = asyncio.create_task(main.card_monitor())
            try:
                await asyncio.wait_for(observed.wait(), timeout=3)
            finally:
                monitor.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await monitor
            read.assert_called_once_with(0)
            self.assertEqual(cards[name]['iccid'], 'replacement')

    async def test_completed_full_rescan_bypasses_maintenance_cache_and_reprobes_card(self):
        import asyncio
        name = 'fixture-reader'
        announced = asyncio.Event()
        cards = {name: {'name': name, 'index': 0, 'present': True,
                        'iccid': 'old-card', 'identity_state': 'confirmed'}}

        async def broadcast(_message):
            announced.set()

        inserted = AsyncMock()
        with patch.object(main.hub, 'cards', cards), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main.hub, 'device_rescan_applied', 'previous'), \
                patch.object(main.hub, 'broadcast', new=AsyncMock(side_effect=broadcast)), \
                patch.object(main.operations, 'device_rescan_status', return_value={
                    'state': 'success', 'operation_id': '0123456789abcdef'}), \
                patch.object(main.card, 'reader_states', return_value=[{
                    'name': name, 'index': 0, 'present': True}]), \
                patch.object(main.card, 'wait_for_change', return_value=None), \
                patch.object(main.os.path, 'getmtime', return_value=main.time.time()), \
                patch.object(main, '_on_card_insert', new=inserted):
            monitor = asyncio.create_task(main.card_monitor())
            try:
                await asyncio.wait_for(announced.wait(), timeout=2)
            finally:
                monitor.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await monitor

        inserted.assert_awaited_once_with(name, 0, verify=True)
        self.assertTrue(main.hub.scanned)

    async def test_esim_refresh_updates_saved_subscription_not_only_memory(self):
        from control.app.sim import CardInfo
        old = {'id': 'line', 'iccid': 'fixture', 'imsi': 'old', 'mcc': '262', 'mnc': '01'}
        good = CardInfo('reader', 0, True, iccid='fixture', imsi='001010000000001', mcc='001', mnc='01')
        with patch.object(main.hub, 'cards', {}), \
                patch.object(main, '_match_instance_by_iccid', return_value=old), \
                patch.object(main.cfg, 'upsert_instance', side_effect=lambda update: {**old, **update}) as save:
            result = await main._esim_refresh_card('reader', 0, good, auto_start=False, broadcast=False)
        self.assertEqual(save.call_args.args[0]['mcc'], '001')
        self.assertEqual(save.call_args.args[0]['imsi'], good.imsi)
        self.assertEqual(result['mcc'], '001')


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_read_keeps_lock_and_cannot_publish_after_replacement(self):
        import asyncio
        from control.app.sim import CardInfo
        gate = asyncio.Event()
        name = 'fixture-reader'
        async def delayed(*_args):
            await gate.wait()
            return CardInfo(name, 0, True, iccid='old', imsi='001010000000001', mcc='001', mnc='01')
        with patch.object(main.hub, 'cards', {}), \
                patch.object(main.hub, 'reader_locks', {}), \
                patch.object(main.hub, 'card_probes', {}), \
                patch.object(main.hub, 'lpa_busy', {}), \
                patch.object(main, 'CARD_PROBE_TIMEOUT_SECONDS', .01), \
                patch.object(main.usbreader, 'port_for_index', return_value=None), \
                patch.object(main, '_find_running_by_reader', return_value=None), \
                patch.object(main, '_probe_inserted_card', new=AsyncMock(side_effect=delayed)) as probe:
            await main._on_card_insert(name, 0)
            self.assertEqual(main.hub.cards[name]['identity_state'], 'failed')
            self.assertTrue(main.hub.reader_lock(name).locked())
            await main._on_card_insert(name, 0)
            probe.assert_awaited_once()
            task = main.hub.card_probes[name]
            replacement = {'name': name, 'present': False, 'generation': 2}
            main.hub.cards[name] = replacement
            gate.set()
            await task
            self.assertIs(main.hub.cards[name], replacement)
            self.assertFalse(main.hub.reader_lock(name).locked())

    async def test_user_stop_waits_for_inflight_start_then_stops_it(self):
        import asyncio
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def starting(*_args, **_kw):
            entered.set()
            await release.wait()
            order.append('started')
        async def stopping(*_args):
            order.append('stopped')
        with patch.object(main.hub, 'instance_locks', {}), \
                patch.object(main.hub, 'manual_stops', set()), \
                patch.object(main.hub, 'hotplug_epochs', {}), \
                patch.object(main.hub, 'hotplug_pending', {}), \
                patch.object(main, '_start_instance_locked', new=AsyncMock(side_effect=starting)), \
                patch.object(main, '_stop_instance_locked', new=AsyncMock(side_effect=stopping)):
            start = asyncio.create_task(main._start_instance('fixture'))
            await entered.wait()
            stop = asyncio.create_task(main._stop_instance('fixture', 'user_requested'))
            await asyncio.sleep(0)
            self.assertEqual(order, [])
            self.assertIn('fixture', main.hub.manual_stops)
            release.set()
            await asyncio.gather(start, stop)
            self.assertEqual(order, ['started', 'stopped'])
