import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import main, store


class LineHistoryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self._patch = patch.multiple(store, DATA_DIR=str(root),
                                     DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                     PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self._patch.start()
        store.init()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()

    def test_repeated_samples_merge_into_one_segment(self):
        for offset in range(0, 40, 4):
            store.record_line_state("1", "up", ts=1000 + offset)
        rows = store.line_states("1", 0)
        self.assertEqual(rows, [{"state": "up", "start_ts": 1000, "end_ts": 1036,
                                 "reason": "", "detail": ""}])

    def test_state_change_continues_where_the_previous_state_ended(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("1", "up", ts=1004)
        store.record_line_state("1", "down", ts=1008)
        rows = store.line_states("1", 0)
        self.assertEqual([(r["state"], r["start_ts"], r["end_ts"]) for r in rows],
                         [("up", 1000, 1004), ("down", 1004, 1008)])

    def test_history_is_kept_per_line(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("2", "down", ts=1000)
        self.assertEqual([r["state"] for r in store.line_states("1", 0)], ["up"])
        self.assertEqual([r["state"] for r in store.line_states("2", 0)], ["down"])

    def test_a_clock_step_backwards_never_inverts_a_segment(self):
        store.record_line_state("1", "up", ts=2000)
        store.record_line_state("1", "up", ts=1400)
        rows = store.line_states("1", 0)
        self.assertEqual(rows, [{"state": "up", "start_ts": 2000, "end_ts": 2000,
                                 "reason": "", "detail": ""}])

    def test_control_plane_downtime_stays_visible_as_a_hole(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("1", "up", ts=1040)
        # ... control plane restarted; nothing was observed for an hour ...
        store.record_line_state("1", "up", ts=4640)
        timeline = store.line_state_timeline("1", 1000, 4680)
        self.assertEqual([(s["state"], s["start"], s["end"]) for s in timeline],
                         [("up", 1000, 1040), ("unknown", 1040, 4640), ("up", 4640, 4680)])

    def test_sampling_jitter_does_not_fragment_the_timeline(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("1", "up", ts=1030)
        timeline = store.line_state_timeline("1", 1000, 1060)
        self.assertEqual([(s["state"], s["start"], s["end"]) for s in timeline],
                         [("up", 1000, 1060)])

    def test_window_before_any_record_is_reported_as_unknown(self):
        store.record_line_state("1", "up", ts=5000)
        store.record_line_state("1", "up", ts=5040)
        timeline = store.line_state_timeline("1", 1000, 5040)
        self.assertEqual(timeline[0], {"state": "unknown", "start": 1000, "end": 5000})

    def test_segments_are_clipped_to_the_requested_window(self):
        for ts in range(1000, 9001, 60):
            store.record_line_state("1", "up", ts=ts)
        timeline = store.line_state_timeline("1", 4000, 6000)
        self.assertEqual(timeline, [{"state": "up", "start": 4000, "end": 6000,
                                     "reason": "", "detail": ""}])

    def test_new_zero_length_state_at_window_start_remains_visible(self):
        store.record_line_state('1', 'down', ts=5000, reason='reg_unanswered')
        timeline = store.line_state_timeline('1', 5000, 5000)
        self.assertEqual(timeline[0]['state'], 'down')

    def test_an_outage_keeps_the_reason_it_began_with(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("1", "down", ts=1004, reason="tunnel_network")
        # The recovery passes through "registering", which says nothing about the cause.
        store.record_line_state("1", "down", ts=1008, reason="registering")
        store.record_line_state("1", "up", ts=1012)
        timeline = store.line_state_timeline("1", 1000, 1012)
        down = next(s for s in timeline if s["state"] == "down")
        self.assertEqual(down["reason"], "tunnel_network")

    def test_a_late_reason_fills_a_segment_that_began_without_one(self):
        store.record_line_state("1", "down", ts=1000)
        store.record_line_state("1", "down", ts=1004, reason="reg_rejected")
        self.assertEqual(store.line_states("1", 0)[0]["reason"], "reg_rejected")

    def test_detail_travels_with_the_reason_it_belongs_to(self):
        evidence = "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org → DNS 223.5.5.5"
        store.record_line_state("1", "down", ts=1000, reason="epdg_unresolved",
                                detail=evidence)
        # A later sample with a different story must not overwrite the original evidence.
        store.record_line_state("1", "down", ts=1004, reason="registering",
                                detail="P-CSCF fd00::5")
        row = store.line_states("1", 0)[0]
        self.assertEqual((row["reason"], row["detail"]), ("epdg_unresolved", evidence))

    def test_stronger_transport_cause_replaces_earlier_registration_symptom(self):
        store.record_line_state("1", "down", ts=1000, reason="reg_rejected",
                                detail="SIP Rejected")
        store.record_line_state("1", "down", ts=1004,
                                reason="tunnel_child_rekey_timeout",
                                detail="CREATE_CHILD_SA unanswered")
        row = store.line_states("1", 0)[0]
        self.assertEqual((row["reason"], row["detail"]),
                         ("tunnel_child_rekey_timeout", "CREATE_CHILD_SA unanswered"))

    def test_summary_counts_only_observed_time(self):
        summary = store.line_state_summary([
            {"state": "up", "start": 0, "end": 900},
            {"state": "down", "start": 900, "end": 1000},
            {"state": "off", "start": 1000, "end": 1500},
            {"state": "unknown", "start": 1500, "end": 3000},
        ])
        self.assertEqual(summary["observed_seconds"], 1000)
        self.assertEqual(summary["uptime_ratio"], 0.9)
        self.assertEqual(summary["outages"], 1)
        self.assertEqual(summary["longest_outage_seconds"], 100)

    def test_summary_has_no_availability_without_observations(self):
        summary = store.line_state_summary([{"state": "unknown", "start": 0, "end": 3600}])
        self.assertIsNone(summary["uptime_ratio"])

    def test_retention_prunes_only_aged_history(self):
        store.record_line_state("1", "up", ts=1000)
        store.record_line_state("1", "up", ts=1040)
        store.record_line_state("1", "down", ts=9000)
        self.assertEqual(store.prune_line_states(5000), 1)
        self.assertEqual([r["state"] for r in store.line_states("1", 0)], ["down"])

    def test_deleting_a_line_removes_the_history_its_id_would_hand_on(self):
        store.record_line_state("1", "up", ts=1000)
        store.clear_line_states("1")
        self.assertEqual(store.line_states("1", 0), [])
        self.assertIsNone(store.line_state_recorded_since("1"))

    def test_recorded_since_reports_the_oldest_retained_observation(self):
        store.record_line_state("1", "down", ts=1000)
        store.record_line_state("1", "up", ts=1040)
        self.assertEqual(store.line_state_recorded_since("1"), 1000)

    def test_month_history_survives_pruning_but_aged_rows_do_not(self):
        now = 40 * 86400
        store.record_line_state("1", "down", ts=now - 35 * 86400)
        store.record_line_state("1", "up", ts=now - 20 * 86400)
        store.prune_line_states(now - store.LINE_STATE_RETENTION_SECONDS)
        self.assertEqual([r["state"] for r in store.line_states("1", 0)], ["up"])


class HistoryRangeApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        self.enterContext(patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(root / 'test.sqlite'),
                                        PREVIOUS_DB_PATH=str(root / 'old.sqlite')))
        store.init()
        self.now = 40 * 86400
        self.enterContext(patch.object(main.time, 'time', return_value=self.now))
        self.enterContext(patch.object(main.cfg, 'get_instance', return_value={'id': 'fixture'}))

    async def test_each_preset_returns_its_exact_window_even_before_first_observation(self):
        for span in (900, 1800, 3600, 10800, 21600, 43200, 86400, 172800,
                     259200, 604800, 1209600, 2592000):
            with self.subTest(span=span):
                result = await main.api_instance_availability('fixture', span)
                self.assertEqual((result['start'], result['end']), (self.now - span, self.now))
                self.assertEqual(result['span_seconds'], span)
                self.assertEqual(result['summary']['unknown'], span)
                self.assertIsNone(result['summary']['uptime_ratio'])
                self.assertEqual(result['summary']['outages'], 0)

    async def test_summary_and_outage_list_are_recomputed_for_the_selected_window(self):
        with store._conn() as connection:
            for state, start, end in [('up', self.now - 21600, self.now - 7200),
                                      ('down', self.now - 7200, self.now - 3600),
                                      ('up', self.now - 3600, self.now)]:
                connection.execute('INSERT INTO line_states(instance,state,start_ts,end_ts) VALUES(?,?,?,?)',
                                   ('fixture', state, start, end))
        recent = await main.api_instance_availability('fixture', 3600)
        longer = await main.api_instance_availability('fixture', 21600)
        self.assertEqual(recent['summary']['uptime_ratio'], 1)
        self.assertEqual(recent['summary']['outages'], 0)
        self.assertEqual(longer['summary']['outages'], 1)
        self.assertEqual(longer['summary']['longest_outage_seconds'], 3600)
        self.assertAlmostEqual(longer['summary']['uptime_ratio'], 5 / 6)
        self.assertTrue(all(s['start'] >= recent['start'] for s in recent['segments']))

    async def test_invalid_ranges_cannot_create_unbounded_queries(self):
        for span in (-1, 0, 899, 2592001, True, 3600.5):
            with self.subTest(span=span), self.assertRaises(main.HTTPException) as error:
                await main.api_instance_availability('fixture', span)
            self.assertEqual(error.exception.status_code, 400)

    def test_legacy_auto_and_keepalive_window_stay_bounded_at_two_days(self):
        self.assertEqual(main._availability_window(self.now, None), 3600)
        self.assertEqual(main._availability_window(self.now, 0), 172800)

    async def test_missing_line_is_still_rejected(self):
        with patch.object(main.cfg, 'get_instance', return_value=None), self.assertRaises(main.HTTPException) as error:
            await main.api_instance_availability('absent', 3600)
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
