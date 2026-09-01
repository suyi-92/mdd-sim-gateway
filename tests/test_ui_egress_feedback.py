"""Regression coverage for the two operator-visible setup and proxy failures."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")


def css_rule(selector: str) -> str:
    start = CSS.index(f"{selector} {{")
    return CSS[start:CSS.index("}", start)]


class EgressFeedbackLayoutTests(unittest.TestCase):
    def test_proxy_parse_summary_no_longer_sizes_the_button_column(self):
        start = SOURCE.index('<div className="u-proxy-actions">')
        actions = SOURCE[start:SOURCE.index("</div>", start)]
        self.assertNotIn("u-test-parsed", actions)
        self.assertIn("u-test-parsed", SOURCE[SOURCE.index("</div>", start):])

        actions_rule = css_rule(".u-proxy-actions")
        self.assertIn("min-width:0", actions_rule)
        self.assertNotIn("max-content", actions_rule)
        parsed_rule = css_rule(".u-test-parsed")
        self.assertIn("grid-column:2 / -1", parsed_rule)
        self.assertIn("min-width:0", parsed_rule)
        self.assertIn("overflow-wrap:anywhere", parsed_rule)
        self.assertIn("overflow:hidden", parsed_rule)

    def test_country_udp_test_has_persistent_busy_and_result_state(self):
        self.assertIn("const [exitTests, setExitTests] = useState({})", SOURCE)
        self.assertIn("disabled={exitTest?.busy || saving}", SOURCE)
        self.assertIn("exitTest?.busy ? 'Testing…' : 'Test UDP'", SOURCE)
        self.assertIn("u-exit-test-result", SOURCE)
        self.assertIn("await persistSettings()", SOURCE)

    def test_saved_assignment_is_distinct_from_a_running_node(self):
        self.assertIn("t('Saved · idle')", SOURCE)
        self.assertIn("t('Saved assignment')", SOURCE)
        self.assertIn("t('Runtime node')", SOURCE)
        self.assertIn("const [saveState, setSaveState] = useState('loading')", SOURCE)
        self.assertIn("setS(saved)", SOURCE)

    def test_draft_vowifi_notice_links_to_each_missing_setup_area(self):
        self.assertIn("<DraftProvisioningNotice device={d} setTab={setTab}/>", SOURCE)
        self.assertIn("onClick={() => setTab('sim')}", SOURCE)
        self.assertIn("onClick={() => setTab('hardware')}", SOURCE)
        self.assertIn("device?.provisioning?.state === 'draft'", SOURCE)


if __name__ == "__main__":
    unittest.main()
