"""Safe card diagnostics, from lpac envelopes through the API and persisted view."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import capability_operations, lpa, main


class DiagnosticTests(unittest.TestCase):
    def test_lpacs_generic_failure_code_does_not_replace_the_card_result(self):
        for step, reason, expected in [
            ('es10c_enable_profile', 'disallowed by policy', 3),
            ('es10c_delete_profile', 'profile not in disabled state', 2),
            ('es10c_disable_profile', 'profile not in enabled state', 2),
            ('es10c_enable_profile', 'iccid or aid not found', 1),
            ('es10c_enable_profile', 'wrong profile reenabling', 4),
        ]:
            with self.subTest(step=step, reason=reason):
                error = lpa.LpaError(step, detail=reason, stage='profile enable')
                data = error.public_detail()
                self.assertEqual(data['diagnostic']['lpac_code'], -1)
                self.assertEqual(data['diagnostic']['card_result'], expected)
                self.assertEqual(data['diagnostic']['step'], step)
                self.assertNotIn('could not be confirmed', data['message'])

    def test_internal_or_unknown_error_does_not_invent_a_card_refusal(self):
        for detail in ['internal error, maybe illegal iccid/aid coding', 'unknown',
                       {'reason': 'disallowed by policy', 'token': 'private-token'}]:
            data = lpa.LpaError('es10c_enable_profile', detail=detail).public_detail()
            self.assertNotIn('card_result', data['diagnostic'])
            self.assertNotIn('private-token', json.dumps(data))

    def test_raw_payload_and_unrecognised_step_are_never_published(self):
        private = 'https://private.example.invalid/card/private-id?token=private-token'
        error = lpa.LpaError(private, detail={'identity': 'private-card', 'password': private},
                             stage=private, category=private, code=123456789)
        data = json.dumps(error.public_detail())
        self.assertNotIn('private', data)
        self.assertNotIn('123456789', data)
        self.assertEqual(error.diagnostic()['step'], 'unknown')

    def test_only_explicit_short_pcsc_and_status_words_are_exported(self):
        error = lpa.LpaError('euicc_init', detail=(
            'SCardConnect() failed: 0xFFFFFFFF8010000B; private-card; SW=6985; '
            'https://private.example.invalid'), stage='profile delete')
        data = error.public_detail()
        self.assertEqual(data['diagnostic']['pcsc_code'], '8010000B')
        self.assertEqual(data['diagnostic']['status_word'], '6985')
        self.assertNotIn('private', json.dumps(data))
        self.assertNotIn('status_word', lpa.LpaError('error', detail='APDU=00126985').diagnostic())

    def test_final_envelope_does_not_hide_transport_error_from_stderr(self):
        error = lpa.LpaError('es10c_enable_profile',
                             detail='internal error, maybe illegal iccid/aid coding',
                             transport_detail='private-reader SCardTransmit failed: SCARD_W_RESET_CARD')
        self.assertEqual(error.diagnostic()['pcsc_code'], '80100068')
        self.assertNotIn('card_result', error.diagnostic())
        self.assertNotIn('private-reader', json.dumps(error.public_detail()))

    def test_scoped_native_diagnostics_ignore_init_cleanup_and_private_payloads(self):
        stderr = ("SW=6F00\nMDD_LPAC_DIAG begin=profile\n"
                  "MDD_LPAC_DIAG stage=response_status sw=6985\n"
                  "MDD_LPAC_DIAG stage=command_exchange\n"
                  "private-card APDU=0123456789\nMDD_LPAC_DIAG end=profile\n"
                  "SCardTransmit() failed: 8010000B (cleanup)\n")
        error = lpa.LpaError('es10c_enable_profile', stage='profile enable', transport_detail=stderr)
        data = error.diagnostic()
        self.assertEqual(data['failure_stage'], 'response_status')
        self.assertEqual(data['status_word'], '6985')
        self.assertNotIn('pcsc_code', data)
        self.assertEqual(data['category'], 'unknown_error')
        self.assertNotIn('busy', error.user_message())
        self.assertNotIn('card_result', data)
        self.assertNotIn('private', json.dumps(error.public_detail()))

    def test_native_parser_uses_closed_tokens_and_bounded_codes(self):
        from control.app.lpa_diagnostics import profile_transport_diagnostic
        for line in ('stage=private-stage', 'stage=response_status sw=698500',
                     'card_result=999', 'card_result=-1', 'stage=response_tag private-token'):
            self.assertEqual(profile_transport_diagnostic(
                'MDD_LPAC_DIAG begin=profile\nMDD_LPAC_DIAG ' + line + '\n'), {})
        self.assertEqual(profile_transport_diagnostic('MDD_LPAC_DIAG stage=response_tag\n'), {})
        self.assertEqual(profile_transport_diagnostic(
            'MDD_LPAC_DIAG begin=profile\nMDD_LPAC_DIAG stage=apdu_transport\n'
            'SCardTransmit() failed: 80100016 (private-reader)\n'),
            {'failure_stage': 'apdu_transport', 'pcsc_code': '80100016'})

    def test_managed_helper_never_falls_back_to_old_shared_binary(self):
        with patch.object(lpa.cfg, 'get_settings', return_value={}), \
                patch.object(lpa.cfg.sys, 'prefix', '/fixture/generation/venv'), \
                patch.object(lpa.cfg.sys, 'base_prefix', '/usr'):
            self.assertEqual(lpa.lpac_bin(), '/fixture/generation/venv/bin/lpac')
        with patch.object(lpa.cfg, 'get_settings', return_value={'esim': {'lpac_bin': '/custom/lpac'}}):
            self.assertEqual(lpa.lpac_bin(), '/custom/lpac')
        with patch.dict(lpa.os.environ, {'LIBEUICC_DEBUG_APDU': '1'}):
            self.assertNotIn('LIBEUICC_DEBUG_APDU', lpa._env_for_reader('fixture-reader'))


class LpacEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_profile_envelope_retains_reason_in_safe_journal(self):
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data((json.dumps({'type': 'lpa', 'payload': {
            'code': -1, 'message': 'es10c_delete_profile',
            'data': 'profile not in disabled state'}}) + '\n').encode())
        stdout.feed_eof()
        stderr.feed_data(b'private-card private-token https://private.example.invalid')
        stderr.feed_eof()
        process = Mock(stdout=stdout, stderr=stderr, returncode=1, wait=AsyncMock(return_value=1))
        with patch.object(lpa, 'lpac_bin', return_value='/fixture/lpac'), \
                patch.object(lpa.os.path, 'isfile', return_value=True), \
                patch.object(lpa.os, 'access', return_value=True), \
                patch.object(lpa.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)), \
                self.assertLogs(lpa.log, level='WARNING') as logs:
            with self.assertRaises(lpa.LpaError) as caught:
                await lpa.profile_delete('fixture-reader', 'fixture-card')
        self.assertEqual(caught.exception.diagnostic()['card_result'], 2)
        self.assertIn('es10c_delete_profile', str(logs.output))
        self.assertIn('profile_not_disabled', str(logs.output))
        self.assertNotIn('private', str(logs.output))


class ProfileResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(main, '_ESIM_CACHE_PATH', str(Path(self.directory) / 'chip.json')))
        for field in ('cards', 'reader_locks', 'esim_switch_locks', 'lpa_busy'):
            self.enterContext(patch.object(main.hub, field, {}))
        self.enterContext(patch.object(main, '_esim_guard_engine'))
        self.enterContext(patch.object(main, '_esim_switch_identity', return_value=('reader', '')))
        self.enterContext(patch.object(main.device_state, 'vpcd_modem_hardware_id', return_value=''))
        self.profiles = [{'id': 'default', 'eid': 'fixture-euicc', 'profiles': [
            {'iccid': 'fixture-card', 'profileState': 'disabled'}]}]
        main._esim_cache_store(self.profiles, '')

    def status(self):
        return main._esim_cache_load()['fixture-euicc']['ses'][0]['profiles'][0]['operation_status']

    async def test_failed_write_survives_reread_and_only_successful_write_clears_it(self):
        read_timestamp = main._esim_cache_load()['fixture-euicc']['ts']
        async def fail():
            raise lpa.LpaError('es10c_enable_profile', detail='disallowed by policy', stage='profile enable')
        with self.assertRaises(main.HTTPException) as caught:
            await main._esim_run('reader', 0, fail(), profile_iccid='fixture-card')
        self.assertEqual(caught.exception.detail['diagnostic']['card_result'], 3)
        self.assertEqual(main._esim_cache_load()['fixture-euicc']['ts'], read_timestamp,
                         'a failed write must not make an old profile list look freshly read')
        main._esim_cache_store(self.profiles, '')
        self.assertEqual(self.status()['state'], 'failed')
        self.assertEqual(self.status()['error'], caught.exception.detail)
        await main._esim_run('reader', 0, AsyncMock(return_value={})(), profile_iccid='fixture-card')
        self.assertEqual(self.status()['state'], 'success')
        self.assertNotIn('error', self.status())

    async def test_uncertain_enable_keeps_its_original_card_diagnostic(self):
        diagnostic = lpa.LpaError('es10c_enable_profile', detail='unknown', transport_detail=(
            'MDD_LPAC_DIAG begin=profile\nMDD_LPAC_DIAG stage=response_status sw=6985\n'
            'MDD_LPAC_DIAG end=profile\n')).diagnostic()
        async def fail():
            raise main.HTTPException(409, {
                'code': 'profile_enable_unconfirmed', 'diagnostic': diagnostic,
                'message': 'The enable result is uncertain. Lines remain stopped until the active SIM is verified.'})
        with self.assertRaises(main.HTTPException):
            await main._esim_run('reader', 0, fail(), profile_iccid='fixture-card')
        self.assertEqual(self.status()['error']['code'], 'profile_enable_unconfirmed')
        self.assertEqual(self.status()['error']['diagnostic'], diagnostic)

    async def test_cache_failure_never_replaces_original_card_error(self):
        async def fail():
            raise lpa.LpaError('es10c_delete_profile', detail='disallowed by policy')
        with patch.object(main, '_esim_cache_update_profile', side_effect=OSError('private-path')):
            with self.assertRaises(main.HTTPException) as caught:
                await main._esim_run('reader', 0, fail(), profile_iccid='fixture-card')
        self.assertEqual(caught.exception.detail['diagnostic']['card_result'], 3)

    async def test_country_exit_failure_is_not_persisted_as_device_unavailable(self):
        store = capability_operations.CapabilityOperations(Path(self.directory) / 'capabilities.json')
        op = store.begin('fixture-reader', {'vowifi_enabled': True})
        with patch.object(main, 'capability_operation_store', store), \
                patch.object(main.hub, 'broadcast', new=AsyncMock()), \
                patch.object(main, '_apply_device_capabilities', new=AsyncMock(side_effect=
                    main.HTTPException(503, {'code': 'egress_unavailable', 'message': 'private-server'}))):
            await main._run_capability_operation(op['operation_id'], 'fixture-reader', {'vowifi_enabled': True})
        result = store.latest('fixture-reader')
        self.assertEqual(result['error_code'], 'egress_unavailable')
        self.assertNotIn('private-server', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
