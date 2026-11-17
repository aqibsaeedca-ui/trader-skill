"""Phases 6 to 9: exit simulator, stop and exit variants, grading policy, de-duplication accounting and strict mode. SYNTHETIC data."""
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load

e2 = load("webui", "experiments2")
ex = load("webui", "experiments")
sc = load("webui", "scorecard")
la = load("webui", "loss_attribution")
gi = load("webui", "gold_intraday")

T0 = 1_700_000_000 // 3600 * 3600
H = 3600
NOW = T0 + 500 * H


def bar(i, hi, lo, cl, op=None):
    return (T0 + i * H, hi, lo, cl, cl if op is None else op)


def plan(kind="pullback", side="long", entry=100.0, stop=95.0, targets=(105.0, 110.0, 115.0), **extra):
    d = dict(name="x", side=side, kind=kind, entry=entry, stop=stop, targets=[dict(price=t) for t in targets], rank=1, grade="A", score=7.0, trend="with", weak=False, blend_r=2.0)
    d.update(extra)
    return d


def rec(**kw):
    return sc.make_record("GC=F", "1h", plan(**kw), T0)


def run_exit(r, bars, spec, stop=None, fill=100.0, limit_bar=False, atr=2.0, h1=None):
    return e2.simulate_exit(r, bars, 0, fill, limit_bar, r["stop"] if stop is None else stop, spec, atr, h1)


class Equivalence(unittest.TestCase):
    def test_the_plan_exit_matches_the_live_manager_on_random_paths(self):
        rnd = random.Random(8)
        n = 0
        for case in range(300):
            side = "long" if case % 2 == 0 else "short"
            sgn = 1 if side == "long" else -1
            risk = rnd.choice([2.0, 3.0, 5.0])
            r = sc.make_record("GC=F", "1h", plan(side=side, entry=100.0, stop=100.0 - sgn * risk, targets=[100.0 + sgn * risk * m for m in (1.2, 2.2, 3.5)]), T0)
            p, bars = 100.0, []
            for i in range(45):
                o = p
                p = max(50.0, p + rnd.uniform(-2.5, 2.5))
                bars.append((T0 + (i + 1) * H, max(o, p) + rnd.uniform(0, 1.2), min(o, p) - rnd.uniform(0, 1.2), p, o))
            lb = bool(case % 3 == 0)
            a = ex.manage(r, bars, 0, 100.0, limit_bar=lb)
            b = e2.simulate_exit(r, bars, 0, 100.0, lb, r["stop"], dict(kind="plan"))
            self.assertAlmostEqual(a["r_net"], b["r_net"], places=9, msg="case %d" % case)
            self.assertEqual(a["final"], b["final"])
            n += 1
        self.assertEqual(n, 300)


class Exits(unittest.TestCase):
    def test_single_target_exits_the_whole_position(self):
        r = rec()
        g = run_exit(r, [bar(1, 106.0, 99.5, 105.0)], dict(kind="single", price=105.0))
        self.assertTrue(g["final"])
        self.assertAlmostEqual(g["r_gross"], 1.0, places=6)

    def test_stop_wins_a_same_bar_tie_for_every_exit_kind(self):
        r = rec()
        for spec in (dict(kind="plan"), dict(kind="plan_nobe"), dict(kind="single", price=105.0), dict(kind="time")):
            g = run_exit(r, [bar(1, 106.0, 94.0, 100.0)], spec)
            self.assertAlmostEqual(g["r_gross"], -1.0, places=6, msg=spec["kind"])

    def test_no_breakeven_variant_does_not_move_the_stop(self):
        r = rec()
        bars = [bar(1, 106.0, 100.5, 105.5), bar(2, 106.0, 99.0, 100.0), bar(3, 101.0, 94.0, 95.0)]
        with_be = run_exit(r, bars, dict(kind="plan"))
        no_be = run_exit(r, bars, dict(kind="plan_nobe"))
        self.assertGreater(with_be["r_gross"], no_be["r_gross"])                      # the breakeven stop saved the runner; without it the plan stop was hit

    def test_trailing_stop_activates_after_one_r_and_ratchets(self):
        r = rec()
        bars = [bar(1, 101.0, 99.8, 100.5), bar(2, 106.5, 100.5, 106.0), bar(3, 108.0, 105.5, 107.5), bar(4, 107.5, 100.0, 101.0, op=107.0)]
        g = run_exit(r, bars, dict(kind="trail"), atr=2.0)
        self.assertTrue(g["final"])
        self.assertGreater(g["r_gross"], 0.5)                                         # exits on the trail, well above the entry
        flat = run_exit(r, [bar(1, 101.0, 99.8, 100.5), bar(2, 102.0, 94.5, 95.5)], dict(kind="trail"), atr=2.0)
        self.assertAlmostEqual(flat["r_gross"], -1.0, places=6)                       # never reached +1R, so the original stop applies

    def test_time_exit_closes_at_the_twelfth_bar_close(self):
        r = rec()
        bars = [bar(i, 101.0, 99.5, 100.0 + 0.1 * i) for i in range(1, 20)]
        g = run_exit(r, bars, dict(kind="time"))
        self.assertTrue(g["final"])
        self.assertAlmostEqual(g["r_gross"], (bars[11][3] - 100.0) / 5.0, places=6)

    def test_regime_exit_closes_when_the_h1_bias_turns_against_the_trade(self):
        class FakeH1:
            def snap(self, ts):
                return dict(bias="bearish") if ts >= T0 + 3 * H else dict(bias="bullish")
        r = rec()
        bars = [bar(i, 101.5, 99.8, 100.5) for i in range(1, 8)]
        g = run_exit(r, bars, dict(kind="regime"), h1=FakeH1())
        self.assertTrue(g["final"])
        self.assertEqual(g["why"], "regime turned")

    def test_open_trades_are_not_final_and_mae_is_in_own_r(self):
        r = rec()
        g = run_exit(r, [bar(1, 101.0, 97.0, 99.0)], dict(kind="plan"))
        self.assertFalse(g["final"])
        self.assertAlmostEqual(g["mae"], 3.0 / 5.0, places=6)

    def test_costs_are_charged_in_the_variants_own_r(self):
        r = rec()
        wide = run_exit(r, [bar(1, 106.0, 99.5, 105.0)], dict(kind="single", price=105.0), stop=90.0)
        self.assertAlmostEqual(wide["r_net"], 0.5 - r["cost"] / 10.0, places=6)       # 5 above a 10 wide stop is 0.5R


class Variants(unittest.TestCase):
    def test_stop_variants_build_from_atr_and_the_h1_swing(self):
        r = rec()
        f = dict(tf_atr=2.0, s1=dict(swing_low=96.0, swing_high=104.0, atr=1.5))
        v = e2.stop_variants(r, f)
        self.assertAlmostEqual(v["S1"], 98.0)
        self.assertAlmostEqual(v["S3"], 96.0)
        self.assertAlmostEqual(v["S4"], 96.0 - 0.25 * 1.5)
        self.assertEqual(e2.stop_variants(r, dict(tf_atr=None, s1={}))["S1"], None)
        self.assertEqual(e2.stop_variants(r, dict(tf_atr=2.0, s1=dict(swing_low=101.0, atr=1.5)))["S4"], r["stop"])   # swing on the wrong side: plan stop

    def test_exit_variants_use_the_plan_stop(self):
        r = rec()
        v = e2.exit_variants(r, dict(tf_atr=2.0))
        self.assertAlmostEqual(v["X1"][1]["price"], 107.5)
        self.assertAlmostEqual(v["X2"][1]["price"], 105.0)
        self.assertAlmostEqual(v["X3"][1]["price"], 104.0)
        self.assertEqual(sorted(v), ["X1", "X2", "X3", "X4", "X5", "X6", "X7"])
        self.assertIsNone(e2.exit_variants(r, dict(tf_atr=None))["X3"][1])


class Policy(unittest.TestCase):
    def m(self, n, mean, lo=None, hi=None):
        return dict(trades=n, mean_r=mean, band_corrected=None if lo is None else [lo, hi])

    def test_evidence_grades_follow_the_registration(self):
        self.assertEqual(e2.bucket_grade(self.m(300, 0.1), self.m(150, 0.2, 0.05, 0.4), e2.ALPHA)[0], "A")
        self.assertEqual(e2.bucket_grade(self.m(300, -0.1), self.m(150, 0.2, 0.05, 0.4), e2.ALPHA)[0], "B")             # train not positive
        self.assertEqual(e2.bucket_grade(self.m(300, 0.1), self.m(150, -0.3, -0.5, -0.1), e2.ALPHA)[0], "NO TRADE")
        self.assertEqual(e2.bucket_grade(self.m(300, 0.1), self.m(60, -0.1, -0.4, 0.2), e2.ALPHA)[0], "B")
        self.assertEqual(e2.bucket_grade(self.m(300, 0.1), self.m(10, 0.5, None, None), e2.ALPHA)[0], "C")
        self.assertEqual(e2.train_only_grade(self.m(150, 0.2, 0.05, 0.4), e2.ALPHA), "A")
        self.assertEqual(e2.train_only_grade(self.m(150, 0.2, -0.05, 0.4), e2.ALPHA), "other")

    def test_spearman_extremes(self):
        self.assertAlmostEqual(e2.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(e2.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
        self.assertIsNone(e2.spearman([1, 1, 1], [1, 2, 3]))

    def test_registration_counts_match_the_code(self):
        self.assertEqual(e2.N_TOTAL, 25)
        self.assertAlmostEqual(e2.ALPHA, 0.05 / 25)
        with open(os.path.join(os.path.dirname(__file__), "..", "webui", "docs", "PHASE_6_9_PREREGISTRATION.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("N = 25", text)
        self.assertIn("alone", text)


class Accounting(unittest.TestCase):
    def test_concurrency_and_drawdown(self):
        rows = [dict(fill_time="2026-01-01T00:00:00Z", exit_time="2026-01-01T05:00:00Z", r_net=1.0, cost_r=0.1),
                dict(fill_time="2026-01-01T01:00:00Z", exit_time="2026-01-01T04:00:00Z", r_net=-1.1, cost_r=0.1),
                dict(fill_time="2026-01-01T02:00:00Z", exit_time="2026-01-01T03:00:00Z", r_net=-1.1, cost_r=0.1)]
        a = e2.account(rows)
        self.assertEqual((a["trades"], a["peak_concurrent_r"]), (3, 3))
        self.assertAlmostEqual(a["total_net_r"], -1.2)
        self.assertAlmostEqual(a["total_cost_r"], 0.3)
        self.assertAlmostEqual(a["max_drawdown_r"], 2.2)
        self.assertEqual(e2.account([])["trades"], 0)


class FullRun(unittest.TestCase):
    def test_a_synthetic_run_has_every_section_and_strict_mode_cannot_succeed_without_evidence(self):
        rnd = random.Random(4)
        bars, p = [], 100.0
        for i in range(-150, 400):
            o = p
            p = max(50.0, p + rnd.uniform(-1.5, 1.5))
            bars.append((T0 + i * H, max(o, p) + 0.4, min(o, p) - 0.4, p, o))
        by = {"GC=F": {"1h": bars, "4h": gi.resample(bars, 4)}}
        ctxs = la.build_contexts(by, [])
        reps, allrecs = [], []
        for n in range(70):
            t = T0 + (n * 5) * H
            kind = "pullback" if n % 2 else "breakout"
            side = "long" if n % 3 else "short"
            px = next(b[3] for b in bars if b[0] >= t)
            risk = 2.0
            e = px + (0.5 if kind == "breakout" else -0.5) * (1 if side == "long" else -1)
            st = e - risk if side == "long" else e + risk
            tg = [e + (1 if side == "long" else -1) * risk * m for m in (1.2, 2.2, 3.5)]
            r = sc.make_record("GC=F", "1h", plan(kind=kind, side=side, entry=e, stop=st, targets=tg, trend=["with", "counter", "range"][n % 3], score=float(n % 8)), t)
            r["result"] = sc.grade(r, bars, NOW)
            r["opportunity"] = "op%d" % n
            reps.append(r)
            g = r["result"]
            allrecs.append(dict(id=r["id"], opportunity=r["opportunity"], created_ts=t, kind=kind, side=side, state=g["state"], fill_time=g.get("fill_time"), exit_time=g.get("exit_time"),
                                r_net=g.get("r_net"), cost_r=(r["cost"] / g["risk"]) if g.get("risk") else None, final=g.get("final")))
        d = tempfile.mkdtemp()
        try:
            doc = e2.run(reps, ctxs, by, [], allrecs, NOW, log_path=os.path.join(d, "log.json"))
            ids = [h["id"] for h in doc["hypotheses"]]
            for need in ("BASE6", "S1", "S2", "S3", "S4", "X1", "X2", "X3", "X4", "X5", "X6", "X7", "H7", "H9"):
                self.assertIn(need, ids)
            self.assertEqual(len([h for h in doc["hypotheses"] if h["decision"] not in ("BASELINE",)]), 13)
            self.assertTrue(all(h["decision"] != "ADOPTED" for h in doc["hypotheses"]))
            self.assertEqual(len(doc["strategies"]), 4)
            self.assertTrue(all(s["probability"] == "INSUFFICIENT DATA" for s in doc["strategies"]))
            self.assertEqual(doc["strict_vs_baseline"]["verdict"], "NOT DEMONSTRATED")
            self.assertEqual(doc["strict_vs_baseline"]["strict"]["trades"], 0)
            self.assertIn("every_recommendation", doc["dedup"])
            self.assertLessEqual(doc["dedup"]["first_of_each_opportunity"]["trades"], doc["dedup"]["every_recommendation"]["trades"])
            with open(os.path.join(d, "log.json"), encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1)
            e2.append_log(doc, os.path.join(d, "log.json"))                                 # same data again: not a new evaluation
            with open(os.path.join(d, "log.json"), encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
