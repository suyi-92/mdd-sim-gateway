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
        self.assertEqual(self.app._bridge_restarts['fixture-request']['state'], 'spawned')
        self.app.cellular_states['fixture-modem'] = {'sim_iccid': 'fixture-card'}
        self.app._sleep_for_work(15)
        self.app.finish_bridge_restart_requests({'fixture-modem'})
        self.assertEqual(self.app._bridge_restarts['fixture-request']['state'], 'channels_ready')
        self.assertLessEqual(sum(self.sleeps), 2)
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
