"""Regression coverage for viewport-sized, touch-scrollable product navigation."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")
APP = (ROOT / "webui/src/App.jsx").read_text(encoding="utf-8")


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
        self.assertIn("margin:0 0 20px", rule)
        self.assertIn("line-height:1.65", rule)

    def test_shared_text_and_form_spacing_is_not_compacted_by_settings(self):
        self.assertIn("line-height: 1.5", css_rule("body"))
        self.assertIn(
            ".u-panel > h2:not(:first-child),.u-panel > h3:not(:first-child) "
            "{ margin-top:26px",
            CSS,
        )
        self.assertIn(".u-form-grid { column-gap:14px; row-gap:18px; }", CSS)
        self.assertIn(
            ".u-panel > label:has(>.u-toggle) { min-height:34px; margin:10px 0",
            CSS,
        )


if __name__ == "__main__":
    unittest.main()
