import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app.ami import AmiClient
from control.app import status


class RegistrationStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inst = {"id": "1", "enabled": True, "mcc": "310", "mnc": "240"}
        self.base = patch.multiple(
            status.engine,
            is_running=lambda _iid: True,
            read_run_json=lambda _iid, _name: {"state": "PIN_DISABLED"},
            tunnel_installed=lambda _iid: True,
            read_pcscf=lambda _iid: "present",
        )

    async def test_ami_registered_avoids_docker_cli(self):
        ami = SimpleNamespace(registration_state=AsyncMock(return_value="Registered"))
        with self.base, patch.object(status, "resolve_epdg", return_value=True), \
                patch.object(status.engine, "registration_state", return_value="unknown") as cli:
            result = await status.compute(self.inst, ami)
        self.assertEqual(result["state"], "OK")
        ami.registration_state.assert_awaited_once()
        cli.assert_not_called()

    async def test_cli_remains_fallback_while_ami_is_starting(self):
        ami = SimpleNamespace(registration_state=AsyncMock(return_value="unknown"))
        with self.base, patch.object(status, "resolve_epdg", return_value=True), \
                patch.object(status.engine, "registration_state", return_value="Registered"):
            result = await status.compute(self.inst, ami)
        self.assertEqual(result["state"], "OK")
        ami.registration_state.assert_awaited_once()

    async def test_silence_and_refusal_get_different_labels(self):
        # Asterisk says "Rejected" for both, but "no answer" (a stale ESP session the
        # carrier aged out) and "refused" (a real SIP 4xx) need different fixes.
        silence = "WARNING: No response received from 'sip:x' on REGISTER attempt"
        refusal = "WARNING: Fatal response '403' received from 'sip:x' on register attempt"
        for tail, expected in ((silence, "reg_unanswered"), (refusal, "reg_rejected")):
            with self.subTest(expected=expected), self.base, \
                    patch.object(status.engine, "logs", lambda _iid, _tail=200, t=tail: t), \
                    patch.object(status.engine, "registration_state", return_value="Rejected"):
                result = await status.compute(self.inst, None)
            self.assertEqual(result["reason_code"], expected)

    async def test_explicit_sip_rejection_keeps_the_server_status_code(self):
        refusal = "WARNING: Fatal response '403' received on registration attempt"
        with self.base, patch.object(status.engine, "logs", return_value=refusal), \
                patch.object(status.engine, "registration_state", return_value="Rejected"):
            result = await status.compute(self.inst, None)
        self.assertEqual(result["reason_code"], "reg_rejected")
        self.assertEqual(result["detail"]["sip_status"], 403)

    async def test_real_registration_attempt_wording_is_unanswered(self):
        real = ("schedule_retry: No response received from 'sip:x' on registration "
                "attempt to 'sip:y', retrying in '30'")
        self.assertTrue(status.registration_unanswered(real))

    async def test_log_read_error_is_not_fast_recovery_evidence(self):
        self.assertFalse(status.registration_unanswered("error: Docker API timed out"))

    async def test_unanswered_registration_records_active_channel_count(self):
        ami = SimpleNamespace(
            registration_state=AsyncMock(return_value="Rejected"),
            active_channel_count=AsyncMock(return_value=0),
        )
        real = "No response received from 'sip:x' on registration attempt"
        with self.base, patch.object(status.engine, "logs", return_value=real):
            result = await status.compute(self.inst, ami)
        self.assertEqual(result["reason_code"], "reg_unanswered")
        self.assertEqual(result["detail"]["active_channels"], 0)
        ami.active_channel_count.assert_awaited_once()

    async def test_the_newest_marker_wins_when_the_log_holds_both(self):
        log = ("WARNING: Fatal response '403' received from 'sip:x' on register attempt\n"
               "WARNING: No response received from 'sip:x' on REGISTER attempt")
        self.assertTrue(status.registration_unanswered(log))
        self.assertFalse(status.registration_unanswered("\n".join(log.splitlines()[::-1])))

    async def test_machine_readable_child_rekey_timeout_beats_generic_log_text(self):
        with patch.multiple(
                status.engine,
                charon_log=lambda _iid, _tail=400: "timeout",
                usim_status=lambda _iid: {},
                read_run_json=lambda _iid, _name: {
                    "state": "DOWN", "reason_code": "rekey_timeout"}):
            code, reason = status.classify_ike("1")
        self.assertEqual(code, "tunnel_child_rekey_timeout")
        self.assertIn("CHILD_SA", reason)

    async def test_missing_eap_challenge_has_an_actionable_tunnel_reason(self):
        with patch.multiple(
                status.engine,
                charon_log=lambda _iid, _tail=400: "received IKE_AUTH (1)",
                usim_status=lambda _iid: {},
                read_run_json=lambda _iid, _name: {
                    "state": "DOWN", "reason_code": "no_eap_challenge"}):
            code, reason = status.classify_ike("1")
        self.assertEqual(code, "tunnel_no_eap")
        self.assertIn("supported EAP", reason)

    async def test_a_resolver_blip_does_not_mark_an_established_tunnel_down(self):
        # An established tunnel talks to an address, not a name. A DNS outage must surface
        # only on lines that actually need a lookup — those still to build their tunnel.
        with self.base, patch.object(status, "resolve_epdg", return_value=False) as resolver, \
                patch.object(status.engine, "registration_state", return_value="Registered"):
            result = await status.compute(self.inst, None)
        self.assertEqual(result["state"], "OK")
        resolver.assert_not_called()

    async def test_dns_failure_still_surfaces_while_the_tunnel_is_down(self):
        with self.base, patch.object(status.engine, "tunnel_installed", lambda _iid: False), \
                patch.object(status, "resolve_epdg", return_value=False):
            result = await status.compute(self.inst, None)
        self.assertEqual(result["state"], "EPDG_UNRESOLVED")

    async def test_missing_pin_observation_during_rebuild_is_not_no_card(self):
        with patch.multiple(
                status.engine,
                is_running=lambda _iid: True,
                read_run_json=lambda _iid, _name: None):
            result = await status.compute(self.inst, None)

        self.assertEqual(result["state"], "REGISTERING")
        self.assertEqual(result["reason_code"], "registering")
        self.assertEqual(result["detail"]["registration"], "unknown")

    async def test_explicit_no_card_observation_remains_no_card(self):
        with patch.multiple(
                status.engine,
                is_running=lambda _iid: True,
                read_run_json=lambda _iid, _name: {"state": "NO_CARD"}):
            result = await status.compute(self.inst, None)

        self.assertEqual(result["state"], "NO_CARD")
        self.assertEqual(result["reason_code"], "no_card")


class AmiRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_uses_bounded_command_without_detailed_action(self):
        client = AmiClient("1", "172.17.0.2", 5038, "user", "secret", "realm")
        client._mgr = object()
        client._connected = True
        client._action = AsyncMock(return_value=[{"Output": "volte_ims Registered"}])

        self.assertEqual(await client.registration_state(), "Registered")
        client._action.assert_awaited_once_with(
            {"Action": "Command", "Command": "pjsip show registrations"}, timeout=3.0)

    async def test_active_channels_uses_bounded_ami_command(self):
        client = AmiClient("1", "172.17.0.2", 5038, "user", "secret", "realm")
        client._mgr = object()
        client._connected = True
        client._action = AsyncMock(return_value=[{"Output": "2 active channels\n1 active call"}])

        self.assertEqual(await client.active_channel_count(), 2)
        client._action.assert_awaited_once_with(
            {"Action": "Command", "Command": "core show channels count"}, timeout=3.0)

    async def test_unreadable_active_channel_count_fails_closed(self):
        client = AmiClient("1", "172.17.0.2", 5038, "user", "secret", "realm")
        client._mgr = object()
        client._connected = True
        client._action = AsyncMock(return_value=[{"Output": "unexpected"}])

        self.assertIsNone(await client.active_channel_count())


if __name__ == "__main__":
    unittest.main()
