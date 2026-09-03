"""Regression coverage for viewport-sized, touch-scrollable product navigation."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")
APP = (ROOT / "webui/src/App.jsx").read_text(encoding="utf-8")
UNIFIED = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
ALLOWANCE = (ROOT / "webui/src/views/AllowancePanel.jsx").read_text(encoding="utf-8")
ESIM = (ROOT / "webui/src/views/Esim.jsx").read_text(encoding="utf-8")


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
        rule = css_rule(".u-compliance-note")
        self.assertIn("margin:0 0 26px", rule)
        self.assertIn("line-height:1.7", rule)

    def test_settings_use_explicit_equal_rhythm_sections_and_aligned_fields(self):
        self.assertIn("line-height: 1.5", css_rule("body"))
        self.assertIn(".u-settings-form { display:grid; gap:0; }", CSS)
        self.assertIn(".u-settings-section { min-width:0; display:grid; gap:16px; }", CSS)
        self.assertIn(".u-settings-section+.u-settings-section { margin-top:20px; padding-top:20px", CSS)
        self.assertIn(".u-form-grid>div>label { min-height:18px; margin:0; }", CSS)
        self.assertIn(".u-form-grid input,.u-form-grid select { min-height:38px; }", CSS)
        self.assertIn('className="u-settings-option"', UNIFIED)

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
