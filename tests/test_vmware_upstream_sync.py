"""Behavior at the VMware / 1.9.1 integration boundaries; no real hardware or services."""
import asyncio
import base64
import json
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import main, sim, store
from engine import pin_keeper, ami_usim
from tests.test_sms_concat import TempStore


class UiccExchangeTests(unittest.TestCase):
    def exchanges(self):
        return (sim._transmit, pin_keeper._transmit, ami_usim._xfr)

    def test_response_length_retry_corrects_get_response_not_the_select(self):
        for exchange in self.exchanges():
            commands = []
            responses = iter([([1], 0x9F, 1), ([], 0x6C, 0), ([2], 0x90, 0)])
            def transmit(command):
                commands.append(command)
                return next(responses)
            with self.subTest(exchange=exchange):
                self.assertEqual(exchange(SimpleNamespace(transmit=transmit),
                                          [0, 0xA4, 0, 4, 2, 0x2F, 0, 0]),
                                 ([1, 2], 0x90, 0))
                self.assertEqual(commands[-1], [0, 0xC0, 0, 0, 0])

    def test_acceptance_exception_never_hides_read_or_authentication_errors(self):
        for exchange in self.exchanges():
            for instruction in (0xA4, 0xB0, 0x88):
                replies = iter([([1], 0x61, 2), ([], 0x69, 0x82)])
                conn = SimpleNamespace(transmit=lambda apdu: next(replies))
                with self.subTest(exchange=exchange, instruction=instruction):
                    result = exchange(conn, [0, instruction, 0, 0, 0])
                    self.assertEqual(result[1:], (0x90, 0) if instruction == 0xA4 else (0x69, 0x82))

    def test_repeated_continuations_fail_instead_of_succeeding(self):
        for exchange in self.exchanges():
            conn = SimpleNamespace(transmit=lambda apdu: ([], 0x61, 1))
            with self.subTest(exchange=exchange), self.assertRaisesRegex(RuntimeError, 'too many'):
                exchange(conn, [0, 0xA4, 0, 0, 0])

    def test_sip_uses_the_canonical_engine_exchange(self):
        self.assertIs(ami_usim._shared_transmit, pin_keeper._transmit)


class PinProofTests(unittest.TestCase):
    def setUp(self):
        self.identity = '8900000000000000001'
        self.inst = {'id': '2', 'iccid': self.identity, 'pin': '1234'}
        self.cards = {'reader': {'index': 1, 'present': True, 'pin_enabled': False,
                                 'iccid': self.identity, 'reader_port': '1-2'}}
        self.proof = {'iccid': self.identity, 'pin_enabled': False,
                      'observed_at': time.monotonic(),
                      'readers': [{'name': 'reader', 'index': 1, 'reader_port': '1-2'}]}

    def test_recent_switch_proof_uses_live_iccid_and_avoids_a_second_full_scan(self):
        with patch.object(main.hub, 'cards', self.cards), \
             patch.object(sim, 'read_iccid', return_value=self.identity) as identity, \
             patch.object(sim, 'read_card') as full:
            result = main._preflight_pin_locked(self.inst, 1, self.proof)
        self.assertEqual(result, {'ok': True, 'need_pin': False})
        identity.assert_called_once_with(1)
        full.assert_not_called()

    def test_live_swap_is_blocked_before_any_pin_verification(self):
        with patch.object(main.hub, 'cards', self.cards), \
             patch.object(sim, 'read_iccid', return_value='8900000000000000002'), \
             patch.object(sim, 'read_card') as full:
            result = main._preflight_pin_locked(self.inst, 1, self.proof)
        self.assertEqual(result['code'], 'card_mismatch')
        full.assert_not_called()

    def test_stale_missing_or_changed_reader_proof_requires_a_full_probe(self):
        variants = [None, True, {**self.proof, 'observed_at': time.monotonic() - 31},
                    {**self.proof, 'readers': [{'name': 'other', 'index': 1}]},
                    {**self.proof, 'pin_enabled': True}]
        probe = SimpleNamespace(present=True, iccid=self.identity, pin_enabled=False)
        for proof in variants:
            with self.subTest(proof=proof), patch.object(main.hub, 'cards', self.cards), \
                 patch.object(sim, 'read_iccid') as identity, \
                 patch.object(sim, 'read_card', return_value=probe) as full:
                self.assertTrue(main._preflight_pin_locked(self.inst, 1, proof)['ok'])
                identity.assert_not_called()
                full.assert_called_once_with(1)

    def test_unreadable_identity_does_not_authorize_cached_pin_state(self):
        probe = SimpleNamespace(present=True, iccid=self.identity, pin_enabled=None, error='read failed')
        with patch.object(main.hub, 'cards', self.cards), \
             patch.object(sim, 'read_iccid', return_value=None), \
             patch.object(sim, 'read_card', return_value=probe) as full:
            result = main._preflight_pin_locked(self.inst, 1, self.proof)
        self.assertEqual(result['code'], 'card_unreadable')
        full.assert_called_once_with(1)

    def test_full_probe_checks_identity_before_using_the_supplied_pin(self):
        probe = SimpleNamespace(present=True, iccid='8900000000000000002', pin_enabled=True)
        with patch.object(sim, 'read_card', return_value=probe) as full:
            result = main._preflight_pin_locked(self.inst, 1)
        self.assertEqual(result['code'], 'card_mismatch')
        full.assert_called_once_with(1)

    def test_arbitrary_preflight_details_never_enter_public_errors(self):
        for code in ('no_card', 'card_unreadable', 'card_mismatch'):
            error = main._pin_preflight_http({'code': code, 'line_iccid': self.identity,
                                             'error': self.identity + ' private diagnostic'})
            self.assertNotIn(self.identity, json.dumps(error.detail))
            self.assertNotIn('private diagnostic', json.dumps(error.detail))


class LateMessageTransactionTests(TempStore):
    def partial(self):
        store.add_sms_segment('2', '888', 7, 2, 1, 'first ', ts=1000)
        return store.take_stale_sms_segments(now=1200, publish=True)[0]['message']

    def test_failed_publication_preserves_buffer_and_has_no_half_created_history(self):
        store.add_sms_segment('2', '888', 7, 2, 1, 'first ', ts=1000)
        with store._conn() as c:
            c.execute("CREATE TRIGGER fail_late BEFORE INSERT ON sms_late_groups "
                      "BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.take_stale_sms_segments(now=1200, publish=True)
        with store._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self._buffered(), 1)

    def test_body_and_late_state_roll_back_together_and_retry_completes_once(self):
        rec = self.partial()
        with store._conn() as c:
            c.execute("CREATE TRIGGER fail_body BEFORE UPDATE OF body ON messages "
                      "BEGIN SELECT RAISE(ABORT, 'injected body failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.merge_late_sms_segment('2', '888', 7, 2, 2, 'second', now=1300)
        with store._conn() as c:
            self.assertEqual(json.loads(c.execute('SELECT parts FROM sms_late_groups').fetchone()[0]),
                             {'1': 'first '})
            self.assertEqual(c.execute('SELECT body FROM messages').fetchone()[0], rec['body'])
            c.execute('DROP TRIGGER fail_body')
        result = store.merge_late_sms_segment('2', '888', 7, 2, 2, 'second', now=1301)
        self.assertEqual(result['message']['body'], 'first second')
        self.assertEqual(result['message']['id'], rec['id'])
        self.assertTrue(store.merge_late_sms_segment('2', '888', 7, 2, 2, 'second', now=1302)['duplicate'])

    def test_deleting_original_message_removes_its_late_group(self):
        rec = self.partial()
        store.delete_messages('2', [rec['id']])
        with store._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sms_late_groups').fetchone()[0], 0)
        self.assertIsNone(store.merge_late_sms_segment('2', '888', 7, 2, 2, 'second', now=1300))

    def test_late_event_updates_original_and_reconciles_without_another_notification(self):
        self.partial()
        payload = {'instance': '2', 'event': 'sms_in',
                   'args': ['888', base64.b64encode(b'second').decode(), '7', '2', '2']}
        broadcast = AsyncMock()
        with patch.object(store.time, 'time', return_value=1300), \
             patch.object(main.hub, 'broadcast', broadcast), \
             patch.object(main, '_harvest_allowance_reply') as harvest, \
             patch.object(main, '_dispatch_push') as notify:
            asyncio.run(main.api_engine_event(payload))
        event = broadcast.call_args.args[0]
        self.assertTrue(event['updated'])
        self.assertEqual(event['message']['body'], 'first second')
        harvest.assert_called_once_with('2', '888')
        notify.assert_not_called()


class EngineModuleContractTests(unittest.TestCase):
    def run_contract(self, names):
        from pathlib import Path
        import subprocess
        import sys
        root = Path(__file__).resolve().parent.parent
        return subprocess.run([sys.executable, str(root / 'tools/engine-modules.py'),
                               str(root / 'engine/asterisk-keep-modules.txt'), '--actual-stdin'],
                              input='\n'.join(names) + '\n', capture_output=True, text=True)

    def modules(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return [line.split('#', 1)[0].strip() for line in
                (root / 'engine/asterisk-keep-modules.txt').read_text().splitlines()
                if line.split('#', 1)[0].strip()]

    def test_exact_set_is_order_independent_and_contains_amd64_opus(self):
        names = self.modules()
        self.assertEqual(len(names), 128)
        for name in ('codec_opus.so', 'format_ogg_opus.so', 'res_format_attr_opus.so'):
            self.assertIn(name, names)
        ordered, reversed_set = self.run_contract(names), self.run_contract(list(reversed(names)))
        self.assertEqual(ordered.returncode, 0, ordered.stderr)
        self.assertEqual(ordered.stdout, reversed_set.stdout)
        self.assertEqual(json.loads(ordered.stdout)['count'], 128)

    def test_same_count_with_the_wrong_module_is_rejected(self):
        names = self.modules()
        names[names.index('codec_opus.so')] = 'unapproved_module.so'
        result = self.run_contract(names)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing: codec_opus.so', result.stderr)
        self.assertIn('extra: unapproved_module.so', result.stderr)

    def test_duplicates_cannot_mask_a_missing_module(self):
        names = self.modules()
        names[0] = names[1]
        result = self.run_contract(names)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('duplicate', result.stderr)

    def test_keep_list_change_invalidates_only_the_base_fingerprint(self):
        from pathlib import Path
        import shutil
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'tools').mkdir(); (repo / 'engine').mkdir()
            shutil.copy2(root / 'tools/engine-fingerprint.sh', repo / 'tools')
            (repo / 'engine/Dockerfile').write_text('FROM scratch\n')
            keep = repo / 'engine/asterisk-keep-modules.txt'
            keep.write_text('codec_opus.so\n')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'add', 'engine', 'tools'], check=True)
            def fingerprint(kind):
                return subprocess.check_output(['sh', str(repo / 'tools/engine-fingerprint.sh'), kind])
            base, runtime = fingerprint('base'), fingerprint('runtime')
            keep.write_text('codec_opus.so\nformat_ogg_opus.so\n')
            self.assertNotEqual(base, fingerprint('base'))
            self.assertEqual(runtime, fingerprint('runtime'))


class FeishuIsolationTests(unittest.TestCase):
    def test_one_retrying_bot_does_not_delay_another_delivery(self):
        import threading
        from control.app import notify_push
        first_started, second_delivered, release_first = (threading.Event() for _ in range(3))
        settings = {'feishu': {'channels': [
            {'id': 'slow', 'enabled': True, 'instances': [], 'events': {'host_alert': True}},
            {'id': 'fast', 'enabled': True, 'instances': [], 'events': {'host_alert': True}},
        ]}}
        def deliver(channel, sender, config, payload):
            if channel == 'feishu:slow':
                first_started.set()
                release_first.wait(2)
            else:
                second_delivered.set()
        with patch.object(notify_push, '_deliver_with_retry', side_effect=deliver):
            worker = threading.Thread(target=notify_push.dispatch,
                                      args=(settings, notify_push.EV_HOST_ALERT, {}, '', 'fixture'))
            worker.start()
            try:
                self.assertTrue(first_started.wait(1))
                self.assertTrue(second_delivered.wait(1), 'retry on the first bot blocked another bot')
            finally:
                release_first.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
