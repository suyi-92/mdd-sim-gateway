"""Exercise recovery scheduling with a fake clock and isolated request files."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_identity_fixture import verified_bridge
from host import mdd_orchestrator as orch


class RecoverySchedulingTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = orch.Orchestrator(root / 'data', root, dry_run=True,
                                     state=root / 'state', backup=root / 'backup')
        self.app.root.mkdir(parents=True)
        self.now = 1000.0
        self.sleeps = []
        self.enterContext(patch.object(orch.time, 'time', side_effect=lambda: self.now))
        self.enterContext(patch.object(self.app, '_usb_fingerprint', return_value=(0, 0)))
        self.enterContext(patch.object(self.app._stop_event, 'wait', side_effect=self.wait))

    def wait(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        return False

    def request(self):
        value = {'request_id': 'fixture-request', 'device_id': 'fixture-modem',
                 'expected_iccid_sha256': hashlib.sha256(b'fixture-card').hexdigest(),
                 'requested_at': self.now}
        orch.atomic_json(self.app.bridge_restart_request_dir / 'fixture-request.json', value)
        return value

    def test_request_arriving_during_reconcile_is_consumed_without_idle_sleep(self):
        app = self.app
        # Leave the production loop and request consumer intact. Substitute only the
        # host/Docker/network work with an unchanged system and a request arriving late.
        values = {
            'settle_service_restart': None, 'process_backup_operation_request': None,
            'process_service_restart_request': None, 'retire_obsolete_services': None,
            'reconcile_timezone': None, 'process_device_rescan_request': None,
            'usb_modems': [], 'migrate_device_ids': None, 'desired_devices': ({}, False),
            'cellular_backend_needed': False, 'country_egress_required': False,
            'active_exit_tests': set(), 'service_active': False,
            'apply_device_radios': None, 'police_orphaned_modem_profiles': None,
            'proxy_reconcile_desired': {}, 'reconcile_proxy': None,
            'reconcile_hardware': {}, 'publish_device_status': None,
            'finish_device_rescan': None,
        }
        for name, value in values.items():
            self.enterContext(patch.object(app, name, return_value=value))
        app._last_conclusion = json.dumps([[], {}, False, False, False, {}])

        def publish(*_args):
            if not app._bridge_restarts:
                self.request()

        def finish(_present):
            if app._bridge_restarts:
                app.stop = True

        with patch.object(app, 'publish_host_diagnostics', side_effect=publish), \
                patch.object(app, 'finish_bridge_restart_requests', side_effect=finish):
            app.loop()

        accepted = app._bridge_restarts['fixture-request']
        self.assertEqual(accepted['state'], 'stopped')
        self.assertEqual(accepted['started_at'], accepted['requested_at'])
        self.assertEqual(self.sleeps, [], 'a request queued during a pass must not wait 15 seconds')

    def test_only_unfinished_recovery_uses_short_waits(self):
        cases = [([], 15), (['channels_ready'], 15), (['failed'], 15),
                 (['stopped'], 1), (['resetting'], 1), (['spawned'], 1),
                 (['channels_ready', 'spawned', 'failed'], 1)]
        for states, expected in cases:
            with self.subTest(states=states):
                self.app._bridge_restarts = {
                    str(i): {'state': state} for i, state in enumerate(states)}
                self.sleeps.clear()
                self.app._sleep_for_work(15)
                self.assertAlmostEqual(sum(self.sleeps), expected)

    def test_request_arriving_during_idle_sleep_still_wakes_on_next_poll(self):
        def wait(seconds):
            self.wait(seconds)
            self.request()
            return False
        with patch.object(self.app._stop_event, 'wait', side_effect=wait):
            self.app._sleep_for_work(15)
        self.assertLessEqual(sum(self.sleeps), .5)
        self.assertEqual(len(self.sleeps), 1)

    def test_ready_bridge_is_confirmed_after_short_wait_without_weakening_identity_gate(self):
        request = {**self.request(), 'started_at': self.now, 'state': 'spawned',
                   'modem_reset': True}
        self.app._bridge_restarts = {'fixture-request': request}
        self.app.bridges['fixture-modem'] = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        self.app.cellular_states['fixture-modem'] = {'sim_iccid': 'old-card'}
        metadata = self.app.data / 'modems/fixture-modem.json'
        identity = {**verified_bridge(), 'iccid': 'fixture-card', 'channel_allocated': 3}

        def wait(seconds):
            self.wait(seconds)
            orch.atomic_json(metadata, identity)
            return False

        with patch.object(self.app._stop_event, 'wait', side_effect=wait):
            self.app._sleep_for_work(15)
        self.app.finish_bridge_restart_requests({'fixture-modem'})
        self.assertEqual(self.app._bridge_restarts['fixture-request']['state'], 'channels_ready')
        self.assertLessEqual(sum(self.sleeps), 1)
        self.sleeps.clear()
        self.app._sleep_for_work(15)
        self.assertEqual(sum(self.sleeps), 15, 'successful recovery must restore idle backoff')

    def test_recovery_timeout_restores_idle_backoff(self):
        self.app._bridge_restarts = {'fixture-request': {
            'request_id': 'fixture-request', 'device_id': 'fixture-modem',
            'started_at': self.now - 101, 'state': 'stopped', 'modem_reset': True}}
        self.app.finish_bridge_restart_requests(set())
        self.assertEqual(self.app._bridge_restarts['fixture-request']['state'], 'failed')
        self.app._sleep_for_work(15)
        self.assertEqual(sum(self.sleeps), 15)

    def test_flight_mode_wait_uses_idle_backoff_until_an_input_change(self):
        self.app._cellular_recoveries = {
            'fixture-modem': {'state': 'waiting_flight_mode', 'deadline_at': 0.0}}
        self.assert_wait(15)

    def test_running_service_restart_uses_short_health_polling(self):
        orch.atomic_json(self.app.root / 'service-restart-status.json', {
            'state': 'running', 'scope': 'services', 'updated_at': int(self.now)})
        self.assert_wait(1)
        orch.atomic_json(self.app.root / 'service-restart-status.json', {
            'state': 'success', 'scope': 'services', 'updated_at': int(self.now)})
        self.assert_wait(15)

    def test_shutdown_interrupts_recovery_wait(self):
        self.app._bridge_restarts = {'fixture-request': {'state': 'stopped'}}
        def wait(seconds):
            self.wait(seconds)
            self.app.request_stop()
            return True
        with patch.object(self.app._stop_event, 'wait', side_effect=wait):
            self.app._sleep_for_work(15)
        self.assertTrue(self.app.stop)
        self.assertEqual(len(self.sleeps), 1)

    def publish_capability(self, *, radio=None, flight=False, data=False,
                           data_active=False, available=True, failed_reason="",
                           rejection=None, generation="generation-a"):
        device_id = 'fixture-modem'
        self.app.radio_states[device_id] = radio
        self.app.cellular_states[device_id] = {
            'available': available, 'data_active': data_active,
            'failed_reason': failed_reason, 'network_reject': rejection or {},
        }
        with patch.object(self.app, 'service_active', return_value=not flight):
            self.app.publish_device_status(
                {device_id: {'flight_mode': flight, 'cellular_enabled': data}},
                {device_id: {'id': device_id, 'usb_generation': generation}})

    def assert_wait(self, seconds):
        self.sleeps.clear()
        self.app._sleep_for_work(15)
        self.assertAlmostEqual(sum(self.sleeps), seconds)

    def test_ordinary_radio_discovery_and_data_use_short_wait_until_confirmed(self):
        self.publish_capability(radio=None, available=False)
        self.assert_wait(1)
        # A retained radio observation is insufficient while MM has no current modem.
        self.publish_capability(radio=True, available=False)
        self.assert_wait(1)
        self.publish_capability(radio=True, data=True)
        self.assert_wait(1)
        self.publish_capability(radio=True, data=True, data_active=True)
        self.assert_wait(15)
        self.publish_capability(radio=True, flight=True)
        self.assert_wait(1)
        self.publish_capability(radio=False, flight=True)
        self.assert_wait(15)

    def test_rejection_missing_sim_and_terminal_recovery_restore_idle(self):
        self.publish_capability(radio=True, data=True)
        self.assert_wait(1)
        self.publish_capability(radio=True, data=True, rejection={'cause_code': 7})
        self.assert_wait(15)
        self.publish_capability(radio=False, failed_reason='sim-missing')
        self.assert_wait(15)
        self.app._cellular_recoveries['fixture-modem'] = {'state': 'failed'}
        self.publish_capability(radio=False)
        self.assert_wait(15)

    def test_unresolved_capability_fast_poll_expires_and_new_generation_gets_budget(self):
        self.publish_capability(radio=False)
        deadline = self.app._capability_poll['fixture-modem'][1]
        self.now = deadline + 1
        self.publish_capability(radio=False)
        self.assert_wait(15)
        self.publish_capability(radio=False)
        self.assertEqual(self.app._capability_poll['fixture-modem'][1], deadline)
        self.assert_wait(15)
        self.publish_capability(radio=False, generation='generation-b')
        self.assert_wait(1)
        self.publish_capability(radio=True)
        self.assert_wait(15)

    def deferred_cellular_recovery(self, **fields):
        return self.app._set_cellular_recovery(
            'fixture-modem', 'waiting_flight_mode',
            usb_generation='generation-a',
            expected_iccid_sha256=hashlib.sha256(b'fixture-card').hexdigest(),
            **fields)

    def test_flight_wait_survives_restart_then_resumes_bounded_identity_wait(self):
        self.deferred_cellular_recovery(power_cycle_completed=True,
                                       identity_deadline_at=self.now + 10)
        self.now += 86400
        resumed = orch.Orchestrator(self.app.data, self.app.repo, dry_run=True)
        modem = {'id': 'fixture-modem', 'usb_generation': 'generation-a'}
        wanted = {'fixture-modem': {'flight_mode': True}}
        with patch.object(resumed, 'modem_snapshot') as snapshot:
            self.assertEqual(resumed.process_cellular_recoveries([modem], wanted, False), set())
        snapshot.assert_not_called()
        self.assertEqual(resumed._cellular_recoveries['fixture-modem']['deadline_at'], 0)
        wanted['fixture-modem']['flight_mode'] = False
        with patch.object(resumed, 'modem_snapshot', return_value={'sim_iccid': 'stale-card'}), \
                patch.object(orch, 'run') as run:
            self.assertEqual(resumed.process_cellular_recoveries([modem], wanted, True),
                             {'fixture-modem'})
            recovery = resumed._cellular_recoveries['fixture-modem']
            self.assertEqual(recovery['state'], 'waiting_identity')
            self.assertEqual(recovery['deadline_at'], self.now + orch.ESIM_CELLULAR_RECOVERY_SECONDS)
            self.assertEqual(recovery['identity_deadline_at'],
                             self.now + orch.ESIM_CELLULAR_IDENTITY_WAIT_SECONDS)
            self.now = recovery['identity_deadline_at'] + 1
            resumed.process_cellular_recoveries([modem], wanted, True)
        run.assert_not_called()
        self.assertEqual(resumed._cellular_recoveries['fixture-modem']['error_code'],
                         'sim_identity_mismatch')

    def test_paused_recovery_still_cancels_changed_usb_before_any_radio_work(self):
        self.deferred_cellular_recovery()
        self.now += 86400
        with patch.object(self.app, 'modem_snapshot') as snapshot:
            self.app.process_cellular_recoveries(
                [{'id': 'fixture-modem', 'usb_generation': 'generation-b'}],
                {'fixture-modem': {'flight_mode': True}}, False)
        snapshot.assert_not_called()
        self.assertEqual(self.app._cellular_recoveries['fixture-modem']['state'], 'cancelled')

    def test_resumed_baseband_budget_expires_without_extending_each_pass(self):
        self.deferred_cellular_recovery()
        self.now += 86400
        modem = {'id': 'fixture-modem', 'usb_generation': 'generation-a'}
        wanted = {'fixture-modem': {'flight_mode': False}}
        self.app.process_cellular_recoveries([modem], wanted, False)
        deadline = self.app._cellular_recoveries['fixture-modem']['deadline_at']
        self.now += 10
        self.app.process_cellular_recoveries([modem], wanted, False)
        self.assertEqual(self.app._cellular_recoveries['fixture-modem']['deadline_at'], deadline)
        self.now = deadline + 1
        self.assertEqual(self.app.process_cellular_recoveries([modem], wanted, False),
                         {'fixture-modem'})
        self.assertEqual(self.app._cellular_recoveries['fixture-modem']['error_code'],
                         'initialization_timeout')
        self.assert_wait(15)
