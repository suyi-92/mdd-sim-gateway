"""Security and API contracts for optional in-call AI subtitles."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from control.app import config, live_translation

try:
    from control.app import main
except ImportError:  # source-only hosts may not have the Control dependencies installed
    main = None


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


class LiveTranslationConfigTests(unittest.TestCase):
    def test_older_and_malformed_configs_load_with_subtitles_safely_disabled(self):
        with tempfile.TemporaryDirectory() as temp, patch.multiple(
                config, DATA_DIR=temp, CONFIG_PATH=str(Path(temp) / "config.yaml")):
            config.save({"settings": {"timezone": "UTC"}, "instances": {}})
            self.assertEqual(config.get_settings()["live_translation"], {
                "enabled": False, "api_key": ""})
            config.save({"settings": {"live_translation": "malformed"}, "instances": {}})
            self.assertEqual(config.get_settings()["live_translation"], {
                "enabled": False, "api_key": ""})

    def test_public_settings_never_return_the_long_lived_api_key(self):
        public = live_translation.public_config({"enabled": True, "api_key": "fixture-test-key"})
        self.assertEqual(public, {"enabled": True, "api_key": "", "api_key_set": True})
        self.assertNotIn("fixture-test-key", repr(public))

    def test_blank_public_key_preserves_stored_key_and_explicit_clear_removes_it(self):
        previous = {"enabled": True, "api_key": "fixture-existing-key"}
        self.assertEqual(
            live_translation.normalize_config(
                {"enabled": True, "api_key": "", "api_key_set": True}, previous),
            previous,
        )
        self.assertEqual(
            live_translation.normalize_config(
                {"enabled": False, "clear_api_key": True}, previous),
            {"enabled": False, "api_key": ""},
        )

    def test_enabled_subtitles_require_a_valid_key(self):
        with self.assertRaisesRegex(ValueError, "not_configured"):
            live_translation.normalize_config({"enabled": True, "api_key": ""}, {})
        with self.assertRaisesRegex(ValueError, "invalid_api_key"):
            live_translation.normalize_config({"enabled": True, "api_key": "has space"}, {})
        with self.assertRaisesRegex(ValueError, "invalid_settings"):
            live_translation.normalize_config({"enabled": "false"}, {})


class LiveTranslationProviderTests(unittest.TestCase):
    def test_client_secret_request_is_fixed_scope_and_returns_only_ephemeral_fields(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "value": "ek-short-lived-secret",
            "expires_at": 12345,
            "provider_internal": "not-for-the-browser",
        }
        post = Mock(return_value=response)
        result = live_translation.create_client_secret(
            {"enabled": True, "api_key": "fixture-server-key"}, "safe-user-hash", post)

        self.assertEqual(result, {
            "value": "ek-short-lived-secret", "expires_at": 12345,
            "model": "gpt-realtime-translate", "target_language": "zh",
        })
        args, kwargs = post.call_args
        self.assertEqual(args, (live_translation.CLIENT_SECRET_URL,))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture-server-key")
        self.assertEqual(kwargs["headers"]["OpenAI-Safety-Identifier"], "safe-user-hash")
        self.assertEqual(kwargs["json"], {"session": {
            "model": "gpt-realtime-translate", "audio": {"output": {"language": "zh"}},
        }})
        self.assertEqual(kwargs["timeout"], live_translation.REQUEST_TIMEOUT_SECONDS)

    def test_provider_failure_is_closed_schema_and_does_not_echo_response_body(self):
        response = Mock(status_code=500, text="upstream leaked fixture-private-provider-value")
        with self.assertRaises(live_translation.LiveTranslationError) as raised:
            live_translation.create_client_secret(
                {"enabled": True, "api_key": "fixture-server-key"}, "safe", Mock(return_value=response))
        self.assertEqual(raised.exception.code, "live_translation.provider_unavailable")
        self.assertNotIn("private-provider", str(raised.exception))


@unittest.skipUnless(NODE, "Node.js is required for browser live-translation tests")
class BrowserLiveTranslationTests(unittest.TestCase):
    def test_remote_track_sidecar_and_transcript_event_contract(self):
        completed = subprocess.run(
            [NODE, "--test", "webui/tests/liveTranslation.test.js",
             "webui/tests/globalSoftphoneRender.test.js"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)


@unittest.skipIf(main is None, "control plane dependencies are not installed")
class LiveTranslationApiTests(unittest.TestCase):
    def test_settings_get_and_put_keep_api_key_server_side(self):
        stored = {"timezone": "UTC", "live_translation": {
            "enabled": True, "api_key": "fixture-stored-server-key"}}
        with patch.object(main.cfg, "get_settings", return_value=stored):
            public = main.api_get_settings()
        self.assertTrue(public["live_translation"]["api_key_set"])
        self.assertNotIn("fixture-stored-server-key", repr(public))

        with patch.object(main.cfg, "get_settings", return_value=stored), \
                patch.object(main.cfg, "update_settings", return_value=stored) as update, \
                patch.object(main.egress, "publish"):
            saved = main.api_put_settings({"live_translation": {
                "enabled": True, "api_key": "", "api_key_set": True}})
        self.assertEqual(update.call_args.args[0]["live_translation"], stored["live_translation"])
        self.assertNotIn("fixture-stored-server-key", repr(saved))

    def test_session_endpoint_maps_only_safe_provider_error_code(self):
        error = live_translation.LiveTranslationError("live_translation.api_key_rejected", 502)
        with patch.object(main.cfg, "get_settings", return_value={
                    "live_translation": {"enabled": True, "api_key": "fixture-api-key"}}), \
                patch.object(main.cfg, "internal_event_token", return_value="internal-secret"), \
                patch.object(main.live_translation, "create_client_secret", side_effect=error):
            with self.assertRaises(Exception) as raised:
                asyncio.run(main.api_live_translation_session())
        self.assertEqual(getattr(raised.exception, "status_code", None), 502)
        self.assertEqual(getattr(raised.exception, "detail", None), "live_translation.api_key_rejected")
        self.assertNotIn("internal-secret", repr(raised.exception))

    def test_session_endpoint_returns_no_store_ephemeral_response_and_hashes_identity(self):
        issued = {"value": "ek-short-lived", "model": "gpt-realtime-translate",
                  "target_language": "zh"}
        create = Mock(return_value=issued)
        with patch.object(main.cfg, "get_settings", return_value={
                    "live_translation": {"enabled": True, "api_key": "fixture-api-key"}}), \
                patch.object(main.cfg, "internal_event_token", return_value="internal-secret"), \
                patch.object(main.live_translation, "create_client_secret", create):
            response = asyncio.run(main.api_live_translation_session())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(json.loads(response.body), issued)
        safety_identifier = create.call_args.args[1]
        self.assertRegex(safety_identifier, r"^[0-9a-f]{64}$")
        self.assertNotIn("internal-secret", safety_identifier)


if __name__ == "__main__":
    unittest.main()
