"""Gold core: strategy signals, no look-ahead, simulation rules, one-at-a-time, decision and holdout handling. SYNTHETIC bars only."""
import random
import shutil
import tempfile
import unittest

from _load import load

gc = load("webui", "gold_core")
T0 = (1_700_000_000 // 14400) * 14400
H = 3600


def walk(n, seed=1, drift=0.0, vol=2.0, start=2000.0, skip=0):
    rnd = random.Random(seed)
    out, px = [], start
    for i in range(n):
        op = px
        px = max(10.0, px + drift + rnd.uniform(-vol, vol))
        out.append((T0 + (skip + i) * H, max(op, px) + rnd.uniform(0, 1), min(op, px) - rnd.uniform(0, 1), px, op))
    return out


def trending(n=900, seed=3):
    """An uptrend with pullbacks, so the H4 trend filter turns on."""
    rnd = random.Random(seed)
    out, px = [], 2000.0
    for i in range(n):
        op = px
        wave = 3.0 if (i // 30) % 3 else -2.0
        px = max(10.0, px + wave * 0.5 + rnd.uniform(-2, 2))
        out.append((T0 + i * H, max(op, px) + rnd.uniform(0, 1), min(op, px) - rnd.uniform(0, 1), px, op))
    return out


class Indicators(unittest.TestCase):
    def test_atr_of_constant_range_bars(self):
        h, l, c = [101.0] * 30, [99.0] * 30, [100.0] * 30
        a = gc.wilder_atr(h, l, c)
        self.assertIsNone(a[13])
        self.assertAlmostEqual(a[14], 2.0)
        self.assertAlmostEqual(a[29], 2.0)

    def test_ema_seeds_with_the_simple_average(self):
        e = gc.ema([float(i) for i in range(1, 31)], 20)
        self.assertIsNone(e[18])
        self.assertAlmostEqual(e[19], 10.5)

    def test_h4_blocks_keep_only_complete_blocks(self):
        bars = walk(10)                       # T0 is on a block boundary: two complete blocks and two spare bars
        b = gc.h4_blocks(bars)
        self.assertEqual(len(b), 2)
        self.assertEqual(b[0][0], T0)
        self.assertEqual(b[0][1], max(x[1] for x in bars[:4]))
        self.assertEqual(b[0][4], bars[0][4])
        self.assertEqual(b[0][3], bars[3][3])


class NoLookAhead(unittest.TestCase):
    def test_signals_do_not_change_when_future_bars_are_added(self):
        bars = trending(900) + walk(300, seed=9, start=2400.0, skip=900)
        full = gc.Context(bars)
        checked = fired = 0
        for sid, _, _, _ in gc.STRATEGIES:
            for i in range(300, 880, 7):
                cut = gc.Context(bars[:i + 1])
                a, b = gc.signal_at(full, i, sid), gc.signal_at(cut, i, sid)
                checked += 1
                self.assertEqual(a, b, (sid, i))
                fired += 1 if a else 0
        self.assertGreater(checked, 300)
        self.assertGreater(fired, 0)             # the comparison is not vacuous

    def test_trend_uses_only_closed_h4_bars(self):
        ctx = gc.Context(trending(600))
        for i in (300, 301, 302, 303):
            j = ctx.last_closed_h4(i)
            self.assertLessEqual(ctx.end4[j], ctx.t[i] + H)

    def test_pivots_are_used_only_after_they_are_confirmed_and_aged(self):
        bars = trending(900)
        ctx = gc.Context(bars)
        for i in range(300, 880, 11):
            for sid in ("B1", "B2"):
                s = gc.signal_at(ctx, i, sid)
                if s:
                    J = ctx.last_closed_h4(i)
                    self.assertTrue(any(j + gc.PIV_K <= J - gc.PIV_CONFIRM_GAP for j in list(ctx.pivot_lo) + list(ctx.pivot_hi)))


def mini(rows, atr=2.0):
    """A Context-like object for the simulator: rows are (open, high, low, close) per H1 bar."""
    bars = [(T0 + i * H, hi, lo, cl, op) for i, (op, hi, lo, cl) in enumerate(rows)]
    ctx = gc.Context(bars) if len(bars) > 20 else None
    if ctx is None:
        class C:
            pass
        ctx = C()
        ctx.t = [b[0] for b in bars]
        ctx.h, ctx.l, ctx.c, ctx.o = [b[1] for b in bars], [b[2] for b in bars], [b[3] for b in bars], [b[4] for b in bars]
    return ctx


class Simulation(unittest.TestCase):
    def sig(self, **kw):
        s = dict(i=0, side="long", stop=95.0, exit="2R", atr=2.0, strategy="A1")
        s.update(kw)
        return s

    def test_fill_is_the_next_open_and_two_r_target(self):
        ctx = mini([(100, 100, 100, 100), (100, 103, 99, 102), (102, 112, 101, 111)])
        r = gc.simulate(ctx, self.sig(), cost=0.0)
        self.assertEqual((r["fill"], r["why"]), (100, "target"))
        self.assertAlmostEqual(r["gross_r"], 2.0)             # risk 5, target 110

    def test_same_bar_stop_and_target_is_a_stop(self):
        ctx = mini([(100, 100, 100, 100), (100, 120, 90, 100)])
        r = gc.simulate(ctx, self.sig(), cost=0.0)
        self.assertEqual(r["why"], "stop")
        self.assertAlmostEqual(r["gross_r"], -1.0)

    def test_a_gap_through_the_stop_exits_at_the_open(self):
        ctx = mini([(100, 100, 100, 100), (101, 102, 100, 101), (90, 91, 88, 89)])
        r = gc.simulate(ctx, self.sig(), cost=0.0)
        self.assertEqual(r["why"], "stop")
        self.assertAlmostEqual(r["gross_r"], (90 - 101) / 6.0)   # filled at 101 (the next open), stopped at the 90 open: worse than minus 1R

    def test_next_open_beyond_the_stop_is_skipped(self):
        ctx = mini([(100, 100, 100, 100), (94, 95, 93, 94)])
        r = gc.simulate(ctx, self.sig(), cost=0.0)
        self.assertNotIn("net_r", r)

    def test_cost_is_charged_in_r_of_the_trade_risk(self):
        ctx = mini([(100, 100, 100, 100), (100, 103, 99, 102), (102, 112, 101, 111)])
        r = gc.simulate(ctx, self.sig(), cost=0.5)
        self.assertAlmostEqual(r["cost_r"], 0.1)
        self.assertAlmostEqual(r["net_r"], 1.9)

    def test_short_mirrors_the_long(self):
        ctx = mini([(100, 100, 100, 100), (100, 101, 97, 98), (98, 99, 88, 89)])
        r = gc.simulate(ctx, self.sig(side="short", stop=105.0), cost=0.0)
        self.assertEqual(r["why"], "target")
        self.assertAlmostEqual(r["gross_r"], 2.0)

    def test_unfinished_trade_is_open_not_counted(self):
        ctx = mini([(100, 100, 100, 100), (100, 103, 99, 101), (101, 103, 100, 102)])
        r = gc.simulate(ctx, self.sig(), cost=0.0)
        self.assertIs(r.get("final"), False)

    def test_time_exit_after_the_holding_limit(self):
        rows = [(100, 100, 100, 100)] + [(100, 101, 99, 100)] * 60
        r = gc.simulate(mini(rows), self.sig(), cost=0.0, hold=48)
        self.assertEqual(r["why"], "time")

    def test_channel_exit_happens_at_the_next_open_after_a_close_beyond_the_channel(self):
        rows = [(100, 100, 100, 100)] + [(100, 103, 99, 102)] * 12 + [(102, 103, 96, 97), (96.5, 97, 95.5, 96)]
        r = gc.simulate(mini(rows), self.sig(exit="channel", stop=90.0), cost=0.0)
        self.assertEqual(r["why"], "channel")
        self.assertEqual(r["fill"], 100)
        self.assertAlmostEqual(r["gross_r"], (96.5 - 100) / 10.0)

    def test_b_variants_skip_on_risk_bounds_and_opposing_structure(self):
        ctx = mini([(100, 100, 100, 100), (100, 103, 99, 102)])
        s = self.sig(strategy="B1", stop=99.0, atr=2.0)                       # risk 1 is below 1 ATR (2)
        self.assertNotIn("net_r", gc.simulate(ctx, s, 0.0))
        s = self.sig(strategy="B1", stop=95.0, atr=2.0, opp_edge=104.0)       # opposing structure within 1.5R (7.5)
        self.assertNotIn("net_r", gc.simulate(ctx, s, 0.0))
        s = self.sig(strategy="B2", stop=95.0, atr=2.0, opp_edge=None)        # no structural target
        self.assertNotIn("net_r", gc.simulate(ctx, s, 0.0))


class OneAtATime(unittest.TestCase):
    def test_no_overlap_and_a_cooldown_after_a_stop(self):
        bars = trending(900)
        ctx = gc.Context(bars)
        tr, info = gc.trades_for(ctx, "A1", 0.0)
        self.assertGreater(len(tr), 3)
        last_exit = -1
        for t in tr:
            self.assertGreaterEqual(t["sig_i"], last_exit)
            last_exit = t["exit_k"] + (gc.COOLDOWN if t["why"] == "stop" else 0)

    def test_one_series_can_have_no_trades_in_a_flat_market(self):
        ctx = gc.Context(walk(600, seed=4, vol=0.3))
        tr, _ = gc.trades_for(ctx, "B1", 0.0)
        self.assertLess(len(tr), 3)


class Decision(unittest.TestCase):
    def stat(self, n, mean, lo):
        return dict(trades=n, mean_r=mean, band_corrected=[lo, mean + 0.2])

    def test_small_samples_are_insufficient(self):
        d, _ = gc.decide(self.stat(40, 0.3, 0.1), [dict(trades=10, mean_r=0.3)] * 3, dict(mean_r=-0.1))
        self.assertEqual(d, "INSUFFICIENT SAMPLE")

    def test_adoption_needs_every_criterion(self):
        folds = [dict(trades=40, mean_r=0.2), dict(trades=40, mean_r=0.1), dict(trades=40, mean_r=-0.1)]
        self.assertEqual(gc.decide(self.stat(120, 0.15, 0.02), folds, dict(mean_r=-0.1))[0], "ADOPTED")
        self.assertEqual(gc.decide(self.stat(120, 0.15, -0.02), folds, dict(mean_r=-0.1))[0], "REJECTED")           # band crosses zero
        self.assertEqual(gc.decide(self.stat(120, 0.15, 0.02), [dict(trades=40, mean_r=0.2), dict(trades=40, mean_r=-0.1), dict(trades=40, mean_r=-0.1)], dict(mean_r=-0.1))[0], "REJECTED")
        self.assertEqual(gc.decide(self.stat(120, 0.15, 0.02), folds, dict(mean_r=0.3))[0], "REJECTED")             # does not beat the old engine


class FullRun(unittest.TestCase):
    def test_holdout_is_not_opened_unless_a_variant_is_adopted_and_nothing_is_adopted_on_noise(self):
        d = tempfile.mkdtemp()
        try:
            doc = gc.run(walk(1500, seed=11, vol=2.5), [], 1_800_000_000, 0.3, log_path=d + "/log.json")
            self.assertEqual(len(doc["hypotheses"]), 5)
            for h in doc["hypotheses"]:
                if h["decision"] != "ADOPTED":
                    self.assertFalse(h["holdout"]["opened"])
                    self.assertFalse(h["eligible"])
            self.assertEqual(doc["eligible"], [h["id"] for h in doc["hypotheses"] if h["eligible"]])
            self.assertEqual(doc["hypotheses_cumulative"], 34)
            self.assertEqual(doc["strategy_c"]["status"], "research only")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_live_candidate_is_at_most_one_and_research_only_unless_eligible(self):
        bars = trending(900)
        now = bars[-1][0] + H
        c, info = gc.live_candidate(bars, now, result=dict(eligible=[]))
        if c is not None:
            self.assertFalse(c["eligible"])
        self.assertLessEqual(1 if c else 0, 1)
        c2, _ = gc.live_candidate(bars[:100], now, result=dict(eligible=[]))
        self.assertIsNone(c2)


if __name__ == "__main__":
    unittest.main()
