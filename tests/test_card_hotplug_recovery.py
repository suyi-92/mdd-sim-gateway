import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, sim, usbreader


class NativeInsertRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.name, self.port = 'SCR fixture', '3-2'
        self.failed = sim.CardInfo(self.name, 0, True, iccid='fixture-card', transport_error=True)
        self.ready = sim.CardInfo(self.name, 1, True, iccid='fixture-card')
        self.read = self.enterContext(patch.object(sim, 'read_card', side_effect=[self.failed, self.ready]))
        self.enterContext(patch.object(sim, 'list_readers', return_value=['other reader', self.name]))
        self.recover = self.enterContext(patch.object(usbreader, 'recover_scr_prime', return_value=True))
        self.owner = self.enterContext(patch.object(main, '_find_running_by_reader', return_value=None))

    async def test_one_recovery_after_transport_failure_re_resolves_the_same_reader(self):
        result = await main._probe_inserted_card(self.name, 0, self.port)
        self.assertIs(result, self.ready)
        self.recover.assert_called_once_with(self.name, self.port)
        self.assertEqual([c.args[0] for c in self.read.call_args_list], [0, 1])

    async def test_no_recovery_on_normal_insert_or_pin_application_error(self):
        self.failed.transport_error = False
        self.failed.error = 'PIN required'
        self.assertIs(await main._probe_inserted_card(self.name, 0, self.port), self.failed)
        self.recover.assert_not_called()

    async def test_running_line_is_never_reset(self):
        self.owner.return_value = {'id': 'other'}
        self.assertIs(await main._probe_inserted_card(self.name, 0, self.port), self.failed)
        self.recover.assert_not_called()

    async def test_modem_channels_are_not_native_usb_recovery_targets(self):
        name = 'VoWiFi Modem fixture 00 00'
        self.assertIs(await main._probe_inserted_card(name, 0, self.port), self.failed)
        self.recover.assert_not_called()

    async def test_failed_reconnect_does_not_loop(self):
        self.recover.return_value = False
        self.assertIs(await main._probe_inserted_card(self.name, 0, self.port), self.failed)
        self.recover.assert_called_once()
        self.read.assert_called_once()

    async def test_second_failed_read_is_returned_without_another_reset(self):
        self.ready.transport_error = True
        self.assertIs(await main._probe_inserted_card(self.name, 0, self.port), self.ready)
        self.recover.assert_called_once()

    async def test_reader_index_race_cannot_attribute_another_card(self):
        self.ready.reader = 'different reader'
        with self.assertRaisesRegex(RuntimeError, 'enumeration changed'):
            await main._probe_inserted_card(self.name, 0, self.port)


class CardTransportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        reader = Mock()
        reader.__str__ = lambda _: 'SCR fixture'
        reader.createConnection.return_value = self.conn
        self.enterContext(patch.object(sim, 'readers', return_value=[reader]))
        self.enterContext(patch.object(sim.usbreader, 'port_for_index', return_value='3-2'))
        self.enterContext(patch.object(sim, '_Tx', side_effect=lambda _: nullcontext()))

    def test_failed_connect_is_distinguished_from_an_application_rejection(self):
        self.conn.connect.side_effect = sim.CardConnectionException('card absent or mute')
        self.assertTrue(sim.read_card(0).transport_error)

    def test_partial_identity_does_not_hide_a_transport_failure(self):
        with patch.object(sim, '_transmit', return_value=([], 0x90, 0)), \
                patch.object(sim, '_read_binary', return_value=([0], 0x90, 0)), \
                patch.object(sim, 'dec_iccid', return_value='fixture-card'), \
                patch.object(sim, '_select_adf_usim', side_effect=sim.CardConnectionException('card not transacted')):
            result = sim.read_card(0)
        self.assertEqual(result.iccid, 'fixture-card')
        self.assertTrue(result.transport_error)
        self.conn.disconnect.assert_called_once()

    def test_missing_application_is_not_a_transport_error(self):
        with patch.object(sim, '_transmit', return_value=([], 0x90, 0)), \
                patch.object(sim, '_read_binary', return_value=([0], 0x90, 0)), \
                patch.object(sim, '_select_adf_usim', return_value=False):
            self.assertFalse(sim.read_card(0).transport_error)


class ReaderResetIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.port = Path(self.temp) / '3-2'
        self.port.mkdir()
        (self.port / 'idVendor').write_text('04d9')
        (self.port / 'idProduct').write_text('c001')
        self.enterContext(patch.object(usbreader, '_SYS_USB', self.temp))
        self.enterContext(patch.object(usbreader, '_SCARD_OK', True))
        self.enterContext(patch.object(usbreader, 'SCardEstablishContext', return_value=(0, 'context')))
        self.connect = self.enterContext(patch.object(usbreader, 'SCardConnect', return_value=(0, 'handle', 0)))
        self.attr = self.enterContext(patch.object(usbreader, 'SCardGetAttrib', return_value=(0, list((0x00200307).to_bytes(4, 'little')))))
        self.map_port = self.enterContext(patch.object(usbreader, '_port_path_for', return_value='3-2'))
        self.reset = self.enterContext(patch.object(usbreader, 'SCardReconnect', return_value=(0, 1)))
        self.disconnect = self.enterContext(patch.object(usbreader, 'SCardDisconnect'))
        self.release = self.enterContext(patch.object(usbreader, 'SCardReleaseContext'))

    def test_recovery_requires_the_open_handles_port_and_real_vid_pid(self):
        self.assertTrue(usbreader.recover_scr_prime('SCR fixture', '3-2'))
        self.reset.assert_called_once_with('handle', usbreader.SCARD_SHARE_SHARED,
            usbreader.SCARD_PROTOCOL_T0 | usbreader.SCARD_PROTOCOL_T1, usbreader.SCARD_RESET_CARD)
        self.disconnect.assert_called_once_with('handle', usbreader.SCARD_LEAVE_CARD)
        self.release.assert_called_once_with('context')

    def test_a_reused_name_at_another_port_is_not_reset(self):
        self.assertFalse(usbreader.recover_scr_prime('SCR fixture', '3-9'))
        self.reset.assert_not_called()
        self.disconnect.assert_called_once()

    def test_a_matching_name_is_not_enough_for_another_device_type(self):
        (self.port / 'idProduct').write_text('ffff')
        self.assertFalse(usbreader.recover_scr_prime('SCR fixture', '3-2'))
        self.reset.assert_not_called()

    def test_reconnect_failure_still_releases_the_handle(self):
        self.reset.return_value = (1, 0)
        self.assertFalse(usbreader.recover_scr_prime('SCR fixture', '3-2'))
        self.disconnect.assert_called_once()
        self.release.assert_called_once()


class HotplugExitRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inst = {'id': 'fixture', 'enabled': True, 'iccid': 'fixture-card'}
        self.cards = [{'present': True, 'iccid': 'fixture-card'}]
        self.enterContext(patch.object(main.hub, 'hotplug_starts', set()))
        self.sleep = self.enterContext(patch.object(main.asyncio, 'sleep', new=AsyncMock()))
        self.enterContext(patch.object(main.cfg, 'get_instance', return_value=self.inst))
        self.enterContext(patch.object(main.cfg, 'get_settings', return_value={}))
        self.enterContext(patch.object(main.engine, 'is_running', return_value=False))
        self.enterContext(patch.object(main.hub, 'cards_list', side_effect=lambda: self.cards))
        self.enterContext(patch.object(main, '_device_for_card', return_value=('device', 'reader')))
        self.enterContext(patch.object(main.device_state, 'desired', return_value={'defaults': {'vowifi_enabled': True}}))
        self.enterContext(patch.object(main.hub, 'broadcast', new=AsyncMock()))
        self.enterContext(patch.object(main.hub, 'reset_health'))
        self.enterContext(patch.object(main, '_record_lifecycle'))
        self.start = self.enterContext(patch.object(main, '_start_engine_checked'))
        self.cold_exit = main.HTTPException(503, {'code': 'egress_unavailable'})

    async def test_cold_exit_gets_bounded_retries_then_starts(self):
        self.start.side_effect = [self.cold_exit, self.cold_exit, 'container']
        await main._auto_start_hotplugged_line('fixture')
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [6, 5, 5])

    async def test_removal_during_retry_cancels_start(self):
        def failed(*args):
            self.cards.clear()
            raise self.cold_exit
        self.start.side_effect = failed
        await main._auto_start_hotplugged_line('fixture')
        self.start.assert_called_once()

    async def test_disabling_the_line_during_retry_is_respected(self):
        def failed(*args):
            self.inst['enabled'] = False
            raise self.cold_exit
        self.start.side_effect = failed
        await main._auto_start_hotplugged_line('fixture')
        self.start.assert_called_once()

    async def test_persistent_exit_failure_stops_after_six_attempts(self):
        self.start.side_effect = self.cold_exit
        await main._auto_start_hotplugged_line('fixture')
        self.assertEqual(self.start.call_count, 6)
        self.assertNotIn('fixture', main.hub.hotplug_starts)

    async def test_card_and_pin_errors_are_not_retried_as_network_errors(self):
        self.start.side_effect = main.HTTPException(409, {'code': 'pin_invalid'})
        await main._auto_start_hotplugged_line('fixture')
        self.start.assert_called_once()
