"""Runtime admission and port lifecycle; fake Docker, fictional SIMs, no hardware."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from control.app import config, engine


class Containers:
    def __init__(self):
        self.items = {}
        self.fail_start = False
        self.fail_remove = False

    def list(self, **kwargs):
        return list(self.items.values())

    def get(self, name):
        if name not in self.items:
            raise engine.docker.errors.NotFound(name)
        return self.items[name]

    def create(self, image, **kwargs):
        owner = self
        name = kwargs['name']
        bindings = {key: [{'HostPort': str(value[1] if isinstance(value, tuple) else value)}]
                    for key, value in kwargs['ports'].items()}

        class Container:
            id = name + '-generation'
            status = 'created'
            attrs = {'Config': {'Labels': kwargs['labels'], 'Image': image},
                     'HostConfig': {'PortBindings': bindings},
                     'State': {'Running': False, 'Status': 'created'}}

            def start(self):
                if owner.fail_start:
                    raise RuntimeError('fixture Docker start failed')
                self.status = 'running'
                self.attrs['State'] = {'Running': True, 'Status': 'running'}

            def reload(self):
                pass

            def stop(self, **kwargs):
                self.status = 'exited'

            def remove(self, **kwargs):
                if owner.fail_remove:
                    raise RuntimeError('fixture removal failed')
                owner.items.pop(name, None)

        result = Container()
        result.name = name
        self.items[name] = result
        return result


class RuntimeCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.multiple(config, DATA_DIR=self.temp,
                                        CONFIG_PATH=str(Path(self.temp) / 'config.yaml')))
        self.enterContext(patch.object(engine, 'DATA_DIR', self.temp))
        self.enterContext(patch.object(engine, '_starting', {}))
        self.enterContext(patch.object(engine, '_line_locks', {}))
        self.containers = Containers()
        self.enterContext(patch.object(engine, '_client', return_value=SimpleNamespace(containers=self.containers)))
        self.enterContext(patch.object(engine.egress, 'ensure_line', return_value={}))
        self.enterContext(patch.object(engine, '_host_pcsclite_library', return_value='/fixture/libpcsclite.so.1'))
        self.enterContext(patch.object(engine, 'capture_diagnostics'))
        self.enterContext(patch.object(engine, 'PCSC_RELEASE_SETTLE_SECONDS', 0))
        self.enterContext(patch.object(config, '_host_port_free', return_value=True))

    def line(self, iid, **values):
        return config.upsert_instance({
            'id': str(iid), 'iccid': 'fixture-card-' + str(iid),
            'imsi': '001010000000001', 'mcc': '001', 'mnc': '01', **values})

    def start(self, iid):
        return engine.start(config.get_instance(str(iid)), config.get_settings())

    def test_many_saved_cards_do_not_consume_running_capacity_or_ports(self):
        config.update_settings({'max_sim_lines': 1})
        for i in range(1, 41):
            self.line(i)
        self.start(40)
        with self.assertRaises(config.LineLimitError):
            self.start(1)
        first_ports = config.get_instance('40')['ports']
        engine.stop('40')
        self.start(1)
        self.assertEqual(config.get_instance('1')['ports'], first_ports)
        self.assertEqual(len(config.list_instances()), 40)
        self.assertEqual(config.get_instance('40')['iccid'], 'fixture-card-40')
        self.assertEqual(config.get_instance('40')['ports'], first_ports)

    def test_concurrent_starts_reserve_capacity_before_slow_egress(self):
        config.update_settings({'max_sim_lines': 1})
        self.line(1); self.line(2)
        entered, release = threading.Event(), threading.Event()

        def slow_egress(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {}

        with ThreadPoolExecutor(2) as pool, patch.object(engine.egress, 'ensure_line', side_effect=slow_egress):
            first = pool.submit(self.start, 1)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(config.LineLimitError):
                    pool.submit(self.start, 2).result(timeout=3)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(len(self.containers.items), 1)

    def test_unrelated_stop_does_not_wait_for_a_slow_start(self):
        self.line(1); self.line(2)
        self.start(1)
        entered, release = threading.Event(), threading.Event()

        def slow_egress(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {}

        with ThreadPoolExecutor(2) as pool, patch.object(engine.egress, 'ensure_line', side_effect=slow_egress):
            future = pool.submit(self.start, 2)
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(pool.submit(engine.stop, '1').result(timeout=2))
            finally:
                release.set()
            future.result(timeout=5)

    def test_stale_stop_cannot_free_the_replacement_container_slot(self):
        config.update_settings({'max_sim_lines': 1})
        self.line(1); self.line(2)
        self.start(1)
        self.assertFalse(engine.stop('1', expected_container_id='stale-generation'))
        with self.assertRaises(config.LineLimitError):
            self.start(2)

    def test_two_inflight_starts_cannot_lease_the_same_ports(self):
        self.line(1); self.line(2)
        barrier = threading.Barrier(2)
        real_write = config.write_instance_json

        def hold_before_docker(inst, settings):
            result = real_write(inst, settings)
            barrier.wait(5)
            return result

        with ThreadPoolExecutor(2) as pool, patch.object(config, 'write_instance_json', side_effect=hold_before_docker):
            futures = [pool.submit(self.start, i) for i in (1, 2)]
            for future in futures:
                future.result(timeout=10)
        first = config._block_ports(config.get_instance('1')['ports'])
        second = config._block_ports(config.get_instance('2')['ports'])
        self.assertFalse(first & second)

    def test_control_restart_uses_docker_and_lowered_limit_keeps_existing_lines(self):
        self.line(1); self.line(2); self.line(3)
        self.start(1); self.start(2)
        config.update_settings({'max_sim_lines': 1})
        with patch.object(engine, '_starting', {}):
            with self.assertRaises(config.LineLimitError):
                self.start(3)
            self.start(2)  # replacement keeps its existing slot
        self.assertEqual(len(self.containers.items), 2)

    def test_failed_start_releases_slot_and_ports_after_created_cleanup(self):
        config.update_settings({'max_sim_lines': 1})
        self.line(1); self.line(2)
        self.containers.fail_start = True
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            self.start(1)
        self.assertFalse(engine._starting)
        self.assertFalse(self.containers.items)
        self.containers.fail_start = False
        self.start(2)
        self.assertEqual(config.get_instance('1')['ports'], config.get_instance('2')['ports'])

    def test_failed_created_cleanup_keeps_resource_claim_until_removed(self):
        config.update_settings({'max_sim_lines': 1})
        self.line(1); self.line(2)
        self.containers.fail_start = self.containers.fail_remove = True
        with self.assertRaises(RuntimeError):
            self.start(1)
        self.containers.fail_start = False
        with self.assertRaises(config.LineLimitError):
            self.start(2)
        self.containers.fail_remove = False
        engine.stop('1')
        self.start(2)

    def test_egress_failure_and_docker_inspection_failure_never_leak_admission(self):
        self.line(1)
        with patch.object(engine.egress, 'ensure_line', side_effect=RuntimeError('egress unavailable')):
            with self.assertRaises(RuntimeError):
                self.start(1)
        self.assertFalse(engine._starting)
        with patch.object(self.containers, 'list', side_effect=RuntimeError('Docker unavailable')):
            with self.assertRaises(RuntimeError):
                self.start(1)
        self.assertFalse(self.containers.items)
        self.assertFalse(engine._starting)

    def test_live_host_conflict_reallocates_auto_but_manual_fails_closed(self):
        self.line(1, ports=config._alloc_ports(0))
        with patch.object(config, '_host_port_free', side_effect=lambda port, **kw: port != 8089):
            self.start(1)
        self.assertNotEqual(config.get_instance('1')['ports']['webrtc'], 8089)
        self.line(2, port_mode='manual', sip_port=5060)
        with patch.object(config, '_host_port_free', return_value=False):
            with self.assertRaises(config.PortAllocationError):
                self.start(2)
        self.assertFalse(engine._starting)

    def test_deleted_line_is_not_recreated_during_port_allocation(self):
        inst = self.line(1)
        config.delete_instance('1')
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            engine.start(inst, config.get_settings())
        self.assertIsNone(config.get_instance('1'))
        self.assertFalse(self.containers.items)

    def test_concurrent_discovery_assigns_unique_ids_and_deduplicates_same_card(self):
        def discover(i):
            return config.upsert_instance({'iccid': f'fixture-card-{i}', 'name': 'Fixture'}, unique_name=True)
        with ThreadPoolExecutor(8) as pool:
            saved = list(pool.map(discover, range(40)))
            repeated = list(pool.map(discover, [39] * 8))
        self.assertEqual(len({x['id'] for x in saved}), 40)
        self.assertEqual(len(config.list_instances()), 40)
        self.assertEqual({x['id'] for x in repeated}, {saved[39]['id']})
        self.assertTrue(all('ports' not in x for x in saved))


if __name__ == '__main__':
    unittest.main()
