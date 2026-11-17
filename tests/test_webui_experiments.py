"""Phases 3 to 5 experiments: entry-mode equivalence with the live grader, regime v2, statistics and the pre-registered decision rule. SYNTHETIC data."""
import os
import random
import shutil
import tempfile
import unittest

from _load import load

ex = load("webui", "experiments")
sc = load("webui", "scorecard")
la = load("webui", "loss_attribution")
gi = load("webui", "gold_intraday")

T0 = 1_700_000_000 // 3600 * 3600
H = 3600


def bar(i, hi, lo, cl, op=None):
    return (T0 + i * H, hi, lo, cl, cl if op is None else op)


def plan(kind="pullback", side="long", entry=100.0, stop=95.0, targets=(105.0, 110.0, 115.0), **extra):
    d = dict(name="x", side=side, kind=kind, entry=entry, stop=stop, targets=[dict(price=t) for t in targets], rank=1, grade="A", score=7.0, trend="with", weak=False, blend_r=2.0)
    d.update(extra)
    return d


def rec(**kw):
    return sc.make_record("GC=F", "1h", plan(**kw), T0)


NOW = T0 + 500 * H


class Equivalence(unittest.TestCase):
    def test_grade_mode_matches_the_live_grader_on_random_paths(self):
        rnd = random.Random(5)
        checked = 0
        for case in range(400):
            kind = "pullback" if case % 2 == 0 else "breakout"
            side = "long" if (case // 2) % 2 == 0 else "short"
            entry = 100.0
            risk = rnd.choice([2.0, 3.0, 5.0])
            stop = entry - risk if side == "long" else entry + risk
            tg = [entry + (1 if side == "long" else -1) * risk * m for m in (1.2, 2.2, 3.5)]
            r = sc.make_record("GC=F", "1h", plan(kind=kind, side=side, entry=entry, stop=stop, targets=tg), T0)
            p, bars = 100.0 + rnd.uniform(-3, 3), []
            for i in range(1, 45):
                o = p
                p = max(50.0, p + rnd.uniform(-2.5, 2.5))
                hi, lo = max(o, p) + rnd.uniform(0, 1.2), min(o, p) - rnd.uniform(0, 1.2)
                bars.append((T0 + i * H, hi, lo, p, o))
            now = bars[-1][0] + H
            a = sc.grade(r, bars, now)
            b = ex.grade_mode(r, bars, now, "limit" if kind == "pullback" else "close")
            self.assertEqual(a["state"], b["state"], "case %d %s %s" % (case, kind, side))
            if a.get("fill_price") is not None:
                self.assertAlmostEqual(a["fill_price"], b["fill_price"], places=9)
                self.assertAlmostEqual(a["r_net"], b["r_net"], places=9)
                self.assertEqual([round(l["price"], 6) for l in a["legs"]], [round(l["price"], 6) for l in b["legs"]])
                checked += 1
        self.assertGreater(checked, 100)


class Modes(unittest.TestCase):
    def test_stop_order_fills_at_the_worse_of_open_and_level_plus_a_tick_with_no_target_credit_that_bar(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        g = ex.grade_mode(r, [bar(1, 108.0, 99.0, 106.0, op=100.8)], NOW, "stop")           # trigger bar also runs past target 1
        self.assertAlmostEqual(g["fill_price"], 100.8 + r["tick"])
        self.assertEqual((g["state"], g["best_target"]), ("OPEN", 0))
        g2 = ex.grade_mode(r, [bar(1, 101.0, 99.0, 100.5, op=99.5)], NOW, "stop")
        self.assertAlmostEqual(g2["fill_price"], 100.0 + r["tick"])

    def test_stop_order_gap_past_the_first_target_is_not_a_trade(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        self.assertEqual(ex.grade_mode(r, [bar(1, 112, 107, 110, op=106.0)], NOW, "stop")["state"], "NOT TRIGGERED")

    def test_retest_waits_for_a_return_then_enters_at_the_next_open(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        bars = [bar(1, 102.0, 100.5, 101.5), bar(2, 103.0, 101.5, 102.5), bar(3, 102.0, 100.1, 101.0), bar(4, 107.0, 101.5, 106.0, op=101.2)]
        g = ex.grade_mode(r, bars, NOW, "retest", atr=2.0)                                 # bar 3 comes back within 0.3 of the level and closes above it
        self.assertAlmostEqual(g["fill_price"], 101.2)
        self.assertEqual(g["state"], "OPEN")

    def test_retest_is_missed_when_the_first_target_comes_first(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        g = ex.grade_mode(r, [bar(1, 102.0, 100.5, 101.5), bar(2, 106.0, 102.0, 105.5)], NOW, "retest", atr=2.0)
        self.assertEqual(g["state"], "NOT TRIGGERED")
        self.assertIn("missed", g["note"])

    def test_retest_is_cancelled_by_a_close_beyond_the_stop_and_expires_without_a_return(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        self.assertEqual(ex.grade_mode(r, [bar(1, 102.0, 100.5, 101.5), bar(2, 101.0, 93.0, 94.0)], NOW, "retest", atr=2.0)["state"], "INVALIDATED")
        far = [bar(1, 102.0, 100.5, 101.5)] + [bar(i, 103.5, 102.5, 103.0) for i in range(2, 12)]
        self.assertEqual(ex.grade_mode(r, far, NOW, "retest", atr=2.0)["state"], "NOT TRIGGERED")

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            ex.grade_mode(rec(), [], NOW, "market")


class Regime(unittest.TestCase):
    def snap(self, **kw):
        s = dict(adx=30.0, er=0.4, slope_atr=0.05, vol_ratio=1.0, bias="bullish", post_news=False, atr=2.0)
        s.update(kw)
        return s

    def test_nine_classes_are_reachable_and_depend_on_both_timeframes(self):
        up = self.snap()
        f = lambda s1, s4=None, **k: dict(s1=s1, s4=s4 or dict(bias=s1.get("bias")), **k)   # noqa: E731
        self.assertEqual(ex.regime_v2(f(up)), "strong_bull")
        self.assertEqual(ex.regime_v2(f(self.snap(slope_atr=-0.05, bias="bearish"))), "strong_bear")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=20.0, er=0.25))), "weak_bull")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=20.0, er=0.25, slope_atr=-0.01, bias="bearish"))), "weak_bear")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=12.0, er=0.1, bias="range"))), "sideways")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=15.0, er=0.1, vol_ratio=0.6, bias="range"))), "vol_compression")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=15.0, er=0.1, vol_ratio=1.5, bias="range"))), "vol_expansion")
        self.assertEqual(ex.regime_v2(f(up, news_near=True)), "news_instability")
        self.assertEqual(ex.regime_v2(f(up, s4=dict(bias="bearish"))), "uncertain")                 # H1 and H4 disagree
        self.assertEqual(ex.regime_v2(dict(s1=None, s4=None)), "uncertain")
        self.assertEqual(ex.regime_v2(f(self.snap(adx=None))), "uncertain")

    def test_a_green_candle_alone_does_not_make_a_bullish_regime(self):
        f = dict(s1=self.snap(adx=12.0, er=0.1, bias="range"), s4=dict(bias="range"), candle_up=True)
        self.assertEqual(ex.regime_v2(f), "sideways")

    def test_masking_a_group_removes_its_condition(self):
        f = dict(s1=self.snap(), s4=dict(bias="bearish"))
        self.assertEqual(ex.regime_v2(f), "uncertain")
        self.assertEqual(ex.regime_v2(f, ("h4_bias",)), "strong_bull")
        self.assertEqual(ex.regime_v2(dict(s1=self.snap(), s4=dict(bias="bullish"), news_near=True), ("news",)), "strong_bull")


class Stats(unittest.TestCase):
    def test_eta_squared_is_zero_for_no_structure_and_one_for_pure_groups(self):
        self.assertAlmostEqual(ex.eta_squared(["a", "a", "b", "b"], [1.0, 3.0, 1.0, 3.0]), 0.0)
        self.assertAlmostEqual(ex.eta_squared(["a", "a", "b", "b"], [1.0, 1.0, 3.0, 3.0]), 1.0)
        self.assertIsNone(ex.eta_squared(["a", "b"], [1.0, 1.0]))

    def test_permutation_null_is_small_for_random_data(self):
        rnd = random.Random(3)
        labels = [rnd.choice("abcd") for _ in range(300)]
        vals = [rnd.gauss(0, 1) for _ in range(300)]
        self.assertLess(ex.permutation_null(labels, vals), 0.06)

    def test_metrics_drawdown_profit_factor_and_bands(self):
        trades = [("2026-01-%02d" % i, r) for i, r in enumerate([1.0, -2.0, 0.5, -1.0, 1.5, 1.0, -0.5, 1.0, 0.5, 1.0, -1.0, 0.5, 1.0, 1.0, -0.5, 1.0, 0.5, 1.0, 1.0, 1.0], 1)]
        m = ex.metrics(trades, recommended=25)
        self.assertAlmostEqual(m["fill_rate"], 20 / 25)
        self.assertAlmostEqual(m["max_drawdown_r"], 2.5)
        self.assertGreater(m["profit_factor"], 1.0)
        lo, hi = m["band"]
        clo, chi = m["band_corrected"]
        self.assertLessEqual(clo, lo)
        self.assertGreaterEqual(chi, hi)
        self.assertIsNone(ex.metrics([("a", 1.0)], 1)["band"])


class Decision(unittest.TestCase):
    def m(self, n, mean, lo=None):
        return dict(trades=n, mean_r=mean, band_corrected=None if lo is None else [lo, mean + 0.2])

    def test_every_criterion_is_enforced(self):
        base_tr, base_te = self.m(300, -0.1, -0.2), self.m(200, -0.1, -0.2)
        self.assertEqual(ex.decide(self.m(300, 0.1, 0.02), self.m(150, 0.2, 0.05), base_tr, base_te)[0], "ADOPTED")
        self.assertEqual(ex.decide(self.m(300, 0.1, 0.02), self.m(99, 0.2, 0.05), base_tr, base_te)[0], "INSUFFICIENT SAMPLE")
        self.assertEqual(ex.decide(self.m(300, 0.1, 0.02), self.m(150, 0.2, -0.01), base_tr, base_te)[0], "REJECTED")        # band touches zero
        self.assertEqual(ex.decide(self.m(300, 0.1, 0.02), self.m(150, -0.05, -0.2), base_tr, base_te)[0], "REJECTED")      # negative
        self.assertEqual(ex.decide(self.m(300, -0.3, -0.4), self.m(150, 0.2, 0.05), base_tr, base_te)[0], "REJECTED")       # worse than baseline in train
        self.assertEqual(ex.decide(self.m(300, 0.1, 0.02), self.m(150, 0.2, 0.05), base_tr, self.m(200, 0.5, 0.3))[0], "REJECTED")  # does not beat baseline
        self.assertEqual(ex.decide(None, None, None, None, is_baseline=True)[0], "BASELINE")

    def test_the_hypothesis_count_and_correction_match_the_registration(self):
        self.assertEqual(ex.N_HYP, 12)
        self.assertAlmostEqual(ex.ALPHA, 0.05 / 12)
        with open(os.path.join(os.path.dirname(__file__), "..", "webui", "docs", "PHASE_3_5_PREREGISTRATION.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("N = 12", text)
        self.assertIn("at least 100", text)


class FullRun(unittest.TestCase):
    def test_a_small_synthetic_run_produces_logged_decisions_and_no_adoption(self):
        rnd = random.Random(9)
        bars, p = [], 100.0
        for i in range(-150, 400):
            o = p
            p = max(50.0, p + rnd.uniform(-1.5, 1.5))
            bars.append((T0 + i * H, max(o, p) + 0.4, min(o, p) - 0.4, p, o))
        by = {"GC=F": {"1h": bars, "4h": gi.resample(bars, 4)}}
        ctxs = la.build_contexts(by, [])
        reps = []
        for n in range(60):
            t = T0 + (n * 5) * H
            kind = "pullback" if n % 2 else "breakout"
            side = "long" if n % 3 else "short"
            px = next(b[3] for b in bars if b[0] >= t)
            risk = 2.0
            e = px + (0.5 if kind == "breakout" else -0.5) * (1 if side == "long" else -1)
            st = e - risk if side == "long" else e + risk
            tg = [e + (1 if side == "long" else -1) * risk * m for m in (1.2, 2.2, 3.5)]
            r = sc.make_record("GC=F", "1h", plan(kind=kind, side=side, entry=e, stop=st, targets=tg, trend=["with", "counter", "range"][n % 3]), t)
            r["result"] = sc.grade(r, bars, NOW)
            r["opportunity"] = "op%d" % n
            reps.append(r)
        d = tempfile.mkdtemp()
        try:
            doc = ex.run(reps, ctxs, by, [], NOW, log_path=os.path.join(d, "log.json"))
            ids = [h["id"] for h in doc["hypotheses"]]
            for need in ("A", "B", "C", "D", "A5", "stop", "retest", "vol", "align", "dist", "candle", "sweep", "news"):
                self.assertIn(need, ids)
            self.assertEqual(len(doc["hypotheses"]), 13)                               # 12 hypotheses plus the two baselines minus the regime test = see the registration
            self.assertTrue(all(h["decision"] in ("BASELINE", "INSUFFICIENT SAMPLE", "REJECTED", "ADOPTED") for h in doc["hypotheses"]))
            self.assertTrue(all(h["decision"] != "ADOPTED" for h in doc["hypotheses"]))     # 60 opportunities can never reach the 100-trade bar
            self.assertIn("DISABLED", doc["breakout_policy"])
            self.assertIn("NO DETECTABLE SIGNAL", doc["regime"]["verdict"]) if not doc["regime"]["has_signal"] else None
            self.assertEqual(len(doc["regime"]["ablation"]), 6)
            import json
            with open(os.path.join(d, "log.json"), encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
