"""Gold/Bitcoin engine: causality, fills, costs, trade plans, per-asset profiles.
Every price series here is SYNTHETIC (seeded random walk or hand-built), never market data."""
import random
import unittest

from _load import load

gold = load("webui", "gold")
bt = load("webui", "gold_backtest")
assets = load("webui", "assets")
watch = load("webui", "gold_watch")
server = load("webui", "server")


def walk(n, seed=7, start=2000.0, vol=8.0):
    rnd = random.Random(seed)
    bars, p, t = [], start, 1_700_000_000
    for _ in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol / 3))
        l = min(o, c) - abs(rnd.gauss(0, vol / 3))
        bars.append((t, h, l, c, o))
        p, t = c, t + 86400
    return bars


class Swings(unittest.TestCase):
    def test_swings_do_not_repaint(self):
        bars = walk(300)
        k = 4
        full = gold.swings(bars, k)
        for n in (60, 120, 200, 299):
            prefix = gold.swings(bars[:n], k)
            confirmed_in_full = [s for s in full if s[0] <= n - 1 - k]
            self.assertEqual(prefix, confirmed_in_full, "prefix %d" % n)

    def test_the_last_k_bars_can_never_be_swings(self):
        bars = walk(100)
        k = 5
        self.assertTrue(all(s[0] <= len(bars) - 1 - k for s in gold.swings(bars, k)))


class Causality(unittest.TestCase):
    def test_setups_use_only_the_bars_given(self):
        bars = walk(300)
        t = 220
        a = gold.compute_setups(bars[:t + 1], 2, 5, 120, [], 100)
        later = bars[:t + 1] + walk(60, seed=99, start=bars[t][3])        # a different future
        b = gold.compute_setups(later[:t + 1], 2, 5, 120, [], 100)
        self.assertEqual([(s["name"], s["level"], s["stop"]) for s in a["setups"]],
                         [(s["name"], s["level"], s["stop"]) for s in b["setups"]])
        self.assertEqual(a["atr"], b["atr"])

    def test_same_inputs_same_outputs(self):
        bars = walk(250)
        a = gold.compute_setups(bars, 3, 5, 120, [], 100)
        b = gold.compute_setups(bars, 3, 5, 120, [], 100)
        self.assertEqual(a["setups"], b["setups"])


class Exits(unittest.TestCase):
    def test_same_bar_stop_and_target_resolves_to_the_stop(self):
        bars = [(0, 2001, 1999, 2000, 2000), (1, 2012, 1988, 2000, 2000)]   # one bar touches both
        px, i, how = bt.exit_trade(bars, 0, 1, 2000, 1990, 2010, 5, False)
        self.assertEqual((how, i), ("stop", 1))
        px, i, how = bt.exit_trade(bars, 0, -1, 2000, 2010, 1990, 5, False)
        self.assertEqual(how, "stop")

    def test_gap_through_stop_exits_at_the_worse_open(self):
        bars = [(0, 2001, 1999, 2000, 2000), (1, 1985, 1980, 1982, 1985)]   # opens below the 1990 stop
        px, i, how = bt.exit_trade(bars, 0, 1, 2000, 1990, 2010, 5, False)
        self.assertEqual(how, "stop")
        self.assertEqual(px, 1985)

    def test_open_on_the_fill_bar_is_not_trusted(self):
        bars = [(0, 2001, 1980, 1995, 1985)]                                # limit filled on this bar
        px, i, how = bt.exit_trade(bars, 0, 1, 2000, 1990, 2010, 5, True)
        self.assertEqual(px, 1990)                                          # the stop price, not the open

    def test_time_exit(self):
        bars = [(i, 2001, 1999, 2000 + i * 0.1, 2000) for i in range(10)]
        px, i, how = bt.exit_trade(bars, 0, 1, 2000, 1900, 2100, 3, False)
        self.assertEqual((how, i), ("time", 3))


class Simulate(unittest.TestCase):
    def setup_dict(self, **kw):
        s = dict(name="LONG on a pullback", side="long", kind="pullback", level=2000.0, stop=1990.0,
                 targets=[2020.0, 2030.0], trend="with", weak=False)
        s.update(kw)
        return s

    def bars(self):
        # t = 0 decision bar; bar 1 trades down to 1999 (limit fills), bar 2 reaches the 2020 target
        return [(0, 2005, 2001, 2003, 2002), (1, 2004, 1999, 2002, 2003), (2, 2022, 2001, 2021, 2002),
                (3, 2025, 2015, 2020, 2021), (4, 2025, 2015, 2020, 2021)]

    def test_limit_fill_and_target(self):
        tr, _ = bt.simulate(self.bars(), 0, self.setup_dict(), window=3, hold=5, cost=0.0)
        self.assertAlmostEqual(tr["r1"], 2.0)
        self.assertEqual(tr["r1_how"], "target")
        self.assertEqual(tr["fill"], 2000.0)

    def test_cost_is_subtracted_in_r(self):
        free, _ = bt.simulate(self.bars(), 0, self.setup_dict(), 3, 5, 0.0)
        paid, _ = bt.simulate(self.bars(), 0, self.setup_dict(), 3, 5, 1.0)
        self.assertAlmostEqual(free["r1"] - paid["r1"], 1.0 / free["risk"])

    def test_nothing_fills_outside_the_window(self):
        bars = [(0, 2005, 2001, 2003, 2002)] + [(i, 2010, 2005, 2008, 2006) for i in range(1, 6)]
        tr, _ = bt.simulate(bars, 0, self.setup_dict(), 3, 5, 0.0)
        self.assertIsNone(tr)

    def test_breakout_fills_at_the_next_open_after_a_close_beyond(self):
        s = self.setup_dict(kind="breakout", name="LONG on a breakout", level=2010.0, stop=2000.0, targets=[2030.0])
        bars = [(0, 2005, 2001, 2003, 2002), (1, 2013, 2004, 2012, 2004), (2, 2016, 2011, 2015, 2014),
                (3, 2032, 2012, 2031, 2015), (4, 2032, 2020, 2030, 2030)]
        tr, _ = bt.simulate(bars, 0, s, 3, 5, 0.0)
        self.assertEqual(tr["fill"], 2014)                                  # bar 2's open, not the 2010 level
        self.assertEqual(tr["i"], 2)


class CostSensitivity(unittest.TestCase):
    TR = [dict(r1=1.0, risk=10.0), dict(r1=-1.0, risk=20.0), dict(r1=0.5, risk=10.0), dict(r1=-0.2, risk=5.0)]

    def test_base_row_matches_the_original_average(self):
        cs = bt.cost_sensitivity(self.TR, 2.0)
        row = [r for r in cs["rows"] if r["multiple"] == 1.0][0]
        self.assertAlmostEqual(row["avg"], sum(x["r1"] for x in self.TR) / 4)

    def test_repricing_formula(self):
        cs = bt.cost_sensitivity(self.TR, 2.0)
        free = cs["rows"][0]
        expect = sum(x["r1"] + 2.0 / x["risk"] for x in self.TR) / 4
        self.assertEqual(free["cost"], 0.0)
        self.assertAlmostEqual(free["avg"], expect)

    def test_breakeven_cost_gives_zero_average(self):
        cs = bt.cost_sensitivity(self.TR, 2.0)
        c = cs["breakeven"]
        avg = sum(x["r1"] + (2.0 - c) / x["risk"] for x in self.TR) / 4
        self.assertAlmostEqual(avg, 0.0, places=9)

    def test_higher_cost_never_improves_the_average(self):
        avgs = [r["avg"] for r in bt.cost_sensitivity(self.TR, 2.0)["rows"]]
        self.assertEqual(avgs, sorted(avgs, reverse=True))

    def test_no_trades(self):
        self.assertIsNone(bt.cost_sensitivity([], 1.0))

    def test_zero_base_cost_has_one_row_per_distinct_cost(self):
        cs = bt.cost_sensitivity(self.TR, 0.0)
        self.assertEqual(len(cs["rows"]), 1)


class Plans(unittest.TestCase):
    def plans(self, bars=None, unit="daily", pts=2):
        bars = bars or walk(300)
        c = gold.compute_setups(bars, pts, 5, 120, [], 100)
        return gold.trade_plans(c, unit), c

    def test_orders_are_coherent(self):
        plans, _ = self.plans()
        self.assertTrue(plans)
        for p in plans:
            if p["side"] == "long":
                self.assertLess(p["stop"], p["entry"])
                self.assertTrue(all(t["price"] > p["entry"] for t in p["targets"]))
            else:
                self.assertGreater(p["stop"], p["entry"])
                self.assertTrue(all(t["price"] < p["entry"] for t in p["targets"]))
            prices = [t["price"] for t in p["targets"]]
            self.assertEqual(prices, sorted(prices, reverse=p["side"] == "short"))
            self.assertEqual(len(set(prices)), len(prices))

    def test_r_matches_the_prices(self):
        plans, _ = self.plans()
        for p in plans:
            for t in p["targets"]:
                self.assertAlmostEqual(t["r"], abs(t["price"] - p["entry"]) / p["risk_per_oz"])

    def test_ranks_are_sequential_and_top_shows_both_sides(self):
        plans, _ = self.plans()
        self.assertEqual([p["rank"] for p in plans], list(range(1, len(plans) + 1)))
        sides = {p["side"] for p in plans}
        if sides == {"long", "short"}:
            self.assertEqual({p["side"] for p in plans[:3]}, {"long", "short"})

    def test_prices_sit_on_the_tick(self):
        plans, c = self.plans()
        tick = assets.tick_for(c["last"])
        for p in plans:
            for v in (p["entry"], p["stop"]):
                self.assertAlmostEqual(v / tick, round(v / tick), places=6)

    def test_intraday_stop_is_at_least_one_atr_away(self):
        bars = walk(300)
        c = gold.compute_setups(bars, 2, 5, 120, [], 100)
        plans = gold.trade_plans(c, "15m", min_risk_atr=1.0)
        for p in plans:
            self.assertGreaterEqual(p["risk_per_oz"] + 1e-6, c["atr"] - assets.tick_for(c["last"]))

    def test_deterministic(self):
        a, _ = self.plans()
        b, _ = self.plans()
        self.assertEqual(a, b)

    def test_grade_is_not_a_probability(self):
        plans, _ = self.plans()
        self.assertTrue(all(p["grade"] in "ABC" and "prob" not in " ".join(p.keys()) for p in plans))

    def test_no_zones_means_no_plans(self):
        c = dict(last=2000.0, atr=5.0, bias="neutral", sup=[], res=[], setups=[], levels=[])
        self.assertEqual(gold.trade_plans(c, "daily"), [])


class Profiles(unittest.TestCase):
    def test_per_asset_numbers(self):
        g, b = assets.profile("GC=F"), assets.profile("BTC-USD")
        self.assertEqual((g["per_year"], b["per_year"]), (252, 365))
        self.assertEqual((g["week"], b["week"]), (5, 7))
        self.assertEqual((g["calendar"], b["calendar"]), ("cme_gold", "24x7"))
        self.assertGreater(b["steps"]["d"], g["steps"]["d"])
        self.assertEqual(assets.profile("UNKNOWN")["id"], "gold")

    def test_tick_scales_with_price(self):
        self.assertEqual(assets.tick_for(3900), 0.5)
        self.assertEqual(assets.tick_for(65000), 5.0)

    def test_volatility_annualisation(self):
        rets = gold.daily_returns([b[3] for b in walk(120)])
        self.assertAlmostEqual(gold.ann_vol(rets, 365) / gold.ann_vol(rets, 252), (365 / 252.0) ** 0.5, places=9)

    def test_year_window_follows_the_asset(self):
        bars = walk(400)
        self.assertEqual(gold.analyse(bars, 365)["ret"]["12 months"], gold.pct(bars[-1][3], bars[-366][3]))
        self.assertEqual(gold.analyse(bars, 252)["ret"]["12 months"], gold.pct(bars[-1][3], bars[-253][3]))

    def test_pivot_week_follows_the_asset(self):
        bars = walk(300)
        t, h, l, c, o = bars[-7]                              # inside a 7-day week, outside a 5-day week
        bars[-7] = (t, h + 500, l, c, o)
        _, _, extra5 = gold.short_term_inputs(bars, 5)
        _, _, extra7 = gold.short_term_inputs(bars, 7)
        p5 = [p for p, l in extra5 if l == "weekly pivot"][0]
        p7 = [p for p, l in extra7 if l == "weekly pivot"][0]
        self.assertGreater(p7, p5 + 100)

    def test_unit_sizing_uses_the_asset_micro(self):
        class A:
            account, risk_pct = 2700.0, 1.0
        g = gold.size_dict(2000, 10, A, "GC=F")
        b = gold.size_dict(65000, 2000, A, "BTC-USD")
        self.assertAlmostEqual(g["micro_risk"], 10 * 10)
        self.assertAlmostEqual(b["micro_risk"], 0.1 * 2000)


class DataQualityPanel(unittest.TestCase):
    def test_report_structure_and_honest_notes(self):
        daily = walk(60)
        saved = dict(gold._INTRA)
        gold._INTRA["TEST=F"] = {"bars": {"15m": walk(30), "1h": walk(30)}}
        try:
            q = gold.data_quality("GC=F", daily, False)
        finally:
            gold._INTRA.clear()
            gold._INTRA.update(saved)
        self.assertEqual([s["label"] for s in q["series"]], ["daily"])
        self.assertFalse(q["bid_ask"])
        self.assertFalse(q["spread_available"])
        self.assertIn("still forming", " ".join(q["notes"]))
        self.assertIn("not spot XAUUSD", " ".join(q["notes"]))

    def test_intraday_series_are_included_when_available(self):
        gold._INTRA["GC=F"] = {"bars": {"15m": walk(30), "1h": walk(30)}}
        try:
            q = gold.data_quality("GC=F", walk(60), False)
        finally:
            gold._INTRA.pop("GC=F", None)
        self.assertEqual([s["label"] for s in q["series"]], ["15m", "1h", "daily"])

    def test_a_failure_is_reported_not_raised(self):
        q = gold.data_quality("GC=F", None, False)
        self.assertIn("error", q)


class BitcoinRegression(unittest.TestCase):
    def test_commands_exist_for_both_assets(self):
        ids = {c["id"] for c in server.COMMANDS}
        for need in ("gold", "gold-backtest", "gold-news", "btc", "btc-backtest", "btc-news", "data-check"):
            self.assertIn(need, ids)

    def test_each_asset_has_its_own_watcher_and_files(self):
        self.assertEqual(set(server.WATCHERS), {"gold", "btc"})
        gf, gl = watch.files(assets.GOLD)
        bf, bl = watch.files(assets.BTC)
        self.assertEqual(len({gf, gl, bf, bl}), 4)
        self.assertTrue(gf.endswith("gold_watch.json"))                    # gold keeps its original file names

    def test_watcher_symbols_cannot_cross(self):
        self.assertEqual(watch.clean_settings({"symbol": "BTC-USD"}, assets.GOLD)["symbol"], "GC=F")
        self.assertEqual(watch.clean_settings({"symbol": "GC=F"}, assets.BTC)["symbol"], "BTC-USD")
        self.assertEqual(watch.clean_settings({"symbol": "MGC=F"}, assets.GOLD)["symbol"], "MGC=F")

    def test_watcher_settings_are_validated(self):
        s = watch.clean_settings({"near_atr": 99, "poll_min": 7, "tfs": ["1d"], "toast": "yes"}, assets.BTC)
        self.assertEqual((s["near_atr"], s["poll_min"], s["tfs"], s["toast"]), (0.5, 5, ["15m", "1h", "4h"], True))

    def test_watch_routes_only_match_known_assets(self):
        self.assertTrue(server.WATCH_PATH.match("/api/btc/watch"))
        self.assertTrue(server.WATCH_PATH.match("/api/gold/watch"))
        self.assertIsNone(server.WATCH_PATH.match("/api/eth/watch"))

    def test_btc_cost_default_is_in_dollars_per_btc(self):
        self.assertEqual(assets.BTC["default_cost"], 25)
        self.assertEqual(assets.GOLD["default_cost"], 0.5)


if __name__ == "__main__":
    unittest.main()
