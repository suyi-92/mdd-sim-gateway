import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control.app import cellular_network as network, main, phone_identity
from host.mdd_orchestrator import Orchestrator


class RegistrationBudgetTests(unittest.TestCase):
    def test_time_spent_waiting_for_the_worker_cannot_restart_the_budget(self):
        runner = Mock()
        with self.assertRaises(network.CellularRegistrationError) as caught:
            network.register('/org/freedesktop/ModemManager1/Modem/9', mode='automatic',
                             runner=runner, deadline=180, clock=lambda: 181)
        self.assertEqual(caught.exception.detail['code'], 'operation_timeout')
        runner.assert_not_called()

    def test_hanging_registration_and_recovery_share_one_budget(self):
        now = [0.0]
        commands = []

        def runner(args, **kwargs):
            if args[-1] == '--output-json':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'modem': {
                    'generic': {'plugin': 'quectel', 'ports': ['cdc-wdm9 (qmi)', 'ttyUSB9 (at)']},
                    '3gpp': {'registration-state': 'searching'}}}))
            commands.append((args, kwargs['timeout']))
            if args[0] == 'busctl':
                self.assertLess(int(args[-1]), kwargs['timeout'])
            now[0] += kwargs['timeout']
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])

        phases = []
        with self.assertRaises(network.CellularRegistrationError) as caught:
            network.register('/org/freedesktop/ModemManager1/Modem/9', mode='manual',
                operator_id='00101', runner=runner, clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds), progress=phases.append)
        self.assertLessEqual(now[0], 180)
        self.assertEqual(caught.exception.detail['code'], 'operation_timeout')
        self.assertNotEqual(caught.exception.detail['recovery']['state'], 'restored')
        self.assertEqual(phases, ['registering', 'confirming', 'restoring'])
        self.assertTrue(any('--3gpp-register-home' in args for args, _ in commands))


class InternationalNumberTests(unittest.TestCase):
    def test_international_type_outweighs_a_different_sim_home_country(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path(temp))
            reply = SimpleNamespace(returncode=0, stdout='+CNUM: ,"12025550123",145')
            with patch('host.mdd_orchestrator.run', return_value=reply) as run:
                self.assertEqual(app.modem_number('/modem/9', 'card-a', '12025550123', '454'), '+12025550123')
                self.assertEqual(app.modem_number('/modem/9', 'card-a', '12025550123', '454'), '+12025550123')
                run.assert_called_once()
            with patch('host.mdd_orchestrator.run', return_value=SimpleNamespace(
                    returncode=0, stdout='+CNUM: ,"12025550100",145')):
                self.assertEqual(app.modem_number('/modem/9', 'card-b', '12025550123', '454'), '12025550123')

    def test_unknown_or_national_type_does_not_invent_a_country_code(self):
        for kind in (None, 129, 161):
            self.assertEqual(Orchestrator.normalize_msisdn('12025550123', '454', kind), '12025550123')

    def test_number_region_does_not_follow_sim_mcc_or_all_plus_one_numbers(self):
        self.assertEqual(phone_identity.number_country('+12025550123'), 'us')
        self.assertEqual(phone_identity.number_country('+14165550123'), 'ca')
        self.assertEqual(phone_identity.number_country('12025550123'), '')
        self.assertEqual(phone_identity.number_country('+100'), '')

    def test_esim_provider_does_not_overwrite_technical_home_network(self):
        cache = {'ses': [{'profiles': [{'iccid': 'card-a', 'serviceProviderName': 'Saily'}]}]}
        with patch.object(main, '_esim_cache_for_iccid', return_value=cache), \
                patch.object(main.carrier_id, 'lookup', return_value={
                    'name': 'Home operator', 'home_network': 'Home operator', 'plmn': '454-00'}):
            carrier = main._carrier_description({'iccid': 'card-a', 'mcc': '454', 'mnc': '00'}, {})
            other = main._carrier_description({'iccid': 'other-card'}, {})
        self.assertEqual(carrier['name'], 'Saily')
        self.assertEqual(carrier['plmn'], '454-00')
        self.assertEqual(carrier['home_network'], 'Home operator')
        self.assertEqual(other['name'], 'Home operator')
