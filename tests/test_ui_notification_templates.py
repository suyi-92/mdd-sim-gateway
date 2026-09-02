"""Regression coverage for every notification-channel template editor."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")


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

    def test_telegram_offers_library_and_country_routes(self):
        self.assertIn("tg.proxy_mode === 'library'", SOURCE)
        self.assertIn("tg.proxy_mode === 'country'", SOURCE)
        self.assertNotIn("s.updates", SOURCE)

    def test_subscription_profiles_are_filtered_from_generic_proxy_pickers(self):
        self.assertIn("profile?.type !== 'subscription'", SOURCE)
        self.assertEqual(SOURCE.count("selectableProxyProfiles(s).map"), 1)

    def test_webui_routes_updates_to_mddctl_only(self):
        self.assertIn("sudo mddctl update", SOURCE)
        self.assertNotIn("checkUpdate", SOURCE)
        self.assertNotIn("openUpdateDialog", SOURCE)

    def test_notification_and_system_saves_use_normal_sized_action_rows(self):
        self.assertEqual(SOURCE.count('className="u-settings-actions"'), 2)
        self.assertIn(".u-settings-actions .btn { flex:none; width:auto; min-width:88px; }", CSS)
        self.assertNotIn(".u-settings-actions .btn { width:100%; }", CSS)


if __name__ == "__main__":
    unittest.main()
