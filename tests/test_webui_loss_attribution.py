"""Loss attribution: evidence-only causes, unknowns, clustering and the simulator audit. All bars and plans are SYNTHETIC."""
import copy
import unittest

from _load import load

la = load("webui", "loss_attribution")
sc = load("webui", "scorecard")

T0 = 1_700_000_000 // 3600 * 3600
H = 3600
BASE = [(T0 - (100 - i) * H, 100.5, 99.5, 100.0, 100.0) for i in range(100)]


def bar(i, hi, lo, cl, op=None):
    return (T0 + i * H, hi, lo, cl, cl if op is None else op)


def plan(kind="pullback", side="long", entry=100.0, stop=95.0, targets=(105.0, 110.0, 115.0), **extra):
    d = dict(name="LONG on a pullback", side=side, kind=kind, entry=entry, stop=stop, targets=[dict(price=t) for t in targets], rank=1, grade="A", score=7.0,
             trend="with", weak=False, why=["support"])
    d.update(extra)
    return d


def graded(tail, **kw):
    r = sc.make_record("GC=F", "1h", plan(**kw), T0)
    bars = BASE + tail
    r["result"] = sc.grade(r, bars, T0 + 300 * H)
    return r, bars


def population(recs, bars, events=()):
    return la.build(recs, {"GC=F": {"1h": bars}}, list(events), "test")


def causes(pop, rid):
    row = next(x for x in pop["rows"] if x["id"] == rid)
    return row


class Causes(unittest.TestCase):
    def test_a_false_breakout_is_flagged_from_the_bars(self):
        r, bars = graded([bar(1, 101.8, 100.5, 101.5), bar(2, 102.0, 97.5, 98.0, op=101.5)], kind="breakout", entry=101.0, stop=98.0, targets=(104.0, 107.0, 110.0))
        self.assertEqual(r["result"]["state"], "LOSS")
        row = causes(population([r], bars), r["id"])
        self.assertIn("false_breakout", row["causes"])
        self.assertIn("closed back below", row["causes"]["false_breakout"])

    def test_a_fast_stop_after_a_limit_fill_is_premature_and_a_later_recovery_marks_a_tight_stop(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0), bar(2, 106.0, 96.0, 105.5)])
        self.assertEqual(r["result"]["state"], "LOSS")
        row = causes(population([r], bars), r["id"])
        self.assertIn("premature_entry", row["causes"])
        self.assertIn("tight_stop", row["causes"])
        self.assertIn("no_confirmation", row["causes"])

    def test_news_inside_the_trade_window_is_flagged_and_a_calendar_outage_is_unknown(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        ev = [dict(ts=T0 + H + 600, importance="high", name="CPI release")]
        self.assertIn("news", causes(population([r], bars, ev), r["id"])["causes"])
        pop = la.build([r], {"GC=F": {"1h": bars}}, None, "test")
        self.assertIn("news", causes(pop, r["id"])["unknown_causes"])

    def test_spread_regime_and_model_are_unknown_never_guessed(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        row = causes(population([r], bars), r["id"])
        for k in ("high_spread", "wrong_regime", "model_error"):
            self.assertIn(k, row["unknown_causes"])
            self.assertNotIn(k, row["causes"])

    def test_old_records_without_a_trend_label_make_countertrend_unknown(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        r["trend"] = None
        self.assertIn("countertrend", causes(population([r], bars), r["id"])["unknown_causes"])
        r["trend"] = "counter"
        self.assertIn("countertrend", causes(population([r], bars), r["id"])["causes"])

    def test_a_loss_with_no_recorded_evidence_is_reported_as_such(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)], kind="breakout", entry=101.0, stop=94.0, targets=(104.0, 107.0, 110.0))
        r["result"] = dict(r["result"])
        pop = population([r], bars)
        self.assertIn("none_supported", pop["losses"])


class Population(unittest.TestCase):
    def test_open_positions_are_never_counted_as_wins_or_losses(self):
        r, bars = graded([bar(1, 101.0, 99.5, 100.2), bar(2, 106.0, 100.5, 105.5)])
        self.assertEqual(r["result"]["state"], "OPEN")
        pop = population([r], bars)
        self.assertEqual((pop["counts"]["completed"], pop["counts"]["open"], pop["stats_all"]["wins"], pop["stats_all"]["losses"]), (0, 1, 0, 0))
        self.assertEqual(pop["counts"]["executed_on_a_broker"], 0)

    def test_the_same_idea_recommended_twice_is_one_opportunity(self):
        a, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        b = copy.deepcopy(a)
        b["id"], b["created_ts"], b["created"], b["entry"] = "rc-second", a["created_ts"] + 600, "x", a["entry"] + 0.2
        pop = population([a, b], bars)
        self.assertEqual((pop["counts"]["recommendations"], pop["counts"]["unique_opportunities"]), (2, 1))
        self.assertLessEqual(pop["stats_deduplicated"]["completed"], 1)
        self.assertIn("duplicate_idea", causes(pop, a["id"])["causes"])

    def test_clearly_different_zones_are_separate_opportunities(self):
        a, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        b = copy.deepcopy(a)
        b["id"], b["entry"], b["stop"], b["targets"] = "rc-far", 140.0, 135.0, [145.0, 150.0, 155.0]
        _, size = la.clusters([a, b], {})
        self.assertEqual(len(size), 2)

    def test_regime_and_session_are_attached_to_filled_rows(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        row = causes(population([r], bars), r["id"])
        self.assertIsNotNone(row["regime"])
        self.assertIn(row["session"], ("asia", "london", "ny", "off"))


class Audit(unittest.TestCase):
    def test_a_sound_record_passes_every_check(self):
        r, bars = graded([bar(1, 101.0, 99.5, 100.2), bar(2, 106.0, 100.5, 105.5), bar(3, 106.0, 99.0, 100.5)])
        ctx = la.Context(bars, 3600)
        self.assertEqual(la.audit(r, ctx), [])

    def test_a_tampered_result_is_caught(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        r["result"]["r_gross"] += 0.5
        self.assertTrue(any("recomputed" in m for m in la.audit(r, la.Context(bars, 3600))))
        r2, bars2 = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        r2["result"]["fill_price"] = 150.0
        self.assertTrue(la.audit(r2, la.Context(bars2, 3600)))

    def test_a_simulation_error_is_listed_as_a_cause_of_the_loss(self):
        r, bars = graded([bar(1, 101.0, 94.0, 95.0, op=100.0)])
        r["result"]["legs"][0]["frac"] = 0.5
        row = causes(population([r], bars), r["id"])
        self.assertIn("simulation_error", row["causes"])


class Fills(unittest.TestCase):
    def test_breakout_fill_gap_is_reported_with_its_explanation(self):
        r, bars = graded([bar(1, 101.8, 100.5, 101.5), bar(2, 102.0, 97.5, 98.0, op=101.9)], kind="breakout", entry=101.0, stop=98.0, targets=(104.0, 107.0, 110.0))
        pop = population([r], bars)
        self.assertEqual(pop["fill_gap"]["breakout_fills"], 1)
        self.assertAlmostEqual(pop["fill_gap"]["mean"], 0.9, places=6)
        self.assertIn("NEXT bar", pop["fill_gap"]["note"])


if __name__ == "__main__":
    unittest.main()
