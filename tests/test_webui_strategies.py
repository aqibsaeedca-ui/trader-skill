"""The four strategies: rules, rejections, validity and causality. All price series are SYNTHETIC."""
import random
import unittest

from _load import load

st = load("webui", "strategies")
rg = load("webui", "regimes")
bt = load("webui", "backtester")


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def flat(n, price=100.0, rng=2.0, t0=1_700_006_400, step=3600):
    return [bar(t0 + i * step, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


def walk(n, seed=3, start=100.0, vol=1.0, step=3600, t0=1_700_006_400):
    rnd = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        out.append(bar(t0 + i * step, o, max(o, c) + abs(rnd.gauss(0, vol / 3)), min(o, c) - abs(rnd.gauss(0, vol / 3)), c))
        p = c
    return out


def zigzag(points, per_leg=6, start=100.0, t0=1_700_006_400, step=3600):
    out, p, t = [], start, t0
    for target in points:
        for s in range(1, per_leg + 1):
            nxt = p + (target - p) * s / per_leg
            o = p + (target - p) * (s - 1) / per_leg
            out.append(bar(t, o, max(o, nxt) + 0.1, min(o, nxt) - 0.1, nxt))
            t += step
        p = target
    return out


def drive(strategy, bars, htf=None, cfg=None):
    """Feed bars one at a time exactly as the backtester does. htf: dict or callable(i) -> dict."""
    A = rg.Analysis(bars, cfg)
    strategy.reset()
    sigs = []
    for i in range(len(bars)):
        A.step(i)
        h = htf(i) if callable(htf) else htf
        sigs.extend(strategy.on_bar(A, i, h))
    return sigs


class Breakout(unittest.TestCase):
    def base(self):
        return flat(25)

    def test_breakout_then_retest_gives_one_long(self):
        bars = self.base()
        t = bars[-1][0]
        bars.append(bar(t + 3600, 100.5, 103.2, 100.4, 103.0))             # closes through the 101 range high
        bars.append(bar(t + 7200, 101.6, 102.6, 101.3, 102.4))             # pulls back to 101, holds, bullish
        sigs = drive(st.BreakoutRetest(), bars)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s["side"], s["decided_i"], s["kind"]), ("long", len(bars) - 1, "market"))
        self.assertLess(s["stop"], 101)
        self.assertEqual(s["targets"], sorted(s["targets"]))
        self.assertGreater(s["targets"][0], s["price"])

    def test_a_close_back_inside_is_a_false_breakout_and_cancels(self):
        bars = self.base()
        t = bars[-1][0]
        bars.append(bar(t + 3600, 100.5, 103.2, 100.4, 103.0))
        bars.append(bar(t + 7200, 103.0, 103.1, 100.2, 100.6))             # back below 101 on the next bar
        bars.append(bar(t + 10800, 101.6, 102.6, 101.3, 102.4))            # would have been a retest
        s = st.BreakoutRetest()
        sigs = drive(s, bars)
        self.assertEqual(sigs, [])
        self.assertTrue(any("false breakout" in r["reason"] for r in s.rejected))
        self.assertEqual(s.false_breakouts, 1)

    def test_no_signal_without_a_validated_consolidation(self):
        bars = walk(60, vol=2.0, seed=4)
        t = bars[-1][0]
        c = bars[-1][3]
        bars.append(bar(t + 3600, c, c + 8, c - 0.1, c + 7.5))
        self.assertEqual(drive(st.BreakoutRetest(), bars), [])

    def test_retest_that_never_comes_expires(self):
        bars = self.base()
        t = bars[-1][0]
        bars.append(bar(t + 3600, 100.5, 103.2, 100.4, 103.0))
        for k in range(2, 14):
            bars.append(bar(t + k * 3600, 103.2, 104.0, 103.0, 103.6))     # keeps going, never retests
        s = st.BreakoutRetest()
        self.assertEqual(drive(s, bars), [])
        self.assertTrue(any("no retest" in r["reason"] for r in s.rejected))

    def test_costs_can_veto_a_signal(self):
        bars = self.base()
        t = bars[-1][0]
        bars.append(bar(t + 3600, 100.5, 103.2, 100.4, 103.0))
        bars.append(bar(t + 7200, 101.6, 102.6, 101.3, 102.4))
        s = st.BreakoutRetest()
        s.reset()
        A = rg.Analysis(bars)
        out = []
        s.cost_per_unit = 5.0                                              # huge compared with the risk
        for i in range(len(bars)):
            A.step(i)
            out += s.on_bar(A, i)
        self.assertEqual(out, [])
        self.assertTrue(any("costs take" in r["reason"] for r in s.rejected))


class Trend(unittest.TestCase):
    def bars(self):
        warm = flat(20, rng=1.0)
        zz = zigzag([90, 110, 100], per_leg=6, t0=warm[-1][0] + 3600)
        trig = bar(zz[-1][0] + 3600, 100.0, 102.5, 99.95, 102.0)
        return warm + zz + [trig]

    def test_pullback_in_an_uptrend_triggers_a_long(self):
        bars = self.bars()
        sigs = drive(st.TrendContinuation(), bars, htf=dict(trend="strong_bull", bias="bullish"))
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s["side"], s["decided_i"]), ("long", len(bars) - 1))
        self.assertLess(s["stop"], 99.95)
        self.assertGreater(s["meta"]["depth"], 0.3)
        self.assertLess(s["meta"]["depth"], 0.7)

    def test_no_higher_timeframe_context_means_no_signal(self):
        self.assertEqual(drive(st.TrendContinuation(), self.bars(), htf=None), [])

    def test_wrong_higher_timeframe_direction_blocks_it(self):
        self.assertEqual(drive(st.TrendContinuation(), self.bars(), htf=dict(trend="strong_bear", bias="bearish")), [])

    def test_a_pullback_that_is_too_deep_is_ignored(self):
        warm = flat(20, rng=1.0)
        zz = zigzag([90, 110, 92], per_leg=6, t0=warm[-1][0] + 3600)       # gives back about 90% of the leg
        bars = warm + zz + [bar(zz[-1][0] + 3600, 92.0, 94.5, 91.95, 94.0)]
        self.assertEqual(drive(st.TrendContinuation(), bars, htf=dict(trend="strong_bull", bias="bullish")), [])

    def test_one_signal_per_leg(self):
        bars = self.bars()
        t = bars[-1][0]
        bars.append(bar(t + 3600, 102.0, 104.0, 101.5, 103.8))
        sigs = drive(st.TrendContinuation(), bars, htf=dict(trend="strong_bull", bias="bullish"))
        self.assertEqual(len(sigs), 1)


class Sweep(unittest.TestCase):
    def bars(self, shift=True, reclaim_close=101.5):
        bars = flat(30, rng=3.0)
        bars[20] = bar(bars[20][0], 100, 105, 99, 100)                     # swing high 105
        bars[26] = bar(bars[26][0], 100, 101.5, 98, 99)                    # swing low 98
        t = bars[-1][0]
        bars.append(bar(t + 3600, 100, 105.6, 99, reclaim_close))          # sweeps 105 and closes back inside
        if shift:
            bars.append(bar(t + 7200, 101, 101.2, 97, 97.5))               # closes below the swing low: structure shift
        else:
            bars.append(bar(t + 7200, 101, 102, 100.5, 101.2))
        return bars

    def test_sweep_plus_shift_gives_a_short(self):
        bars = self.bars()
        sigs = drive(st.LiquiditySweepReversal(max_risk_atr=5), bars)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s["side"], s["decided_i"]), ("short", len(bars) - 1))
        self.assertGreater(s["stop"], 105.6)
        self.assertEqual(s["targets"], sorted(s["targets"], reverse=True))

    def test_a_sweep_without_a_shift_is_rejected(self):
        bars = self.bars(shift=False)
        t = bars[-1][0]
        for k in range(1, 8):
            bars.append(bar(t + k * 3600, 101, 102, 100.5, 101.2))
        s = st.LiquiditySweepReversal(max_risk_atr=5)
        self.assertEqual(drive(s, bars), [])
        self.assertTrue(any("without a structure shift" in r["reason"] for r in s.rejected))

    def test_weak_reclaim_close_is_rejected(self):
        s = st.LiquiditySweepReversal(max_risk_atr=5)
        self.assertEqual(drive(s, self.bars(reclaim_close=104.5)), [])
        self.assertTrue(any("weak reclaim" in r["reason"] for r in s.rejected))

    def test_reversal_against_a_strong_higher_timeframe_trend_is_rejected(self):
        s = st.LiquiditySweepReversal(max_risk_atr=5)
        self.assertEqual(drive(s, self.bars(), htf=dict(trend="strong_bull", bias="bullish")), [])
        self.assertTrue(any("against a strong higher-timeframe" in r["reason"] for r in s.rejected))
        s2 = st.LiquiditySweepReversal(max_risk_atr=5, allow_countertrend=True)
        self.assertEqual(len(drive(s2, self.bars(), htf=dict(trend="strong_bull", bias="bullish"))), 1)

    def test_a_stop_that_is_too_wide_is_rejected(self):
        s = st.LiquiditySweepReversal(max_risk_atr=1.0)
        self.assertEqual(drive(s, self.bars()), [])
        self.assertTrue(any("stop distance" in r["reason"] for r in s.rejected))


class Range(unittest.TestCase):
    def test_bounds_come_from_the_distribution_of_highs_and_lows(self):
        bars = [bar(i, 100, 100 + (i % 10), 90 - (i % 5), 100) for i in range(60)]
        upper, lower = st.range_bounds(bars, 59, 40)
        highs = sorted(b[1] for b in bars[20:60])
        lows = sorted(b[2] for b in bars[20:60])
        self.assertAlmostEqual(upper, st._quantile(highs, 0.9))
        self.assertAlmostEqual(lower, st._quantile(lows, 0.1))

    def test_quantile(self):
        self.assertEqual(st._quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(st._quantile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertAlmostEqual(st._quantile([1, 2, 3, 4], 0.5), 2.5)

    def oscillating(self, n=140, amp=2.5):
        import math
        out = []
        for i in range(n):
            c = 100 + amp * math.sin(i / 2.2)
            o = 100 + amp * math.sin((i - 1) / 2.2)
            out.append(bar(1_700_006_400 + i * 3600, o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        return out

    def test_signals_are_valid_and_inside_the_range(self):
        sigs = drive(st.RangeTrading(), self.oscillating())
        for s in sigs:
            self.assertEqual(s["kind"], "market")
            if s["side"] == "long":
                self.assertLess(s["stop"], s["price"])
                self.assertTrue(all(t > s["price"] for t in s["targets"]))
                self.assertLess(s["stop"], s["meta"]["lower"])
            else:
                self.assertGreater(s["stop"], s["price"])
                self.assertTrue(all(t < s["price"] for t in s["targets"]))
                self.assertGreater(s["stop"], s["meta"]["upper"])

    def test_a_trend_never_gets_range_signals(self):
        bars = [bar(1_700_006_400 + i * 3600, 100 + i, 100.6 + i, 99.4 + i, 100.8 + i) for i in range(150)]
        self.assertEqual(drive(st.RangeTrading(), bars), [])

    def test_a_break_out_of_the_range_switches_it_off(self):
        bars = self.oscillating(100)
        c = bars[-1][3]
        t = bars[-1][0]
        bars.append(bar(t + 3600, c, c + 12, c - 0.2, c + 11))
        s = st.RangeTrading()
        drive(s, bars)
        self.assertTrue(any("invalidated" in r["reason"] for r in s.rejected))


class Causality(unittest.TestCase):
    def check(self, name, params=None, htf_sec=None):
        bars = walk(900, seed=17, vol=1.2)
        full = bt.generate(bars, [st.make(name, **(params or {}))], htf_sec=htf_sec)[0]
        full = list(full.values())[0]
        for n in (200, 450, 700):
            part = bt.generate(bars[:n], [st.make(name, **(params or {}))], htf_sec=htf_sec)[0]
            part = list(part.values())[0]
            self.assertEqual(part, [s for s in full if s["decided_i"] < n], "%s at %d" % (name, n))

    def test_breakout(self):
        self.check("breakout_retest", dict(n=12))

    def test_trend(self):
        self.check("trend_continuation", htf_sec=4 * 3600)

    def test_sweep(self):
        self.check("liquidity_sweep")

    def test_range(self):
        self.check("range")

    def test_signals_are_structurally_valid_on_random_data(self):
        bars = walk(900, seed=23, vol=1.2)
        for name in st.STRATEGIES:
            inst = st.make(name)
            sigs = bt.generate(bars, [inst], htf_sec=4 * 3600)[0][id(inst)]
            for s in sigs:
                sg = 1 if s["side"] == "long" else -1
                self.assertGreater((s["price"] - s["stop"]) * sg, 0)
                self.assertTrue(all((t - s["price"]) * sg > 0 for t in s["targets"]))
                self.assertEqual(s["targets"], sorted(s["targets"], reverse=s["side"] == "short"))
                self.assertTrue(s["id"].startswith(name))
                self.assertIn(s["kind"], ("market", "limit", "stop"))

    def test_signal_ids_are_deterministic_and_unique(self):
        bars = walk(700, seed=29, vol=1.2)
        inst = st.make("liquidity_sweep")
        a = bt.generate(bars, [inst])[0][id(inst)]
        inst2 = st.make("liquidity_sweep")
        b = bt.generate(bars, [inst2])[0][id(inst2)]
        self.assertEqual([s["id"] for s in a], [s["id"] for s in b])
        self.assertEqual(len({s["id"] for s in a}), len(a))


class HigherTimeframe(unittest.TestCase):
    def test_a_block_is_visible_only_after_it_has_closed(self):
        bars = walk(200, seed=5)
        H = rg.HTF(bars, 4 * 3600)
        for i in range(len(bars)):
            snap = H.at(i)
            if snap is not None:
                block_start = snap["t"] // (4 * 3600) * (4 * 3600)
                self.assertLessEqual(block_start + 4 * 3600, bars[i][0] + 3600, "bar %d" % i)

    def test_first_block_has_no_context(self):
        bars = walk(50)
        H = rg.HTF(bars, 4 * 3600)
        self.assertIsNone(H.at(0))

    def test_htf_snapshot_is_not_changed_by_later_bars(self):
        bars = walk(300, seed=8)
        full = rg.HTF(bars, 4 * 3600)
        part = rg.HTF(bars[:150], 4 * 3600)
        for i in range(150):
            self.assertEqual(part.at(i), full.at(i))


class Registry(unittest.TestCase):
    def test_unknown_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            st.make("range", nonsense=1)
        with self.assertRaises(ValueError):
            st.make("nope")

    def test_grids_are_small_and_cover_the_defaults_keys(self):
        for name, cls in st.STRATEGIES.items():
            g = st.param_grid(name)
            self.assertLessEqual(len(g), 16)
            for k in cls.grid:
                self.assertIn(k, cls.defaults)

    def test_every_strategy_can_be_switched_off_by_not_running_it(self):
        self.assertEqual(set(st.STRATEGIES), {"breakout_retest", "trend_continuation", "liquidity_sweep", "range"})


if __name__ == "__main__":
    unittest.main()
