"""Regression coverage for viewport-sized, touch-scrollable product navigation."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")
APP = (ROOT / "webui/src/App.jsx").read_text(encoding="utf-8")
UNIFIED = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
ALLOWANCE = (ROOT / "webui/src/views/AllowancePanel.jsx").read_text(encoding="utf-8")
ESIM = (ROOT / "webui/src/views/Esim.jsx").read_text(encoding="utf-8")
KEEPALIVE = (ROOT / "webui/src/views/Keepalive.jsx").read_text(encoding="utf-8")
LOGS = (ROOT / "webui/src/views/Logs.jsx").read_text(encoding="utf-8")
MESSAGES = (ROOT / "webui/src/views/Messages.jsx").read_text(encoding="utf-8")
SIM_CONFIG = (ROOT / "webui/src/views/SimConfig.jsx").read_text(encoding="utf-8")
SIM_SELECTOR = (ROOT / "webui/src/views/SimSelector.jsx").read_text(encoding="utf-8")
SOFTPHONE = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")


def css_rule(selector: str) -> str:
    start = CSS.index(f"{selector} {{")
    return CSS[start:CSS.index("}", start)]


class SidebarLayoutTests(unittest.TestCase):
    def test_shell_tracks_safaris_dynamic_viewport(self):
        self.assertRegex(CSS, r"\.u-shell\s*\{[^}]*height:\s*100dvh")

    def test_sidebar_is_touch_scrollable_and_safe_area_aware(self):
        start = CSS.index(".u-sidebar {")
        sidebar = CSS[start:CSS.index("}\n", start)]
        self.assertIn("min-height:0", sidebar)
        self.assertIn("overflow-y:auto", sidebar)
        self.assertIn("touch-action:pan-y", sidebar)
        self.assertIn("-webkit-overflow-scrolling:touch", sidebar)
        self.assertIn("env(safe-area-inset-bottom)", sidebar)

    def test_overlay_sidebar_uses_dynamic_viewport_height(self):
        media = CSS.index("@media(max-width:900px)")
        sidebar = CSS.index(".u-sidebar {", media)
        rule = CSS[sidebar:CSS.index("}", sidebar)]
        self.assertIn("height:100dvh", rule)


class PageRhythmTests(unittest.TestCase):
    def test_compliance_notice_has_its_own_spacing_before_every_page(self):
        self.assertIn('className="u-note u-compliance-note" role="note"', APP)
        rule = css_rule(".u-content>.u-compliance-note")
        self.assertIn("margin:0 0 26px", rule)
        self.assertIn("line-height:1.7", rule)
        self.assertGreater(CSS.count(".u-content>.u-compliance-note"), 0)

    def test_settings_use_explicit_equal_rhythm_sections_and_aligned_fields(self):
        self.assertIn("line-height: 1.5", css_rule("body"))
        self.assertIn(".u-settings-form { display:grid; gap:0; }", CSS)
        self.assertIn(".u-settings-section { min-width:0; display:grid; gap:16px; }", CSS)
        self.assertIn(".u-settings-section+.u-settings-section { margin-top:20px; padding-top:20px", CSS)
        self.assertIn(".u-form-grid>div>label { min-height:18px; margin:0; }", CSS)
        self.assertIn(".u-form-grid input,.u-form-grid select,.u-form-grid textarea { min-height:38px; }", CSS)
        self.assertIn('className="u-settings-option"', UNIFIED)

    def test_non_settings_pages_share_explicit_field_and_note_rhythm(self):
        self.assertIn(".u-field-stack { min-width:0; display:grid; align-content:start; gap:6px; }", CSS)
        self.assertIn(".u-note-stack { display:grid; gap:10px; margin-top:14px; }", CSS)
        self.assertIn('className="u-note-stack"', UNIFIED)
        self.assertIn("u-form-card", UNIFIED)
        self.assertIn('className="u-sim-layout"', SIM_CONFIG)
        self.assertIn('className="u-field-stack"', SIM_CONFIG)
        self.assertIn('className="u-field-stack u-keepalive-threshold"', KEEPALIVE)
        self.assertIn('className="u-form-grid u-allowance-edit-grid"', ALLOWANCE)
        self.assertIn("u-esim-page", ESIM)

    def test_shared_line_and_transport_selectors_keep_text_and_controls_aligned(self):
        self.assertIn('className="card u-line-selector"', SIM_SELECTOR)
        self.assertIn('className="u-inline-field u-call-route-field"', SOFTPHONE)
        self.assertIn('className="u-inline-field u-message-route-field"', MESSAGES)
        self.assertIn('className="u-inline-field u-esim-reader-field"', ESIM)
        self.assertIn("grid-template-columns:max-content minmax(0,1fr)", css_rule(".u-inline-field"))
        self.assertIn("minmax(0,680px)", css_rule(".u-line-selector"))

    def test_timezone_is_a_single_choice_of_common_regions_and_preserves_custom_values(self):
        self.assertIn("const COMMON_TIMEZONES = [", UNIFIED)
        self.assertIn('<select id="system-timezone"', UNIFIED)
        self.assertNotIn('<input list="timezones"', UNIFIED)
        self.assertIn("!timezoneIsCommon && <option value={selectedTimezone}>{selectedTimezone}</option>", UNIFIED)
        for timezone in ("Asia/Shanghai", "Europe/London", "America/New_York",
                         "America/Los_Angeles", "Asia/Tokyo", "UTC"):
            self.assertIn(f"['{timezone}',", UNIFIED)

    def test_new_cellular_modems_default_to_flight_mode_once(self):
        self.assertIn("Enable flight mode for newly detected modems", UNIFIED)
        self.assertIn("checked={s.device_defaults?.flight_mode !== false}", UNIFIED)
        self.assertIn("changing these defaults never rewrites known devices", UNIFIED)

    def test_diagnostics_separate_the_action_from_capability_rows(self):
        self.assertIn('className="u-page u-diagnostics-page"', UNIFIED)
        self.assertIn('className="card u-panel u-diagnostic-card"', UNIFIED)
        self.assertIn('className="btn btn-ghost u-diagnostic-action"', UNIFIED)
        self.assertIn("margin-top:14px", css_rule(".u-diagnostic-action"))

    def test_sim_forms_follow_their_container_at_medium_and_narrow_widths(self):
        self.assertIn("container:sim-config / inline-size", CSS)
        self.assertIn("container:sim-card / inline-size", CSS)
        self.assertIn("@container sim-config (max-width:720px)", CSS)
        self.assertIn("@container sim-card (max-width:440px)", CSS)

    def test_calls_messages_keepalive_and_logs_have_narrow_layout_contracts(self):
        self.assertIn('className="u-call-layout"', SOFTPHONE)
        self.assertIn('className="u-messages-layout"', MESSAGES)
        self.assertIn('className="u-keepalive-grid"', KEEPALIVE)
        self.assertIn('className="u-log-toolbar"', LOGS)
        self.assertIn("@media(max-width:760px)", CSS)
        self.assertIn(".u-call-layout,.u-messages-layout { flex:none; grid-template-columns:1fr", CSS)
        self.assertIn(".u-keepalive-grid { min-width:980px; }", CSS)

    def test_communication_pages_fill_only_the_remaining_desktop_viewport(self):
        self.assertIn("const communicationView = view === 'calls' || view === 'messages'", APP)
        self.assertIn("communicationView ? ' u-content-communication' : ''", APP)
        content = css_rule(".u-content.u-content-communication")
        self.assertIn("display:flex", content)
        self.assertIn("flex-direction:column", content)
        self.assertIn("overflow:hidden", content)
        page = css_rule(".u-content-communication>.u-communication-page")
        self.assertIn("min-height:0", page)
        self.assertIn("height:auto", page)
        self.assertIn("flex:1", page)
        panels = css_rule(
            ".u-phone-panel,.u-history-panel,.u-message-thread-list,.u-message-conversation")
        self.assertIn("min-height:0", panels)
        self.assertNotIn("minHeight: 520", SOFTPHONE)

    def test_communication_pages_restore_document_scrolling_on_narrow_screens(self):
        narrow = CSS[CSS.index("@media(max-width:760px)"):]
        self.assertIn(
            ".u-content.u-content-communication { display:block; overflow:auto; }", narrow)
        self.assertIn(
            ".u-content-communication>.u-communication-page,.u-communication-page { height:auto; }",
            narrow)

    def test_other_multi_button_async_controls_reserve_their_width(self):
        for class_name, source in (
                ("u-refresh-action", UNIFIED),
                ("u-edit-action", ALLOWANCE),
                ("u-query-action", ALLOWANCE),
                ("u-query-settings-action", ALLOWANCE),
                ("u-load-action", ESIM),
                ("u-update-action", ESIM),
                ("u-download-action", ESIM),
                ("u-upload-action", ESIM),
                ("u-stop-action", ESIM)):
            self.assertIn(class_name, source)
            self.assertIn(f".{class_name}", CSS)


if __name__ == "__main__":
    unittest.main()
