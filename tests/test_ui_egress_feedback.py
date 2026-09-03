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
    def test_proxy_diagnostics_stay_in_the_record_instead_of_adding_a_row(self):
        start = SOURCE.index('<div className="u-proxy-actions">')
        actions = SOURCE[start:SOURCE.index("</div>", start)]
        self.assertNotIn("u-proxy-diagnostic", actions)
        self.assertIn("u-proxy-diagnostic", SOURCE[:start])
        self.assertNotIn("u-test-row", SOURCE)

        actions_rule = css_rule(".u-proxy-actions")
        self.assertIn("min-width:0", actions_rule)
        self.assertNotIn("max-content", actions_rule)
        diagnostic_rule = css_rule(".u-proxy-diagnostic")
        self.assertIn("min-width:0", diagnostic_rule)
        self.assertIn("overflow:hidden", diagnostic_rule)
        self.assertIn("text-overflow:ellipsis", diagnostic_rule)
        self.assertIn("white-space:nowrap", diagnostic_rule)

    def test_country_udp_test_has_persistent_busy_and_result_state(self):
        self.assertIn("const [exitTests, setExitTests] = useState({})", SOURCE)
        self.assertIn("disabled={exitTest?.busy || saving}", SOURCE)
        self.assertIn("exitTest?.busy ? 'Testing…' : 'Test UDP'", SOURCE)
        self.assertIn("u-exit-test-result", SOURCE)
        self.assertIn("await persistSettings()", SOURCE)

    def test_async_feedback_has_a_permanent_slot_and_fixed_action_columns(self):
        self.assertIn('className="u-exit-button-row"', SOURCE)
        self.assertIn("exitTest?.busy ? t('Testing…')", SOURCE)
        self.assertIn("aria-live=\"polite\"", SOURCE)
        self.assertIn("grid-template-columns:minmax(220px,.85fr) minmax(300px,1.6fr) minmax(270px,1fr) 190px", CSS)
        self.assertIn(".u-exit-actions { width:190px", CSS)
        self.assertIn(".u-exit-test-result { display:block; flex:0 1 104px", CSS)
        self.assertIn(".u-proxy-actions { width:190px", CSS)
        self.assertIn(".u-test-action { width:104px; }", CSS)

    def test_country_result_is_in_the_status_area_not_under_remove(self):
        actions = SOURCE[SOURCE.index('<div className="u-exit-actions">'):]
        actions = actions[:actions.index("</div>") + len("</div>")]
        self.assertNotIn("u-exit-test-result", actions)
        runtime = SOURCE[SOURCE.index('<div className="u-exit-runtime">'):]
        runtime = runtime[:runtime.index("</div>")]
        self.assertIn("u-exit-test-result", runtime)

        exit_row = css_rule(".u-exit-row")
        self.assertIn(".u-proxy-row { display:grid;", CSS)
        self.assertIn(".u-proxy-row { display:grid; grid-template-columns:minmax(220px,.9fr) minmax(300px,2fr) minmax(220px,1fr) 190px; align-items:center", CSS)
        self.assertIn("align-items:center", exit_row)
        self.assertIn("padding:11px 14px; }", CSS[CSS.index(".u-proxy-row { display:grid;"):])
        self.assertIn("padding:11px 14px", exit_row)

    def test_saved_assignment_is_distinct_from_a_running_node(self):
        self.assertIn("t('Saved · idle')", SOURCE)
        self.assertIn("t('Saved assignment')", SOURCE)
        self.assertIn("t('Runtime node')", SOURCE)
        self.assertIn("const [saveState, setSaveState] = useState('loading')", SOURCE)
        self.assertIn("setS(saved)", SOURCE)

    def test_proxy_and_country_records_follow_their_container_width(self):
        self.assertIn('className="u-egress-list"', SOURCE)
        self.assertIn('className="card u-exit-row"', SOURCE)
        self.assertIn("container:proxy-list / inline-size", CSS)
        self.assertIn("container:egress-list / inline-size", CSS)
        self.assertIn("@container proxy-list (max-width:1100px)", CSS)
        self.assertIn("@container egress-list (max-width:720px)", CSS)

    def test_pages_fill_the_browser_and_transient_surfaces_have_no_black_shadow(self):
        page = css_rule(".u-page")
        toast = css_rule(".u-toast")
        save_bar = css_rule(".u-egress-save-bar")
        self.assertIn("max-width:none", page)
        self.assertIn("box-shadow:none", toast)
        self.assertIn("box-shadow:none", save_bar)

    def test_draft_vowifi_notice_links_to_each_missing_setup_area(self):
        self.assertIn("<DraftProvisioningNotice device={d} setTab={setTab}/>", SOURCE)
        self.assertIn("onClick={() => setTab('sim')}", SOURCE)
        self.assertIn("onClick={() => setTab('hardware')}", SOURCE)
        self.assertIn("device?.provisioning?.state === 'draft'", SOURCE)


if __name__ == "__main__":
    unittest.main()
