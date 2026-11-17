"""Recommendation scorecard: grading rules, conservatism, dedupe and summaries. All prices and bars here are SYNTHETIC."""
import os
import shutil
import tempfile
import unittest

from _load import load

sc = load("webui", "scorecard")

T0 = 1_700_000_000
H = 3600


def bar(i, hi, lo, cl, op=None):
    """Bar i hours after the plan was recorded: (t, high, low, close, open)."""
    return (T0 + i * H, hi, lo, cl, cl if op is None else op)


def plan(kind="pullback", side="long", entry=100.0, stop=95.0, targets=(105.0, 110.0, 115.0), rank=1):
    return dict(name="LONG on a pullback", side=side, kind=kind, entry=entry, stop=stop, targets=[dict(price=t) for t in targets], rank=rank, grade="A", score=7.0)


def rec(**kw):
    return sc.make_record("GC=F", "1h", plan(**kw), T0)


NOW = T0 + 200 * H


class Limit(unittest.TestCase):
    def test_a_touch_is_not_a_fill_and_the_plan_expires(self):
        bars = [bar(i, 101, 100.0, 100.5) for i in range(1, 20)]          # low only touches 100.0, never one tick through
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual(g["state"], "NOT TRIGGERED")
        self.assertIsNone(g["r_net"])

    def test_fill_then_first_target_is_a_win_and_breakeven_stop_follows(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 106, 100.5, 105.5), bar(3, 106, 99, 100.5)]
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual((g["state"], g["best_target"]), ("WIN", 1))
        self.assertAlmostEqual(g["r_gross"], 0.5, places=6)               # half the position at +1R, the rest stopped at the entry
        self.assertAlmostEqual(g["r_net"], 0.5 - 0.5 / 5.0, places=6)     # minus the round-trip cost in R

    def test_no_target_credit_on_the_fill_bar(self):
        bars = [bar(1, 106, 99.5, 105.8)]                                 # fills AND the high is past target 1, same bar
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual(g["state"], "OPEN")
        self.assertEqual(g["best_target"], 0)

    def test_stop_and_target_in_one_bar_means_the_stop(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 106, 94.0, 100.0)]
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual(g["state"], "LOSS")
        self.assertAlmostEqual(g["r_gross"], -1.0, places=6)

    def test_a_gap_through_the_stop_exits_at_the_open_not_the_stop(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 93, 92, 92.5, op=93.0)]
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual(g["state"], "LOSS")
        self.assertLess(g["r_gross"], -1.3)

    def test_three_targets_is_a_win_with_the_blended_result(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 101, 100.2, 100.8), bar(3, 116, 101, 115.5)]
        g = sc.grade(rec(), bars, NOW)
        self.assertEqual((g["state"], g["best_target"]), ("WIN", 3))
        self.assertAlmostEqual(g["r_gross"], 0.5 * 1 + 0.3 * 2 + 0.2 * 3, places=6)

    def test_price_closing_beyond_the_stop_before_the_fill_invalidates(self):
        g = sc.grade(rec(kind="breakout"), [bar(1, 99.0, 94.0, 94.5)], NOW)        # a breakout idea dies if price closes beyond the stop first
        self.assertEqual(g["state"], "INVALIDATED")

    def test_short_side_mirrors_the_rules(self):
        r = sc.make_record("GC=F", "1h", plan(side="short", entry=100.0, stop=105.0, targets=(95.0, 90.0, 85.0)), T0)
        g = sc.grade(r, [bar(1, 100.5, 99, 99.5), bar(2, 99.5, 94.0, 94.5), bar(3, 100.5, 94, 99)], NOW)
        self.assertEqual((g["state"], g["best_target"]), ("WIN", 1))

    def test_unresolved_trade_times_out_and_is_scored_at_the_close(self):
        r = rec()
        bars = [bar(1, 101, 99.5, 100.2)] + [bar(i, 102, 99.6, 101.0) for i in range(2, 2 + r["hold_bars"] + 2)]
        g = sc.grade(r, bars, NOW)
        self.assertEqual(g["state"], "TIMED OUT")
        self.assertIsNotNone(g["r_net"])


class Breakout(unittest.TestCase):
    def test_confirming_close_then_fill_at_the_next_open(self):
        bars = [bar(1, 101.5, 99, 101.0), bar(2, 106, 100.8, 105.5, op=101.2)]
        g = sc.grade(rec(kind="breakout"), bars, NOW)
        self.assertEqual(g["state"], "WIN")
        self.assertAlmostEqual(g["fill_price"], 101.2)

    def test_a_wick_through_the_entry_without_a_close_is_not_a_trigger(self):
        bars = [bar(i, 103, 99, 99.8) for i in range(1, 20)]
        self.assertEqual(sc.grade(rec(kind="breakout"), bars, NOW)["state"], "NOT TRIGGERED")

    def test_a_gap_past_the_first_target_is_not_a_trade(self):
        bars = [bar(1, 101.5, 99, 101.0), bar(2, 112, 106, 111, op=107.0)]
        self.assertEqual(sc.grade(rec(kind="breakout"), bars, NOW)["state"], "NOT TRIGGERED")


class Causal(unittest.TestCase):
    def test_bars_before_the_recommendation_and_forming_bars_are_ignored(self):
        r = rec()
        early = (T0 - H, 120, 90, 100, 100)                               # starts before the plan was recorded
        forming = bar(5, 101, 99.0, 100)                                  # not closed at 'now'
        g = sc.grade(r, [early, forming], T0 + 5 * H + 60)
        self.assertEqual((g["state"], g["checked_bars"]), ("WAITING", 0))

    def test_grading_is_prefix_stable_once_resolved(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 106, 100.5, 105.5), bar(3, 106, 99, 100.5), bar(4, 200, 1, 50)]
        a = sc.grade(rec(), bars[:3], NOW)
        b = sc.grade(rec(), bars, NOW)
        self.assertEqual((a["state"], round(a["r_net"], 6)), (b["state"], round(b["r_net"], 6)))


class Records(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sc.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_bad_geometry_is_not_recorded(self):
        self.assertIsNone(sc.make_record("GC=F", "1h", plan(stop=101.0), T0))
        self.assertIsNone(sc.make_record("GC=F", "1h", plan(targets=(99.0, 98.0, 97.0)), T0))
        self.assertIsNone(sc.make_record("GC=F", "9h", plan(), T0))

    def test_only_shown_plans_are_recorded_once(self):
        recs = []
        added = sc.record(recs, "GC=F", "1h", [plan(rank=1), plan(rank=2, entry=99.0), plan(rank=7, entry=98.0)], T0)
        self.assertEqual(len(added), 2)
        self.assertEqual(sc.record(recs, "GC=F", "1h", [plan(rank=1)], T0 + 60), [])      # same plan, still alive: not added twice

    def test_a_resolved_plan_can_be_recommended_again_after_its_window(self):
        recs = []
        sc.record(recs, "GC=F", "1h", [plan()], T0)
        recs[0]["result"] = dict(state="LOSS", final=True)
        self.assertEqual(sc.record(recs, "GC=F", "1h", [plan()], T0 + 5 * H), [])         # inside its own 12 bar window
        self.assertEqual(len(sc.record(recs, "GC=F", "1h", [plan()], T0 + 20 * H)), 1)

    def test_summary_counts_and_bands(self):
        rows = []
        for st, r in (("WIN", 0.5), ("WIN", 1.0), ("LOSS", -1.1), ("NOT TRIGGERED", None), ("OPEN", 0.2)):
            x = rec()
            x["result"] = dict(state=st, r_net=r)
            rows.append(x)
        s = sc.summary(rows)["all"]
        self.assertEqual((s["resolved"], s["counts"]["WIN"], s["counts"]["LOSS"], s["triggered"]), (3, 2, 1, 4))
        self.assertAlmostEqual(s["win_rate"], 2 / 3)
        self.assertFalse(s["enough"])
        lo, hi = s["win_ci"]
        self.assertTrue(0 <= lo < s["win_rate"] < hi <= 1)
        self.assertAlmostEqual(s["mean_r"], (0.5 + 1.0 - 1.1) / 3)

    def test_story_says_won_lost_or_nothing(self):
        r = rec()
        r["result"] = sc.grade(r, [bar(1, 101, 99.5, 100.2), bar(2, 106, 100.5, 105.5)], NOW)
        self.assertIn("WON", sc.story(r))
        r["result"] = sc.grade(r, [bar(1, 101, 99.5, 100.2), bar(2, 101, 94, 94.5)], NOW)
        self.assertIn("LOST", sc.story(r))
        r["result"] = sc.grade(r, [bar(i, 101, 100.0, 100.5) for i in range(1, 20)], NOW)
        self.assertIn("Nothing would have been lost or won", sc.story(r))

    def test_snapshot_records_grades_and_persists_with_a_fake_analysis(self):
        bars = [bar(1, 101, 99.5, 100.2), bar(2, 106, 100.5, 105.5), bar(3, 106, 99, 100.5)]
        res = dict(setup={"1h": dict(plans=[plan()])}, bars={k: bars for k in ("15m", "1h", "4h", "1d", "1w")})

        def fake(sym):
            return res
        # the plan is recorded 'now', so give snapshot a clock just before the fake bars
        d = sc.snapshot(symbols=("GC=F",), now=T0, path=self.path, analyse=fake)
        self.assertEqual(d["added"], 1)
        d2 = sc.snapshot(symbols=("GC=F",), now=NOW, path=self.path, analyse=fake)
        self.assertEqual(d2["summary"]["all"]["counts"]["WIN"], 1)
        self.assertEqual(len(sc.load(self.path)), 1)
        self.assertIn("WON", d2["rows"][0]["story"])

    def test_a_failing_data_source_is_reported_not_raised(self):
        def boom(sym):
            raise SystemExit("offline")
        d = sc.snapshot(symbols=("GC=F",), now=NOW, path=self.path, analyse=boom)
        self.assertEqual(len(d["errors"]), 1)

    def test_record_from_json_reads_every_horizon(self):
        out = dict(intraday={"15m": dict(plans=[plan()]), "1h": dict(plans=[plan(entry=99.0)])}, short=dict(plans=[plan(entry=98.0)]), long=dict(plans=[plan(entry=97.0)]))
        self.assertEqual(len(sc.record_from_json(out, "GC=F", now=T0, path=self.path)), 4)
        self.assertEqual(sc.record_from_json(out, "GC=F", now=T0 + 60, path=self.path), [])


if __name__ == "__main__":
    unittest.main()
