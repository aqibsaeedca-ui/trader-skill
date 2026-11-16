"""Indicators, regimes and market structure: definitions, confirmation timing and no repainting.
All price series are SYNTHETIC (seeded random walks or hand-built)."""
import math
import random
import unittest

from _load import load

ind = load("webui", "indicators")
rg = load("webui", "regimes")


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def walk(n, seed=3, start=100.0, vol=1.0, step=3600, t0=1_700_006_400):
    rnd = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol / 3))
        l = min(o, c) - abs(rnd.gauss(0, vol / 3))
        out.append(bar(t0 + i * step, o, h, l, c))
        p = c
    return out


def trend(n, slope=0.8, noise=0.15, seed=5, step=3600):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        base = 100 + slope * i
        o = base + rnd.uniform(-noise, noise)
        c = base + slope + rnd.uniform(-noise, noise)
        out.append(bar(1_700_006_400 + i * step, o, max(o, c) + 0.2, min(o, c) - 0.2, c))
    return out


def chop(n, amp=2.0, seed=9, step=3600):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        c = 100 + amp * math.sin(i / 2.0) + rnd.uniform(-0.2, 0.2)
        o = 100 + amp * math.sin((i - 1) / 2.0)
        out.append(bar(1_700_006_400 + i * step, o, max(o, c) + 0.2, min(o, c) - 0.2, c))
    return out


class Indicators(unittest.TestCase):
    def test_prefix_invariance(self):
        bars = walk(300)
        fns = [lambda b: ind.atr(b, 14), lambda b: ind.adx(b, 14)[0], lambda b: ind.adx(b, 14)[1],
               lambda b: ind.efficiency_ratio(b, 20), lambda b: ind.slope(b, 20), lambda b: ind.realised_vol(b, 20),
               lambda b: ind.range_zscore(b, 20), lambda b: ind.true_ranges(b)]
        for fn in fns:
            full = fn(bars)
            for n in (60, 150, 299):
                self.assertEqual(fn(bars[:n]), full[:n])

    def test_efficiency_and_slope_on_a_straight_line(self):
        bars = [bar(i, 100 + i, 100.5 + i, 99.5 + i, 100 + i) for i in range(60)]
        self.assertAlmostEqual(ind.efficiency_ratio(bars, 20)[-1], 1.0)
        self.assertAlmostEqual(ind.slope(bars, 20)[-1], 1.0)

    def test_adx_high_in_a_trend_low_in_chop(self):
        self.assertGreater(ind.adx(trend(120), 14)[0][-1], 40)
        self.assertLess(ind.adx(chop(160), 14)[0][-1], 25)

    def test_di_direction(self):
        a, p, q = ind.adx(trend(120), 14)
        self.assertGreater(p[-1], q[-1])
        down = [bar(b[0], b[4], 200 - (b[1] - 100), 200 - (b[2] - 100), 200 - (b[3] - 100)) for b in trend(120)]
        down = [bar(t, o, max(o, c) + 0.2, min(o, c) - 0.2, c) for (t, h, l, c, o) in down]
        a, p, q = ind.adx(down, 14)
        self.assertGreater(q[-1], p[-1])

    def test_warmup_is_none(self):
        bars = walk(80)
        self.assertIsNone(ind.atr(bars, 14)[13])
        self.assertIsNotNone(ind.atr(bars, 14)[14])
        self.assertIsNone(ind.adx(bars, 14)[0][26])
        self.assertIsNotNone(ind.adx(bars, 14)[0][27])

    def test_range_zscore_flags_a_spike(self):
        bars = walk(60, vol=0.5)
        t, o, h, l, c = bars[-1][0], bars[-1][4], bars[-1][1], bars[-1][2], bars[-1][3]
        bars[-1] = bar(t, o, h + 10, l - 10, c)
        self.assertGreater(ind.range_zscore(bars, 20)[-1], 5)

    def test_ema_seed_and_skip(self):
        out = ind.ema([None, 1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:3], [None, None, None])
        self.assertAlmostEqual(out[3], 2.0)


class NoRepainting(unittest.TestCase):
    def test_snapshot_and_events_never_change(self):
        bars = walk(420, seed=11)
        full = rg.analyse(bars)
        for n in (80, 133, 250, 419):
            part = rg.analyse(bars[:n])
            self.assertEqual(part.snaps[-1], full.snaps[n - 1], "snapshot at %d" % n)
            self.assertEqual(part.events, [e for e in full.events if e["i"] < n], "events up to %d" % n)

    def test_every_snapshot_matches_an_incremental_run(self):
        bars = walk(200, seed=2)
        full = rg.analyse(bars)
        inc = rg.Analysis(bars)
        for i in range(len(bars)):
            self.assertEqual(inc.step(i), full.snaps[i])

    def test_steps_must_be_in_order(self):
        a = rg.Analysis(walk(50))
        with self.assertRaises(ValueError):
            a.step(3)

    def test_events_are_never_dated_before_their_confirmation(self):
        a = rg.analyse(walk(300, seed=21))
        for e in a.events:
            if e["type"] == "swing":
                self.assertEqual(e["i"], e["swing_i"] + rg.DEFAULT_CFG["k"])


def zigzag(points, per_leg=4, start=100.0, t0=1_700_006_400, step=3600):
    """Bars that travel between the given turning prices."""
    out, p, t = [], start, t0
    for target in points:
        for s in range(1, per_leg + 1):
            nxt = p + (target - p) * s / per_leg
            o = p + (target - p) * (s - 1) / per_leg
            out.append(bar(t, o, max(o, nxt) + 0.1, min(o, nxt) - 0.1, nxt))
            t += step
        p = target
    return out


class Structure(unittest.TestCase):
    def test_swing_is_confirmed_exactly_k_bars_late(self):
        bars = zigzag([110, 100], per_leg=5)
        a = rg.analyse(bars, dict(k=3))
        sw = [e for e in a.events if e["type"] == "swing" and e["kind"] == "high"]
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw[0]["i"], sw[0]["swing_i"] + 3)
        self.assertTrue(all(e["i"] >= 3 for e in a.events))

    def test_higher_highs_and_higher_lows_make_a_bullish_bias(self):
        a = rg.analyse(zigzag([110, 104, 116, 108, 122, 114, 128, 120], per_leg=5), dict(k=3))
        labels = [e["label"] for e in a.events if e["type"] == "swing"]
        self.assertIn("HH", labels)
        self.assertIn("HL", labels)
        self.assertEqual(a.bias, "bullish")

    def test_lower_lows_make_a_bearish_bias(self):
        a = rg.analyse(zigzag([90, 96, 84, 92, 78, 86, 72, 80], per_leg=5, start=100), dict(k=3))
        self.assertEqual(a.bias, "bearish")

    def test_bos_with_the_bias_and_choch_against_it(self):
        up = zigzag([110, 104, 116, 108, 122, 114, 128], per_leg=5)
        crash = zigzag([95], per_leg=3, start=128, t0=up[-1][0] + 3600)
        a = rg.analyse(up + crash, dict(k=3))
        kinds = [(e["type"], e["dir"]) for e in a.events if e["type"] in ("bos", "choch")]
        self.assertIn(("bos", "bull"), kinds)                      # new highs in a bullish structure
        self.assertIn(("choch", "bear"), kinds)                    # close below a higher low

    def test_a_wick_through_a_swing_is_not_a_bos(self):
        base = zigzag([110, 100, 109], per_leg=5)
        t = base[-1][0] + 3600
        wick = bar(t, 109, 112, 108.5, 109.5)                     # trades above 110 but closes below
        a = rg.analyse(base + [wick, bar(t + 3600, 109.5, 110, 109, 109.5)], dict(k=3))
        self.assertFalse([e for e in a.events if e["type"] in ("bos", "choch") and e["dir"] == "bull"])

    def test_bos_is_not_repeated_for_the_same_level(self):
        bars = zigzag([110, 100, 115, 120, 125], per_leg=5)
        a = rg.analyse(bars, dict(k=3))
        bos = [e for e in a.events if e["type"] in ("bos", "choch") and e["dir"] == "bull"]
        self.assertEqual(len({e["level"] for e in bos}), len(bos))


def flat(n, price=100.0, rng=2.0, t0=1_700_006_400, step=3600):
    return [bar(t0 + i * step, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


def swing_sweeps(analysis, level=None):
    """Sweeps of confirmed swings only (session and day levels also exist on hourly bars)."""
    return [e for e in analysis.events if e["type"] == "sweep" and e["source"] in ("swing high", "swing low")
            and (level is None or e["level"] == level)]


class Sweeps(unittest.TestCase):
    def setup_level(self):
        bars = flat(30)
        bars[20] = bar(bars[20][0], 100, 104, 99, 101)          # a clear swing high at 104 (confirmed 3 bars later)
        return bars

    def tail(self, bars, items):
        t = bars[-1][0]
        return bars + [bar(t + (n + 1) * 3600, *spec) for n, spec in enumerate(items)]

    def test_wick_sweep_confirms_on_the_same_close(self):
        bars = self.tail(self.setup_level(), [(100, 105.5, 99.5, 103)])
        a = rg.analyse(bars, dict(k=3))
        sw = swing_sweeps(a)
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw[0]["reversal"], "bear")
        self.assertEqual(sw[0]["i"], len(bars) - 1)

    def test_no_sweep_while_price_is_still_beyond_the_level(self):
        bars = self.tail(self.setup_level(), [(100, 105.5, 99.5, 105.0), (105, 106, 104.5, 105.5)])
        a = rg.analyse(bars, dict(k=3, sweep_reclaim_bars=3))
        self.assertFalse(swing_sweeps(a))

    def older_swing(self):
        """104 is an older swing high; a later, lower swing high at 102 is now the last one."""
        bars = self.setup_level()
        bars[25] = bar(bars[25][0], 100, 102, 99.5, 100.5)
        return bars

    def test_reclaim_within_the_window_confirms_on_the_reclaim_bar(self):
        bars = self.tail(self.older_swing(), [(100, 105.5, 99.5, 105.0), (105, 106, 104.5, 105.5), (105.5, 105.8, 102, 103)])
        a = rg.analyse(bars, dict(k=3, sweep_reclaim_bars=3))
        sw = swing_sweeps(a, 104)
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw[0]["i"], len(bars) - 1)
        self.assertGreaterEqual(sw[0]["extreme"], 106)

    def test_late_reclaim_is_acceptance_not_a_sweep(self):
        items = [(100, 105.5, 99.5, 105.0)] + [(105, 106, 104.5, 105.5)] * 4 + [(105.5, 105.8, 102, 103)]
        a = rg.analyse(self.tail(self.older_swing(), items), dict(k=3, sweep_reclaim_bars=3))
        self.assertFalse(swing_sweeps(a, 104))

    def test_a_close_beyond_the_last_swing_is_a_bos_not_a_sweep(self):
        bars = self.tail(self.setup_level(), [(100, 105.5, 99.5, 105.0), (105.5, 105.8, 102, 103)])
        a = rg.analyse(bars, dict(k=3, sweep_reclaim_bars=3))
        self.assertFalse(swing_sweeps(a))
        self.assertTrue([e for e in a.events if e["type"] in ("bos", "choch") and e["dir"] == "bull" and e["level"] == 104])

    def test_a_tiny_breach_is_not_a_sweep(self):
        bars = self.tail(self.setup_level(), [(100, 104.05, 99.5, 103)])      # 0.05 above, less than 0.1 ATR
        self.assertFalse(swing_sweeps(rg.analyse(bars, dict(k=3))))

    def test_sweep_of_lows_is_bullish(self):
        bars = flat(30)
        bars[20] = bar(bars[20][0], 100, 101, 96, 99)
        bars = self.tail(bars, [(100, 100.5, 94.5, 98)])
        sw = swing_sweeps(rg.analyse(bars, dict(k=3)))
        self.assertEqual([e["reversal"] for e in sw], ["bull"])

    def test_a_level_can_only_be_swept_once(self):
        bars = self.tail(self.setup_level(), [(100, 105.5, 99.5, 103), (100, 105.6, 99.5, 103)])
        self.assertEqual(len(swing_sweeps(rg.analyse(bars, dict(k=3)), 104)), 1)


class Gaps(unittest.TestCase):
    def test_bullish_fvg_is_confirmed_on_the_third_bar_and_filled_later(self):
        bars = flat(25)
        t = bars[-1][0]
        bars += [bar(t + 3600, 100, 101, 99.5, 100.8), bar(t + 7200, 100.8, 106, 100.7, 105.5), bar(t + 10800, 105.5, 108, 104, 107)]
        a = rg.analyse(bars)
        fv = [e for e in a.events if e["type"] == "fvg"]
        self.assertEqual(len(fv), 1)
        self.assertEqual((fv[0]["dir"], fv[0]["i"], fv[0]["lo"], fv[0]["hi"]), ("bull", len(bars) - 1, 101, 104))
        bars += [bar(t + 14400, 107, 107.5, 100.5, 101)]
        filled = [e for e in rg.analyse(bars).events if e["type"] == "fvg_filled"]
        self.assertEqual(len(filled), 1)

    def test_small_gaps_are_ignored(self):
        bars = flat(25)
        t = bars[-1][0]
        bars += [bar(t + 3600, 100, 100.6, 99.8, 100.5), bar(t + 7200, 100.5, 101.2, 100.4, 101), bar(t + 10800, 101, 101.3, 100.7, 101.1)]
        self.assertFalse([e for e in rg.analyse(bars).events if e["type"] == "fvg"])

    def test_bearish_fvg(self):
        bars = flat(25)
        t = bars[-1][0]
        bars += [bar(t + 3600, 100, 100.5, 99, 99.2), bar(t + 7200, 99.2, 99.3, 94, 94.5), bar(t + 10800, 94.5, 96, 92, 93)]
        fv = [e for e in rg.analyse(bars).events if e["type"] == "fvg"]
        self.assertEqual([e["dir"] for e in fv], ["bear"])


class OrderBlocks(unittest.TestCase):
    def test_last_opposite_candle_before_a_displacement_that_breaks_structure(self):
        warm = flat(20)
        up_then_down = warm + zigzag([112, 104], per_leg=5, t0=warm[-1][0] + 3600)
        t = up_then_down[-1][0]
        bars = up_then_down + [bar(t + 3600, 104, 104.4, 103.2, 103.4),        # bearish candle (the block)
                               bar(t + 7200, 103.4, 120, 103.3, 119.5)]         # displacement through 112
        a = rg.analyse(bars, dict(k=3))
        obs = [e for e in a.events if e["type"] == "ob"]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["dir"], "bull")
        self.assertEqual(obs[0]["candle_i"], len(bars) - 2)
        self.assertEqual(obs[0]["i"], len(bars) - 1)

    def test_a_small_break_is_not_an_order_block(self):
        warm = flat(20)
        bars = warm + zigzag([110, 104, 110.6], per_leg=5, t0=warm[-1][0] + 3600)
        self.assertFalse([e for e in rg.analyse(bars, dict(k=3)).events if e["type"] == "ob"])


class Calendar(unittest.TestCase):
    def test_previous_day_levels_exist_only_after_the_day_completes(self):
        day = 86400
        t0 = (1_700_006_400 // day) * day
        bars = [bar(t0 + h * 3600, 100, 101 + h * 0.1, 99, 100) for h in range(24)]
        bars += [bar(t0 + day + h * 3600, 100, 101, 99, 100) for h in range(3)]
        a = rg.Analysis(bars)
        for i in range(24):
            a.step(i)
            self.assertNotIn("prev_day_high", a.fixed_levels, "bar %d" % i)
        a.step(24)
        self.assertAlmostEqual(a.fixed_levels["prev_day_high"]["price"], 101 + 23 * 0.1)
        self.assertEqual(a.fixed_levels["prev_day_low"]["price"], 99)

    def test_session_levels_use_only_completed_sessions(self):
        day = 86400
        t0 = (1_700_006_400 // day) * day
        bars = [bar(t0 + h * 3600, 100, 100 + h, 99, 100) for h in range(10)]         # asia 0-6, london from 7
        a = rg.Analysis(bars)
        for i in range(7):
            a.step(i)
        self.assertNotIn("sess_asia_high", a.fixed_levels)
        a.step(7)
        self.assertEqual(a.fixed_levels["sess_asia_high"]["price"], 106)

    def test_sessions_are_skipped_on_daily_bars(self):
        bars = [bar(1_700_006_400 + i * 86400, 100, 101, 99, 100) for i in range(5)]
        a = rg.analyse(bars)
        self.assertFalse([k for k in a.fixed_levels if k.startswith("sess_")])

    def test_week_key_starts_on_monday(self):
        mon = 4 * 86400
        self.assertEqual(rg.week_key(mon), rg.week_key(mon + 6 * 86400))
        self.assertEqual(rg.week_key(mon - 1) + 1, rg.week_key(mon))

    def test_session_names(self):
        self.assertEqual(rg.session_of(3 * 3600), "asia")
        self.assertEqual(rg.session_of(9 * 3600), "london")
        self.assertEqual(rg.session_of(15 * 3600), "ny")
        self.assertEqual(rg.session_of(22 * 3600), "off")


class Regimes(unittest.TestCase):
    def test_a_steady_uptrend_is_strong_bull(self):
        a = rg.analyse(trend(160))
        self.assertEqual(a.snaps[-1]["label"], "strong_bull")
        self.assertEqual(a.snaps[-1]["trend"], "strong_bull")

    def test_a_steady_downtrend_is_strong_bear(self):
        t = trend(160, slope=-0.8)
        self.assertEqual(rg.analyse(t).snaps[-1]["trend"], "strong_bear")

    def test_chop_is_sideways(self):
        labels = {s["trend"] for s in rg.analyse(chop(200)).snaps[80:]}
        self.assertIn("sideways", labels)
        self.assertNotIn("strong_bull", labels)

    def test_warmup_is_unknown_not_guessed(self):
        self.assertEqual(rg.analyse(walk(100)).snaps[5]["label"], "unknown")

    def test_priority_post_news_then_sweep_then_volatility(self):
        f = dict(adx=30, pdi=30, mdi=10, er=0.5, slope_atr=0.2, vol_ratio=2.0)
        c = rg.DEFAULT_CFG
        self.assertEqual(rg.classify(f, c, post_news=True)["label"], "post_news")
        weak = dict(f, adx=20, er=0.25, slope_atr=0.02)
        self.assertEqual(rg.classify(weak, c, recent_sweep=True)["label"], "sweep_reversal")
        self.assertEqual(rg.classify(weak, c)["label"], "vol_expansion")
        self.assertEqual(rg.classify(f, c)["label"], "strong_bull")             # a strong trend keeps its name
        comp = dict(adx=10, pdi=10, mdi=10, er=0.1, slope_atr=0.0, vol_ratio=0.5)
        self.assertEqual(rg.classify(comp, c)["label"], "vol_compression")
        calm = dict(comp, vol_ratio=1.0)
        self.assertEqual(rg.classify(calm, c)["label"], "sideways")

    def test_weak_trend_keeps_its_direction(self):
        f = dict(adx=20, pdi=12, mdi=18, er=0.3, slope_atr=-0.02, vol_ratio=1.0)
        r = rg.classify(f, rg.DEFAULT_CFG)
        self.assertEqual((r["label"], r["trend"]), ("weak_trend", "weak_bear"))

    def test_post_news_flag_uses_release_times_known_in_advance(self):
        bars = flat(10)
        ev = [bars[4][0] + 100]                                  # a release inside bar 4
        flags = rg.post_news_flags(bars, ev, 2, 3600)
        self.assertEqual(flags, [False, False, False, False, True, True, True, False, False, False])

    def test_news_flag_reaches_the_snapshot(self):
        bars = walk(120)
        a = rg.analyse(bars, event_times=[bars[100][0]])
        self.assertEqual(a.snaps[101]["label"], "post_news")
        self.assertNotEqual(a.snaps[90]["label"], "post_news")

    def test_range_anomaly_event(self):
        bars = walk(80, vol=0.4)
        t, o, h, l, c = bars[60][0], bars[60][4], bars[60][1], bars[60][2], bars[60][3]
        bars[60] = bar(t, o, o + 6, o - 6, c)
        ev = [e for e in rg.analyse(bars).events if e["type"] == "anomaly"]
        self.assertTrue(any(e["i"] == 60 and "range proxy" in e["basis"] for e in ev))

    def test_diagnostics_shape(self):
        d = rg.regime_diagnostics(chop(300), horizon=5)
        self.assertTrue(d)
        for v in d.values():
            self.assertGreater(v["n"], 0)
            self.assertGreaterEqual(v["mean_abs_move_atr"], 0)
        up = rg.regime_diagnostics(trend(300), horizon=5)
        self.assertGreater(up["strong_bull"]["mean_directional_move_atr"], 0)


if __name__ == "__main__":
    unittest.main()
