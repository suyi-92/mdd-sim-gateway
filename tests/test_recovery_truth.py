"""Current hardware evidence, never saved intent or an old task, completes recovery."""
import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import main
from host import mdd_orchestrator as host


class CapabilityTruthTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_intent_and_settled_sim_failure_cannot_complete_rf_enable(self):
        wanted = {'flight_mode': False, 'cellular_enabled': False, 'vowifi_enabled': False}
        observed = {'devices': {'modem': {'present': True, 'desired': wanted,
                     'transitioning': False, 'actual': {'cellular_radio_enabled': False},
                     'cellular': {'failed_reason': 'sim-missing'}}}}
        with patch.object(main.device_state, 'status', return_value=observed):
            with self.assertRaisesRegex(RuntimeError, 'not initialized'):
                await main._wait_for_device_request('modem', wanted)
            # Turning data OFF still succeeds when data is already off, even if RF is
            # unavailable for a different reason; this action does not promise RF recovery.
            await main._wait_for_device_request('modem', wanted, requested={'cellular_enabled': False})

    async def test_waits_for_actual_radio_then_actual_data_and_permits_radio_off(self):
        wanted = {'flight_mode': False, 'cellular_enabled': True}
        row = {'present': True, 'desired': wanted, 'actual': {'cellular_radio_enabled': False},
               'cellular': {'data_active': False}}
        observations = []
        def advance(_seconds):
            observations.append(1)
            if len(observations) == 1:
                row['actual']['cellular_radio_enabled'] = True
            else:
                row['cellular']['data_active'] = True
        with patch.object(main.device_state, 'status', return_value={'devices': {'modem': row}}), \
                patch.object(main.asyncio, 'sleep', new=AsyncMock(side_effect=advance)):
            await main._wait_for_device_request('modem', wanted)
        self.assertEqual(len(observations), 2)
        wanted.update(flight_mode=True)
        row['actual']['cellular_radio_enabled'] = False
        row['cellular'].update(data_active=False, failed_reason='sim-missing')
        with patch.object(main.device_state, 'status', return_value={'devices': {'modem': row}}):
            await main._wait_for_device_request('modem', wanted)


class HostTruthTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = host.Orchestrator(root / 'data', root)
        self.task = {'state': 'ready', 'operation_id': 'current', 'usb_generation': 'usb-a',
                     'expected_iccid_sha256': hashlib.sha256(b'fixture-card').hexdigest()}
        self.app._cellular_recoveries['modem'] = dict(self.task)

    def test_old_ready_is_hidden_for_missing_subscription_new_usb_or_other_card(self):
        good = {'sim_iccid': 'fixture-card', 'sim_present': True, 'subscription_available': True}
        for state, generation in (({**good, 'subscription_available': False}, 'usb-a'),
                                  (good, 'usb-b'), ({**good, 'sim_iccid': 'other'}, 'usb-a')):
            self.app.cellular_states['modem'] = state
            self.assertEqual(self.app._current_cellular_recovery('modem', {'usb_generation': generation}), {})
            self.assertEqual(self.app._cellular_recoveries['modem'], self.task)
        self.app.cellular_states['modem'] = good
        self.assertEqual(self.app._current_cellular_recovery('modem', {'usb_generation': 'usb-a'})['state'], 'ready')

    def test_same_iccid_without_subscription_runs_scoped_uim_cycle_once(self):
        task = self.app._cellular_recoveries['modem']
        task.update(state='pending', deadline_at=time.time() + 120)
        modem = {'id': 'modem', 'tty': '/dev/ttyUSB9', 'usb_generation': 'usb-a'}
        snapshot = {'available': True, 'sim_present': True, 'subscription_available': False,
                    'sim_iccid': 'fixture-card', 'mm_object': '/org/freedesktop/ModemManager1/Modem/9'}
        with patch.object(self.app, 'modem_snapshot', return_value=snapshot), \
                patch.object(self.app, 'stop_bridge') as stop, \
                patch.object(host, 'run', return_value=SimpleNamespace(returncode=0,
                    stdout='modem.generic.primary-port: cdc-wdm9\nmodem.generic.primary-sim-slot: 1')) as run:
            for _ in range(2):
                self.assertEqual(self.app.process_cellular_recoveries([modem], {'modem': {'flight_mode': False}}, True), {'modem'})
        stop.assert_called_once_with('modem')
        self.assertEqual(len(run.call_args_list), 3)
        self.assertTrue(self.app._cellular_recoveries['modem']['power_cycle_completed'])
        self.assertEqual(self.app._cellular_recoveries['modem']['state'], 'waiting_identity')

    def test_queued_request_for_old_usb_does_not_stop_new_device(self):
        host.atomic_json(self.app.hw_state_path, {'assignments': {'modem': {'usb_generation': 'usb-b'}}})
        host.atomic_json(self.app.bridge_restart_request_dir / 'request.json', {
            'request_id': 'request', 'device_id': 'modem', 'usb_generation': 'usb-a',
            'expected_iccid_sha256': self.task['expected_iccid_sha256']})
        with patch.object(self.app, '_stop_bridge_process') as stop:
            self.app.process_bridge_restart_requests()
        stop.assert_not_called()
        self.assertEqual(self.app._bridge_restarts['request']['state'], 'failed')
