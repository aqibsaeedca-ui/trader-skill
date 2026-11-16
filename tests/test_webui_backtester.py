"""Execution model, statistics, walk-forward, holdout lock and the validation gate. All data SYNTHETIC."""
import math
import os
import random
import shutil
import tempfile
import unittest

from _load import load

bt = load("webui", "backtester")
st = load("webui", "strategies")


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def sig(side="long", kind="market", price=100.0, stop=98.0, targets=(102.0, 104.0, 106.0), i=0, expires=1, atr=1.0, t=None):
    return dict(id="x-%d-%s" % (i, side), strategy="x", side=side, kind=kind, price=price, stop=stop, targets=list(targets),
                expires=expires, decided_i=i, decided_t=1_700_000_000 + i * 60 if t is None else t, regime="sideways",
                session="london", atr=atr, risk=abs(price - stop), note="", invalidation="", meta={})


def bars_from(rows, t0=1_700_000_000):
    return [bar(t0 + i * 60, *r) for i, r in enumerate(rows)]      # rows are (o, h, l, c)


FREE = bt.costs()
CFG = dict(max_hold=20)


class Fills(unittest.TestCase):
    def run1(self, rows, s, c=FREE, cfg=None):
        trades, rej = bt.execute(bars_from(rows), [s], c, dict(CFG, **(cfg or {})))
        return trades, rej

    def test_market_long_hits_all_three_targets(self):
        rows = [(100, 100.4, 99.6, 100), (100, 102.1, 99.9, 102), (102, 104.2, 101, 104), (104, 106.5, 103, 106), (106, 106, 106, 106)]
        (t,), _ = self.run1(rows, sig())
        self.assertAlmostEqual(t["entry"], 100.0)
        self.assertAlmostEqual(t["risk"], 2.0)
        self.assertAlmostEqual(t["r"], 0.5 * 1 + 0.3 * 2 + 0.2 * 3)        # 0.5R, 0.6R, 0.6R
        self.assertEqual([e["how"] for e in t["exits"]], ["target 1", "target 2", "target 3"])

    def test_stop_wins_when_stop_and_target_share_a_bar(self):
        rows = [(100, 100.4, 99.6, 100), (100, 106, 97, 101), (101, 101, 101, 101)]
        (t,), _ = self.run1(rows, sig())
        self.assertAlmostEqual(t["r"], -1.0)
        self.assertEqual(t["exits"][0]["how"], "stop")

    def test_a_gap_through_the_stop_exits_at_the_open(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.5, 99.8, 100), (96, 97, 95, 96)]
        (t,), _ = self.run1(rows, sig())
        self.assertAlmostEqual(t["exits"][0]["price"], 96.0)
        self.assertAlmostEqual(t["r"], -2.0)

    def test_stop_moves_to_breakeven_after_the_first_target(self):
        rows = [(100, 100.4, 99.6, 100), (100, 102.2, 99.9, 101), (101, 101, 99.5, 100), (100, 100, 100, 100)]
        (t,), _ = self.run1(rows, sig())
        self.assertAlmostEqual(t["r"], 0.5, places=6)                       # half closed at +1R, rest out at entry
        self.assertEqual([e["how"] for e in t["exits"]], ["target 1", "breakeven"])

    def test_breakeven_can_be_switched_off(self):
        rows = [(100, 100.4, 99.6, 100), (100, 102.2, 99.9, 101), (101, 101, 97.5, 98), (98, 98, 98, 98)]
        (t,), _ = self.run1(rows, sig(), cfg=dict(be_after_t1=False))
        self.assertLess(t["r"], 0.1)                                         # full stop on the rest: 0.5 - 0.5 = 0

    def test_costs_move_the_fill_and_shrink_the_result(self):
        rows = [(100, 100.4, 99.6, 100), (100, 102.1, 99.9, 102), (102, 104.2, 101, 104), (104, 106.5, 103, 106), (106, 106, 106, 106)]
        (free,), _ = self.run1(rows, sig())
        (paid,), _ = self.run1(rows, sig(), c=bt.costs(spread=1.0, slippage=0.5))
        self.assertAlmostEqual(paid["entry"], 101.0)                         # open + half spread + slippage
        self.assertAlmostEqual(paid["risk"], 3.0)
        self.assertLess(paid["r"], free["r"])

    def test_commission_comes_off_in_r(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.5, 97, 98), (98, 98, 98, 98)]
        (a,), _ = self.run1(rows, sig())
        (b,), _ = self.run1(rows, sig(), c=bt.costs(commission_per_unit=0.2))
        self.assertAlmostEqual(a["r"] - b["r"], 0.2 / 2.0)

    def test_limit_needs_price_to_trade_through_not_touch(self):
        s = sig(kind="limit", price=100.0, stop=97.0, expires=2)
        touch = [(102, 102.5, 101.5, 102), (101, 101.5, 100.0, 101), (101, 101, 101, 101)]
        _, rej = self.run1(touch, s)
        self.assertTrue(any("not filled" in r["reason"] for r in rej))
        through = [(102, 102.5, 101.5, 102), (101, 101.5, 99.4, 101), (101, 101, 101, 101)]
        (t,), _ = self.run1(through, s)
        self.assertAlmostEqual(t["entry"], 100.0)

    def test_limit_is_cancelled_if_price_closes_beyond_the_stop_first(self):
        s = sig(kind="limit", price=100.0, stop=97.0, expires=3)
        rows = [(102, 102.5, 101.5, 102), (99, 99.5, 96, 96.5), (96, 96, 96, 96)]
        _, rej = self.run1(rows, s)
        self.assertTrue(any("cancelled" in r["reason"] for r in rej))

    def test_stop_entry_fills_at_the_worse_of_open_and_level_plus_slippage(self):
        s = sig(kind="stop", price=101.0, stop=98.0, expires=2)
        rows = [(100, 100.4, 99.6, 100), (100, 101.5, 99.9, 101.2), (101.2, 101.2, 101.2, 101.2)]
        (t,), _ = self.run1(rows, s, c=bt.costs(slippage=0.25))
        self.assertAlmostEqual(t["entry"], 101.25)
        gap = [(100, 100.4, 99.6, 100), (102, 103, 101.8, 102.5), (102.5, 102.5, 102.5, 102.5)]
        (g,), _ = self.run1(gap, s)
        self.assertAlmostEqual(g["entry"], 102.0)                            # gapped past the level: filled at the open

    def test_short_market_mirrors_the_long(self):
        s = sig(side="short", price=100.0, stop=102.0, targets=(98.0, 96.0, 94.0))
        rows = [(100, 100.4, 99.6, 100), (100, 100.1, 97.9, 98), (98, 98.2, 95.9, 96), (96, 96.2, 93.8, 94), (94, 94, 94, 94)]
        (t,), _ = self.run1(rows, s)
        self.assertAlmostEqual(t["r"], 0.5 * 1 + 0.3 * 2 + 0.2 * 3)
        paid = bt.costs(spread=1.0)
        (p,), _ = self.run1(rows, s, c=paid)
        self.assertAlmostEqual(p["entry"], 99.5)                             # sells at the bid, half a spread below
        self.assertLess(p["r"], t["r"] + 1e-9)

    def test_short_target_needs_the_ask_to_reach_it(self):
        s = sig(side="short", price=100.0, stop=102.0, targets=(98.0,))
        rows = [(100, 100.4, 99.6, 100), (100, 100.1, 97.8, 98.5), (98.5, 98.6, 98.5, 98.5)]
        (a,), _ = self.run1(rows, s, c=bt.costs(spread=0.0))
        self.assertEqual(a["exits"][0]["how"], "target 1")
        (b,), _ = self.run1(rows, s, c=bt.costs(spread=0.5))                 # low 97.8 + 0.5 spread is above 98: not hit
        self.assertNotEqual(b["exits"][0]["how"], "target 1")

    def test_time_exit(self):
        rows = [(100, 100.4, 99.6, 100)] + [(100, 100.5, 99.5, 100.1)] * 6
        (t,), _ = self.run1(rows, sig(), cfg=dict(max_hold=3))
        self.assertEqual(t["exits"][-1]["how"], "time")
        self.assertEqual(t["bars_held"], 3)

    def test_excursions_are_measured_in_r(self):
        rows = [(100, 100.4, 99.6, 100), (100, 101.0, 98.9, 100.5), (100.5, 100.8, 100.2, 100.5), (100, 106.5, 99, 106)]
        (t,), _ = self.run1(rows, sig(), cfg=dict(be_after_t1=False))
        self.assertAlmostEqual(t["mae_r"], (100 - 98.9) / 2.0)
        self.assertGreater(t["mfe_r"], 3)

    def test_a_stop_too_close_to_the_fill_is_refused(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.5, 99.5, 100)]
        _, rej = self.run1(rows, sig(stop=99.9, atr=1.0))
        self.assertTrue(any("too close" in r["reason"] for r in rej))

    def test_no_next_bar_means_no_fill(self):
        trades, rej = self.run1([(100, 100.4, 99.6, 100)], sig())
        self.assertEqual(trades, [])
        self.assertTrue(rej)

    def test_one_position_at_a_time(self):
        rows = [(100, 100.4, 99.6, 100)] * 2 + [(100, 100.4, 99.6, 100)] * 6
        a, b = sig(i=0), sig(i=2)
        trades, rej = bt.execute(bars_from(rows), [a, b], FREE, dict(max_hold=20))
        self.assertEqual(len(trades), 1)
        self.assertTrue(any("already open" in r["reason"] for r in rej))

    def test_daily_loss_limit_blocks_new_entries(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.5, 97, 98), (98, 98.4, 97.6, 98), (98, 98.4, 97.6, 98),
                (100, 100.4, 99.6, 100), (100, 100.4, 99.6, 100), (100, 100.4, 99.6, 100)]
        a, b = sig(i=0), sig(i=3, price=100, stop=98)
        trades, rej = bt.execute(bars_from(rows), [a, b], FREE, dict(max_hold=20, daily_loss_limit_r=1.0))
        self.assertEqual(len(trades), 1)
        self.assertTrue(any("daily loss limit" in r["reason"] for r in rej))
        trades2, _ = bt.execute(bars_from(rows), [a, b], FREE, dict(max_hold=20, daily_loss_limit_r=None))
        self.assertEqual(len(trades2), 2)

    def test_weights_renormalise_when_fewer_targets_remain(self):
        rows = [(100, 100.4, 99.6, 100), (100, 102.1, 99.9, 102), (102, 102, 102, 102)]
        (t,), _ = self.run1(rows, sig(targets=(102.0,)), cfg=dict(be_after_t1=True))
        self.assertAlmostEqual(sum(e["weight"] for e in t["exits"]), 1.0)

    def test_negative_costs_rejected(self):
        with self.assertRaises(ValueError):
            bt.costs(spread=-1)

    def test_scaled_and_round_trip(self):
        c = bt.costs(0.4, 0.1, 0.05)
        self.assertAlmostEqual(bt.round_trip(c), 0.4 + 0.2 + 0.05)
        self.assertAlmostEqual(bt.round_trip(bt.scaled(c, 2)), 2 * bt.round_trip(c))


class Statistics(unittest.TestCase):
    def trades(self, rs):
        return [dict(r=r, entry_t=1_700_000_000 + i * 3600, mae_r=0.5, mfe_r=1.0, bars_held=3, cost_r=0.05) for i, r in enumerate(rs)]

    def test_wilson_interval(self):
        lo, hi = bt.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)
        self.assertEqual(bt.wilson(0, 0), (None, None))

    def test_summary_numbers(self):
        s = bt.summary(self.trades([1, -1, 1, -1, 2]))
        self.assertEqual(s["n"], 5)
        self.assertAlmostEqual(s["expectancy"], 0.4)
        self.assertAlmostEqual(s["win_rate"], 0.6)
        self.assertAlmostEqual(s["profit_factor"], 2.0)
        self.assertAlmostEqual(s["max_dd_r"], 1.0)
        self.assertEqual(s["max_consec_losses"], 1)
        self.assertAlmostEqual(s["avg_win"], 4 / 3.0)
        self.assertAlmostEqual(s["avg_loss"], -1.0)
        self.assertIsNone(s["sharpe"])                                       # too few trades to mean anything

    def test_drawdown_and_streaks(self):
        s = bt.summary(self.trades([1, 1, -1, -1, -1, 2, -1]))
        self.assertAlmostEqual(s["max_dd_r"], 3.0)
        self.assertEqual(s["max_consec_losses"], 3)
        self.assertGreater(s["max_dd_pct"], 0)

    def test_sharpe_only_with_enough_trades(self):
        rnd = random.Random(1)
        s = bt.summary(self.trades([rnd.gauss(0.1, 1) for _ in range(60)]))
        self.assertIsNotNone(s["sharpe"])
        self.assertIsNotNone(s["sortino"])

    def test_empty(self):
        self.assertEqual(bt.summary([]), {"n": 0})

    def test_segments(self):
        tr = self.trades([1, -1, 1, 1])
        for t, side in zip(tr, ("long", "short", "long", "long")):
            t["side"] = side
        seg = bt.segment(tr, lambda t: t["side"])
        self.assertEqual(seg["long"]["n"], 3)
        self.assertEqual(seg["short"]["n"], 1)

    def test_bootstrap_is_reproducible_and_brackets_the_mean(self):
        rnd = random.Random(5)
        rs = [rnd.gauss(0.2, 1) for _ in range(200)]
        a = bt.block_bootstrap(rs, 400, seed=3)
        b = bt.block_bootstrap(rs, 400, seed=3)
        c = bt.block_bootstrap(rs, 400, seed=4)
        self.assertEqual(a, b)
        self.assertNotEqual(a["lo"], c["lo"])
        self.assertLess(a["lo"], sum(rs) / 200)
        self.assertGreater(a["hi"], sum(rs) / 200)
        self.assertIsNone(bt.block_bootstrap([1, 2], 100))

    def test_inverse_normal(self):
        self.assertAlmostEqual(bt._phi_inv(0.975), 1.959964, places=4)
        self.assertAlmostEqual(bt._phi_inv(0.5), 0.0, places=8)
        for p in (0.001, 0.2, 0.9, 0.999):
            self.assertAlmostEqual(bt._phi(bt._phi_inv(p)), p, places=7)

    def test_deflated_sharpe_falls_with_more_trials(self):
        rnd = random.Random(2)
        rs = [rnd.gauss(0.15, 1) for _ in range(300)]
        a = bt.deflated_sharpe(rs, 1)["dsr"]
        b = bt.deflated_sharpe(rs, 20)["dsr"]
        c = bt.deflated_sharpe(rs, 500)["dsr"]
        self.assertGreater(a, b)
        self.assertGreater(b, c)

    def test_deflated_sharpe_needs_data_and_variation(self):
        self.assertIsNone(bt.deflated_sharpe([1, 2, 3], 5))
        self.assertIsNone(bt.deflated_sharpe([0.5] * 50, 5))

    def test_pure_noise_does_not_look_skilled(self):
        rnd = random.Random(9)
        rs = [rnd.gauss(0, 1) for _ in range(400)]
        self.assertLess(bt.deflated_sharpe(rs, 24)["dsr"], 0.95)

    def test_holm(self):
        self.assertEqual([round(x, 6) for x in bt.holm([0.01, 0.04, 0.03])], [0.03, 0.06, 0.06])
        self.assertEqual(bt.holm([0.6, 0.9]), [1.0, 0.9])
        self.assertTrue(all(0 <= x <= 1 for x in bt.holm([0.5, 0.7, 0.9])))
        self.assertEqual(bt.holm([0.001]), [0.001])


class Gate(unittest.TestCase):
    def good(self):
        return dict(oos=dict(n=150, expectancy=0.2), bootstrap=dict(lo=0.05), dsr=dict(dsr=0.97),
                    halves=[0.1, 0.3], holdout=dict(attempts=1, summary=dict(n=40, expectancy=0.1)))

    STRESS = [dict(multiple=1.0, expectancy=0.2), dict(multiple=2.0, expectancy=0.05)]

    def test_all_conditions_met(self):
        v = bt.verdict(self.good(), self.STRESS)
        self.assertTrue(v["status"].startswith("VALIDATED"))
        self.assertIn("past data only", v["status"])
        self.assertTrue(all(v["checks"].values()))

    def test_too_few_trades_is_insufficient_not_failed(self):
        r = self.good()
        r["oos"] = dict(n=40, expectancy=0.5)
        self.assertEqual(bt.verdict(r, self.STRESS)["status"], "INSUFFICIENT SAMPLE")

    def test_each_condition_can_fail_the_gate(self):
        cases = {
            "bootstrap": lambda r: r.update(bootstrap=dict(lo=-0.01)),
            "expectancy": lambda r: r["oos"].update(expectancy=-0.1),
            "dsr": lambda r: r.update(dsr=dict(dsr=0.5)),
            "halves": lambda r: r.update(halves=[0.3, -0.1]),
            "holdout size": lambda r: r["holdout"]["summary"].update(n=10),
            "holdout sign": lambda r: r["holdout"]["summary"].update(expectancy=-0.2),
            "holdout reuse": lambda r: r["holdout"].update(attempts=3),
        }
        for name, mutate in cases.items():
            r = self.good()
            mutate(r)
            v = bt.verdict(r, self.STRESS)
            self.assertEqual(v["status"], "NOT VALIDATED", name)
            self.assertTrue(v["reasons"], name)

    def test_cost_stress_can_fail_the_gate(self):
        v = bt.verdict(self.good(), [dict(multiple=2.0, expectancy=-0.02)])
        self.assertEqual(v["status"], "NOT VALIDATED")
        self.assertTrue(any("costs" in r for r in v["reasons"]))

    def test_missing_holdout_cannot_validate(self):
        r = self.good()
        r["holdout"] = None
        self.assertEqual(bt.verdict(r, self.STRESS)["status"], "NOT VALIDATED")

    def test_wording_never_says_profitable(self):
        for r in (self.good(),):
            v = bt.verdict(r, self.STRESS)
            self.assertNotIn("profitable", (v["status"] + " ".join(v["reasons"])).lower())


class HoldoutLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "log.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_same_parameters_count_once(self):
        self.assertEqual(bt.holdout_register("k", {"a": 1}, self.path), 1)
        self.assertEqual(bt.holdout_register("k", {"a": 1}, self.path), 1)

    def test_a_second_parameter_set_contaminates_the_holdout(self):
        bt.holdout_register("k", {"a": 1}, self.path)
        self.assertEqual(bt.holdout_register("k", {"a": 2}, self.path), 2)

    def test_segments_are_independent(self):
        bt.holdout_register("k1", {"a": 1}, self.path)
        self.assertEqual(bt.holdout_register("k2", {"a": 2}, self.path), 1)


def noise(n, seed=1, vol=1.0, step=3600, t0=1_700_006_400):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        out.append(bar(t0 + i * step, o, max(o, c) + abs(rnd.gauss(0, vol / 3)), min(o, c) - abs(rnd.gauss(0, vol / 3)), c))
        p = c
    return out


class WalkForward(unittest.TestCase):
    WF = dict(train_min=400, test_len=200, holdout_frac=0.2, min_train_trades=3, warmup=60)

    def run_wf(self, name="range", seed=1, store=None, bars=None):
        bars = bars or noise(1500, seed=seed)
        return bars, bt.walk_forward(bars, name, bt.costs(spread=0.2, slippage=0.05), dict(max_hold=20), self.WF,
                                     holdout_store=store, symbol="TEST", interval="1h")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = os.path.join(self.dir, "h.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_folds_are_chronological_and_do_not_overlap(self):
        bars, r = self.run_wf(store=self.store)
        prev = None
        for f in r["folds"]:
            self.assertGreaterEqual(f["start"], self.WF["train_min"])
            if prev is not None:
                self.assertEqual(f["start"], prev["end"])
            self.assertLessEqual(f["end"], r["split"])
            prev = f

    def test_out_of_sample_trades_sit_inside_test_windows_only(self):
        bars, r = self.run_wf(store=self.store)
        windows = [(f["start"], f["end"]) for f in r["folds"] if f.get("chosen") is not None]
        for t in r["oos_trades"]:
            self.assertTrue(any(a <= t["decided_i"] < b for a, b in windows))
            self.assertLess(t["decided_i"], r["split"])

    def test_the_holdout_is_never_in_the_walk_forward(self):
        bars, r = self.run_wf(store=self.store)
        self.assertTrue(all(t["decided_i"] < r["split"] for t in r["oos_trades"]))
        if r["holdout"]:
            self.assertEqual(r["holdout"]["start_t"], bars[r["split"]][0])

    def test_the_embargo_defaults_to_the_holding_period(self):
        bars, r = self.run_wf(store=self.store)
        self.assertEqual(r["embargo"], 20)

    def test_results_are_reproducible(self):
        bars = noise(1500, seed=4)
        _, a = self.run_wf(bars=bars, store=os.path.join(self.dir, "a.json"))
        _, b = self.run_wf(bars=bars, store=os.path.join(self.dir, "b.json"))
        self.assertEqual(a["oos"], b["oos"])
        self.assertEqual(a["bootstrap"], b["bootstrap"])
        self.assertEqual([f.get("chosen") for f in a["folds"]], [f.get("chosen") for f in b["folds"]])

    def test_second_run_with_the_same_parameters_is_not_a_second_look(self):
        bars = noise(1500, seed=6)
        _, a = self.run_wf(bars=bars, store=self.store)
        _, b = self.run_wf(bars=bars, store=self.store)
        if a["holdout"]:
            self.assertEqual(b["holdout"]["attempts"], 1)

    def test_noise_is_not_validated(self):
        for name in st.STRATEGIES:
            bars = noise(1500, seed=11)
            r = bt.walk_forward(bars, name, bt.costs(spread=0.2, slippage=0.05), dict(max_hold=20), self.WF,
                                htf_sec=4 * 3600, holdout_store=os.path.join(self.dir, name + ".json"), symbol="N", interval="1h")
            v = bt.verdict(r, [dict(multiple=2.0, expectancy=-0.1)])
            self.assertFalse(v["status"].startswith("VALIDATED"), name)

    def test_grid_size_counts_as_trials(self):
        bars, r = self.run_wf(store=self.store)
        self.assertEqual(r["grid_size"], len(st.param_grid("range")))
        self.assertEqual(r["n_trials"], r["grid_size"])

    def test_too_little_data_gives_no_folds(self):
        bars = noise(300, seed=2)
        r = bt.walk_forward(bars, "range", bt.costs(), dict(max_hold=20), self.WF, holdout_store=self.store)
        self.assertEqual(r["folds"], [])
        self.assertEqual(r["oos"], {"n": 0})
        self.assertEqual(bt.verdict(r, [])["status"], "INSUFFICIENT SAMPLE")


class StressAndRecord(unittest.TestCase):
    def test_higher_costs_never_improve_expectancy(self):
        bars = noise(1500, seed=3)
        rows = bt.stress(bars, "range", {}, bt.costs(spread=0.2, slippage=0.05), (0.0, 1.0, 2.0, 4.0), dict(max_hold=20))
        ex = [r["expectancy"] for r in rows if r["expectancy"] is not None]
        self.assertEqual(ex, sorted(ex, reverse=True))

    def test_run_record_is_enough_to_reproduce(self):
        bars = noise(300)
        rec = bt.run_record(bars, "range", "TEST", "1h", bt.costs(spread=0.1), dict(bt.EXEC_DEFAULTS), dict(bt.WF_DEFAULTS), dict(bt.GATE_DEFAULTS), 7)
        for k in ("seed", "costs", "dataset", "code_version", "python", "strategy", "walk_forward", "gate"):
            self.assertIn(k, rec)
        self.assertEqual(rec["dataset"]["sha256"], bt.dataset_hash(bars))
        self.assertEqual(len(rec["code_version"]), 12)

    def test_dataset_hash_changes_with_the_data(self):
        a = noise(100)
        b = list(a)
        b[5] = (b[5][0], b[5][1] + 0.01, b[5][2], b[5][3], b[5][4])
        self.assertNotEqual(bt.dataset_hash(a), bt.dataset_hash(b))

    def test_run_strategy_end_to_end(self):
        bars = noise(900, seed=8)
        r = bt.run_strategy(bars, "liquidity_sweep", None, bt.costs(spread=0.1), dict(max_hold=20))
        self.assertEqual(len(r["signals"]), len(r["trades"]) + len([x for x in r["rejected"] if "id" in x]))


if __name__ == "__main__":
    unittest.main()
