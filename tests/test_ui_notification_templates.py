"""Regression coverage for every notification-channel template editor."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class NotificationTemplateUiTests(unittest.TestCase):
    def test_every_outbound_channel_has_an_event_template_editor(self):
        for channel in ("Webhook", "Telegram", "PushPlus", "Feishu / Lark"):
            self.assertIn(f'<MessageTemplateEditor channel="{channel}"', SOURCE)

    def test_editor_exposes_only_the_backend_template_fields(self):
        expected = "{{title}} {{content}} {{event}} {{sim_name}} {{msisdn}} {{from}} {{text}} {{instance}} {{iccid}}"
        self.assertIn(expected, SOURCE)

    def test_each_channel_can_test_the_selected_event(self):
        for method in ("testWebhook", "testTelegram", "testPushPlus", "testFeishu"):
            self.assertIn(f"api.{method}({{", SOURCE)
        self.assertGreaterEqual(SOURCE.count("_test_event: event"), 4)

    def test_channel_test_buttons_show_and_lock_the_pending_state(self):
        self.assertIn("const [channelTesting, setChannelTesting] = useState('')", SOURCE)
        self.assertIn("disabled={!!channelTesting}", SOURCE)
        self.assertIn("channelTesting === key ? 'Testing…' : 'Test'", SOURCE)
        self.assertEqual(SOURCE.count("{testButton("), 4)

    def test_event_forwarding_options_are_collapsed_like_template_editors(self):
        self.assertIn('<details className="u-event-options"><summary>', SOURCE)

    def test_telegram_and_updates_offer_library_and_country_routes(self):
        self.assertIn("tg.proxy_mode === 'library'", SOURCE)
        self.assertIn("s.updates?.proxy_mode === 'country'", SOURCE)

    def test_subscription_profiles_are_filtered_from_generic_proxy_pickers(self):
        self.assertIn("profile?.type !== 'subscription'", SOURCE)
        self.assertEqual(SOURCE.count("selectableProxyProfiles(s).map"), 2)

    def test_network_and_version_notes_follow_their_own_controls(self):
        network_note = SOURCE.index("SOCKS5 entries connect directly")
        update_method = SOURCE.index("<div><label>{t('Update method')}")
        version_note = SOURCE.index("The All versions option follows")
        self.assertLess(network_note, update_method)
        self.assertLess(update_method, version_note)


if __name__ == "__main__":
    unittest.main()
