"""BUY STOP, SELL STOP, BUY LIMIT, SELL LIMIT: geometry, regime gates, causality, fills and order statistics. All SYNTHETIC."""
import random
import unittest
from unittest import mock

from _load import load

ps = load("webui", "pending_strategies")
st = load("webui", "strategies")
bt = load("webui", "backtester")
rg = load("webui", "regimes")

T0 = 1_700_006_400
STEP = 3600


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def flat(n, price=100.0, rng=2.0):
    return [bar(T0 + i * STEP, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


def walk(n, seed=3, vol=1.0):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        out.append(bar(T0 + i * STEP, o, max(o, c) + abs(rnd.gauss(0, vol / 3)), min(o, c) - abs(rnd.gauss(0, vol / 3)), c))
        p = c
    return out


def drive(strategy, bars, htf=None, spread=0.3, cost=0.5, gate=True):
    """Feed closed bars one at a time. gate=False bypasses the regime gate so geometry can be tested on a flat market."""
    A = rg.Analysis(bars)
    strategy.reset()
    strategy.spread, strategy.cost_per_unit = spread, cost
    out = []
    ctx = mock.patch.object(ps, "regime_gate", lambda *a, **k: (True, "")) if not gate else mock.MagicMock()
    with ctx:
        for i in range(len(bars)):
            A.step(i)
            out.extend(strategy.on_bar(A, i, htf))
    return out


class Geometry(unittest.TestCase):
    BARS = flat(70)                                       # price 100, bars 99 to 101: levels at 101 (highs) and 99 (lows)
    MIN_STOP = 0.30

    def one(self, cls, **params):
        sigs = drive(cls(**params), self.BARS, gate=False)
        self.assertTrue(sigs, "no order was produced")
        return sigs[0]

    def test_buy_stop_sits_above_the_ask_with_a_buffer_over_resistance(self):
        s = self.one(ps.BuyStop)
        ask = 100.0 + 0.15
        self.assertEqual((s["side"], s["kind"], s["meta"]["order_type"]), ("long", "stop", "BUY STOP"))
        self.assertGreaterEqual(s["price"], ask + self.MIN_STOP)
        self.assertGreater(s["price"], s["meta"]["level"])                        # trigger is beyond the level, by the buffer
        self.assertGreaterEqual(s["meta"]["buffer"], 2 * 0.3 - 1e-9)              # at least twice the spread
        self.assertLess(s["stop"], s["meta"]["level"])
        self.assertEqual(s["targets"], sorted(s["targets"]))
        self.assertTrue(all(t > s["price"] for t in s["targets"]))
        self.assertEqual(s["meta"]["invalid_price"], s["stop"])
        self.assertEqual(s["expires"], 8)

    def test_sell_stop_sits_below_the_bid(self):
        s = self.one(ps.SellStop)
        bid = 100.0 - 0.15
        self.assertEqual((s["side"], s["kind"], s["meta"]["order_type"]), ("short", "stop", "SELL STOP"))
        self.assertLessEqual(s["price"], bid - self.MIN_STOP)
        self.assertLess(s["price"], s["meta"]["level"])
        self.assertGreater(s["stop"], s["meta"]["level"])
        self.assertEqual(s["targets"], sorted(s["targets"], reverse=True))
        self.assertTrue(all(t < s["price"] for t in s["targets"]))

    def test_buy_limit_rests_below_the_bid_just_above_support(self):
        s = self.one(ps.BuyLimit, stop_atr=0.9, min_net_rr=0.5, max_cost_r=0.5)
        bid = 100.0 - 0.15
        self.assertEqual((s["side"], s["kind"], s["meta"]["order_type"]), ("long", "limit", "BUY LIMIT"))
        self.assertLessEqual(s["price"], bid - self.MIN_STOP)
        self.assertGreaterEqual(s["price"], s["meta"]["level"])                    # just above support, so it can fill
        self.assertLess(s["stop"], s["meta"]["level"])                             # the stop is below the zone
        self.assertEqual(s["expires"], 8)

    def test_sell_limit_rests_above_the_ask_just_below_resistance(self):
        s = self.one(ps.SellLimit, stop_atr=0.9, min_net_rr=0.5, max_cost_r=0.5)
        ask = 100.0 + 0.15
        self.assertEqual((s["side"], s["kind"], s["meta"]["order_type"]), ("short", "limit", "SELL LIMIT"))
        self.assertGreaterEqual(s["price"], ask + self.MIN_STOP)
        self.assertLessEqual(s["price"], s["meta"]["level"])
        self.assertGreater(s["stop"], s["meta"]["level"])

    def test_the_stop_is_never_widened_to_fit(self):
        # with the default 0.3 ATR margin a limit order's structural stop is only 0.35 ATR away: refused, not stretched
        s = ps.BuyLimit()
        self.assertEqual(drive(s, self.BARS, gate=False), [])
        why = [r["reason"] for r in s.rejected if "structural stop" in r["reason"]]
        self.assertEqual(len(why), 1)                                              # recorded once, not on every bar
        self.assertIn("will not be moved to fit", why[0])

    def test_net_reward_after_costs_is_enforced(self):
        s = ps.BuyLimit(stop_atr=0.9, max_cost_r=0.5)                              # geometry fine, but T1 pays under 1R after costs
        self.assertEqual(drive(s, self.BARS, gate=False), [])
        self.assertTrue(any("after costs" in r["reason"] for r in s.rejected))

    def test_costs_that_eat_the_reward_veto_every_type(self):
        for cls in (ps.BuyStop, ps.SellStop):
            self.assertEqual(drive(cls(), self.BARS, cost=6.0, gate=False), [], cls.__name__)

    def test_one_working_order_per_level_until_it_expires(self):
        sigs = drive(ps.BuyStop(expiry_bars=4), self.BARS, gate=False)
        idx = [s["decided_i"] for s in sigs]
        self.assertGreaterEqual(len(idx), 2)
        self.assertTrue(all(b - a > 4 for a, b in zip(idx, idx[1:])), idx)

    def test_a_level_too_far_away_makes_no_order(self):
        far = flat(70, rng=2.0)
        far[-1] = bar(far[-1][0], 100, 101, 99, 100)
        sigs = drive(ps.BuyStop(max_dist_atr=0.1), far, gate=False)
        self.assertEqual(sigs, [])

    def test_a_wider_broker_stop_level_pushes_the_trigger_out(self):
        near = self.one(ps.BuyStop, min_stop=0.30)
        far = self.one(ps.BuyStop, min_stop=3.0)
        self.assertGreaterEqual(far["price"], 100.15 + 3.0)
        self.assertGreater(far["price"], near["price"])

    def test_each_order_type_is_its_own_strategy(self):
        self.assertEqual(set(ps.PENDING), {"buy_stop", "sell_stop", "buy_limit", "sell_limit"})
        self.assertEqual(ps.BY_ORDER_TYPE, {"BUY STOP": "buy_stop", "SELL STOP": "sell_stop", "BUY LIMIT": "buy_limit", "SELL LIMIT": "sell_limit"})
        for name in ps.PENDING:
            self.assertEqual(st.make(name).name, name)
            self.assertLessEqual(len(st.param_grid(name)), 8)
        self.assertTrue(set(ps.PENDING).isdisjoint(st.STRATEGIES))                  # the core four are untouched


class Gate(unittest.TestCase):
    def snap(self, trend="sideways", label=None, vol="normal", ratio=1.0):
        return dict(label=label or trend, trend=trend, vol=vol, vol_ratio=ratio)

    def test_news_blocks_everything(self):
        for t in ("BUY STOP", "SELL STOP", "BUY LIMIT", "SELL LIMIT"):
            ok, why = ps.regime_gate(t, self.snap("strong_bull", label="post_news"), None)
            self.assertFalse(ok, t)
            self.assertIn("news-driven", why)

    def test_buys_are_suppressed_in_a_downtrend(self):
        for t in ("BUY STOP", "BUY LIMIT"):
            self.assertFalse(ps.regime_gate(t, self.snap("strong_bear"), None)[0])
            self.assertFalse(ps.regime_gate(t, self.snap("weak_bull"), dict(trend="strong_bear"))[0])
            self.assertFalse(ps.regime_gate(t, self.snap("weak_bear"), dict(trend="weak_bear"))[0])
            self.assertTrue(ps.regime_gate(t, self.snap("weak_bull"), dict(trend="weak_bear"))[0] or t == "BUY STOP")

    def test_sells_are_suppressed_in_an_uptrend(self):
        for t in ("SELL STOP", "SELL LIMIT"):
            self.assertFalse(ps.regime_gate(t, self.snap("strong_bull"), None)[0])
            self.assertFalse(ps.regime_gate(t, self.snap("weak_bear"), dict(trend="strong_bull"))[0])
            self.assertFalse(ps.regime_gate(t, self.snap("weak_bull"), dict(trend="weak_bull"))[0])

    def test_trend_following_orders_are_allowed_with_the_trend(self):
        self.assertTrue(ps.regime_gate("BUY STOP", self.snap("strong_bull"), dict(trend="strong_bull"))[0])
        self.assertTrue(ps.regime_gate("BUY LIMIT", self.snap("strong_bull"), None)[0])
        self.assertTrue(ps.regime_gate("SELL STOP", self.snap("strong_bear"), None)[0])
        self.assertTrue(ps.regime_gate("SELL LIMIT", self.snap("strong_bear"), None)[0])

    def test_breakouts_in_a_sideways_market_need_volatility_expansion(self):
        for t in ("BUY STOP", "SELL STOP"):
            ok, why = ps.regime_gate(t, self.snap("sideways"), None)
            self.assertFalse(ok)
            self.assertIn("volatility expansion", why)
            self.assertTrue(ps.regime_gate(t, self.snap("sideways", vol="expansion", ratio=1.6), None)[0])
            self.assertTrue(ps.regime_gate(t, self.snap("sideways", ratio=1.25), None)[0])

    def test_limit_orders_are_allowed_in_a_range(self):
        for t in ("BUY LIMIT", "SELL LIMIT"):
            self.assertTrue(ps.regime_gate(t, self.snap("sideways"), None)[0])

    def test_the_gate_stops_orders_in_a_real_flat_market(self):
        s = ps.BuyStop()
        self.assertEqual(drive(s, flat(70)), [])
        self.assertTrue(any("volatility expansion" in r["reason"] for r in s.rejected))


class Causality(unittest.TestCase):
    def test_orders_never_change_when_later_bars_arrive(self):
        bars = walk(900, seed=17, vol=1.2)
        for name in ps.PENDING:
            full = bt.generate(bars, [st.make(name)], htf_sec=4 * 3600, spread=0.1)[0]
            full = list(full.values())[0]
            for n in (300, 520, 760):
                part = list(bt.generate(bars[:n], [st.make(name)], htf_sec=4 * 3600, spread=0.1)[0].values())[0]
                self.assertEqual(part, [s for s in full if s["decided_i"] < n], "%s at %d" % (name, n))

    def test_orders_are_structurally_valid_on_random_data(self):
        bars = walk(1200, seed=23, vol=1.2)
        total = 0
        for name in ps.PENDING:
            inst = st.make(name)
            sigs = bt.generate(bars, [inst], htf_sec=4 * 3600, spread=0.1)[0][id(inst)]
            for s in sigs:
                total += 1
                sg = 1 if s["side"] == "long" else -1
                self.assertGreater((s["price"] - s["stop"]) * sg, 0)
                self.assertTrue(all((t - s["price"]) * sg > 0 for t in s["targets"]))
                self.assertLessEqual(len(s["targets"]), 3)
                self.assertEqual(s["kind"], "stop" if "stop" in name else "limit")
                self.assertEqual(s["meta"]["order_type"].split()[0], "BUY" if sg == 1 else "SELL")
                self.assertGreaterEqual(s["meta"]["net_rr"], 1.0 - 1e-9)
                self.assertEqual(s["meta"]["invalid_price"], s["stop"])
        self.assertGreater(total, 0)

    def test_deterministic(self):
        bars = walk(600, seed=5, vol=1.2)
        a = bt.generate(bars, [st.make("buy_limit")], spread=0.1)[0]
        b = bt.generate(bars, [st.make("buy_limit")], spread=0.1)[0]
        self.assertEqual(list(a.values()), list(b.values()))


def bars_from(rows):
    return [bar(1_700_000_000 + i * 3600, *r) for i, r in enumerate(rows)]      # rows are (o, h, l, c)


def osig(i, kind, side, price, stop, targets, expires=3):
    return dict(id="x-%d-%s" % (i, kind), strategy="x", side=side, kind=kind, price=price, stop=stop, targets=targets, expires=expires, decided_i=i,
                decided_t=1_700_000_000 + i * 3600, regime="r", session="s", atr=1.0, risk=abs(price - stop), note="", invalidation="", meta={})


FREE = bt.costs()


class Execution(unittest.TestCase):
    def test_a_stop_order_is_cancelled_if_price_closes_through_its_stop_first(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.2, 95.0, 96.0), (96, 96.5, 95.5, 96), (96, 105, 95, 104)]
        trades, rej = bt.execute(bars_from(rows), [osig(0, "stop", "long", 101.0, 97.0, [105.0], expires=3)], FREE, dict(max_hold=10))
        self.assertEqual(trades, [])
        self.assertTrue(any("cancelled" in r["reason"] for r in rej))

    def test_trades_record_the_order_price_slippage_and_wait(self):
        rows = [(100, 100.4, 99.6, 100), (100, 100.5, 99.9, 100.2), (100.2, 102.0, 100.1, 101.8), (101.8, 106, 101.5, 105)]
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "stop", "long", 101.0, 98.0, [104.0], expires=3)], bt.costs(slippage=0.25), dict(max_hold=10))
        self.assertEqual(t["order_price"], 101.0)
        self.assertAlmostEqual(t["entry_slip"], 0.25)                                # the trigger price was not the fill price
        self.assertEqual(t["waited"], 2)

    def test_a_gap_through_the_trigger_costs_more_than_the_slippage_alone(self):
        rows = [(100, 100.4, 99.6, 100), (103, 104, 102.5, 103.5), (103.5, 108, 103, 107)]
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "stop", "long", 101.0, 98.0, [106.0], expires=2)], FREE, dict(max_hold=10))
        self.assertAlmostEqual(t["entry"], 103.0)                                    # filled at the open, not at 101
        self.assertAlmostEqual(t["entry_slip"], 2.0)

    def test_a_limit_filled_below_gets_the_limit_price_not_a_better_one(self):
        rows = [(100, 100.4, 99.6, 100), (99, 99.5, 96.0, 98), (98, 103, 97.5, 102)]
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "limit", "long", 98.5, 95.0, [101.0], expires=2)], FREE, dict(max_hold=10))
        self.assertAlmostEqual(t["entry"], 98.5)
        self.assertAlmostEqual(t["entry_slip"], 0.0)


class LimitFillBar(unittest.TestCase):
    """The high of the bar that fills a limit order may have come before the fill, so no target is credited on that bar."""

    def test_no_target_on_the_bar_a_limit_fills(self):
        rows = [(100, 100.4, 99.6, 100), (100.5, 106.0, 98.0, 105.0), (105, 106.5, 104.0, 106.0)]          # bar 1 fills at 99 and touches 104
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "limit", "long", 99.0, 95.0, [104.0], expires=2)], FREE, dict(max_hold=10))
        self.assertEqual(t["exits"][0]["i"], 2)                                                                # credited a bar later, not on the fill bar
        self.assertEqual(t["exits"][0]["how"], "target 1")

    def test_the_stop_can_still_hit_on_that_bar(self):
        rows = [(100, 100.4, 99.6, 100), (100.5, 106.0, 94.0, 96.0), (96, 97, 95.5, 96)]                    # closes at 96, above the stop, so the order is not cancelled
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "limit", "long", 99.0, 95.0, [104.0], expires=2)], FREE, dict(max_hold=10))
        self.assertEqual((t["exits"][0]["i"], t["exits"][0]["how"]), (1, "stop"))
        self.assertAlmostEqual(t["r"], -1.0)

    def test_short_limits_mirror_it(self):
        rows = [(100, 100.4, 99.6, 100), (99.5, 102.5, 94.0, 95.0), (95, 96, 93, 94)]
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "limit", "short", 101.0, 105.0, [96.0], expires=2)], FREE, dict(max_hold=10))
        self.assertEqual(t["exits"][0]["i"], 2)

    def test_stop_entries_and_market_orders_keep_same_bar_targets(self):
        rows = [(100, 100.4, 99.6, 100), (100.5, 106.0, 100.0, 105.0), (105, 106, 104, 105)]
        (t,), _ = bt.execute(bars_from(rows), [osig(0, "stop", "long", 101.0, 98.0, [104.0], expires=2)], FREE, dict(max_hold=10))
        self.assertEqual(t["exits"][0]["i"], 1)                                                                # price crossed the trigger on its way to 106
        (m,), _ = bt.execute(bars_from(rows), [osig(0, "market", "long", 100.0, 98.0, [104.0], expires=1)], FREE, dict(max_hold=10))
        self.assertEqual(m["exits"][0]["i"], 1)


class OrderStatistics(unittest.TestCase):
    def run_three(self):
        rows = [(100, 100.4, 99.6, 100)] * 2 + [(100, 100.5, 99.5, 100)] * 30
        rows[2] = (100, 103.2, 99.9, 103.0)                                         # order A: buy stop 101 fills here, reaches 103 next
        rows[3] = (103, 104.0, 102.5, 103.8)
        sigs = [osig(0, "stop", "long", 101.0, 99.0, [103.0], expires=4),          # fills and wins
                osig(10, "limit", "long", 90.0, 85.0, [95.0], expires=3),          # never reached: expires
                osig(15, "stop", "long", 110.0, 99.0, [115.0], expires=3)]         # never triggers: expires
        rows[16] = (100, 100.2, 97.0, 98.0)                                         # closes below order C's stop: it is cancelled instead
        bars = bars_from(rows)
        tr, rej = bt.execute(bars, sigs, FREE, dict(max_hold=10))
        return sigs, tr, rej

    def test_counts_add_up(self):
        sigs, tr, rej = self.run_three()
        o = bt.order_stats(sigs, tr, rej)
        self.assertEqual(o["submitted"], 3)
        self.assertEqual(o["filled"], 1)
        self.assertEqual(o["expired"], 1)
        self.assertEqual(o["cancelled"], 1)
        self.assertEqual(o["skipped"], 0)
        self.assertEqual(o["filled"] + o["expired"] + o["cancelled"] + o["skipped"], o["submitted"])

    def test_fill_rate_and_hit_rate_are_separate_with_bands(self):
        sigs, tr, rej = self.run_three()
        o = bt.order_stats(sigs, tr, rej)
        self.assertAlmostEqual(o["fill_rate"], 1 / 3.0)
        self.assertEqual(o["target_hit_rate"], 1.0)
        self.assertLess(o["fill_ci"][0], o["fill_rate"])
        self.assertGreater(o["fill_ci"][1], o["fill_rate"])
        self.assertLess(o["target_hit_ci"][0], 1.0)                                  # one trade can never prove a 100% hit rate
        self.assertFalse(o["enough"])
        self.assertAlmostEqual(o["expected_per_submitted"], o["fill_rate"] * bt.summary(tr)["expectancy"])

    def test_entry_slippage_and_wait_are_reported(self):
        sigs, tr, rej = self.run_three()
        o = bt.order_stats(sigs, tr, rej)
        self.assertAlmostEqual(o["avg_entry_slip"], 0.0)
        self.assertEqual(o["avg_bars_to_fill"], 2.0)
        self.assertIn("not filled within # bar(s)", o["reasons"])

    def test_position_open_counts_as_skipped_not_expired(self):
        rows = [(100, 100.4, 99.6, 100)] * 12
        a = osig(0, "limit", "long", 99.0, 95.0, [110.0], expires=2)
        b = osig(1, "limit", "long", 99.0, 95.0, [110.0], expires=2)
        rows[1] = (100, 100.2, 98.0, 99.0)                                           # A fills on bar 1; B decided on bar 1 finds A open
        tr, rej = bt.execute(bars_from(rows), [a, b], FREE, dict(max_hold=8))
        o = bt.order_stats([a, b], tr, rej)
        self.assertEqual((o["filled"], o["skipped"], o["expired"]), (1, 1, 0))

    def test_enough_only_with_thirty_filled(self):
        sigs = [osig(i, "market", "long", 100.0, 98.0, [104.0]) for i in range(40)]
        trades = [dict(r=1.0, risk=2.0, entry_slip=0.0, waited=1, exits=[dict(how="target 1")], how="target", entry_t=i, mae_r=0, mfe_r=1, bars_held=2, cost_r=0.1)
                  for i in range(35)]
        o = bt.order_stats(sigs, trades, [])
        self.assertTrue(o["enough"])
        self.assertEqual(o["skipped"], 5)

    def test_nothing_submitted(self):
        o = bt.order_stats([], [], [])
        self.assertEqual((o["submitted"], o["fill_rate"], o["target_hit_rate"], o["expected_per_submitted"]), (0, None, None, None))


class WalkForward(unittest.TestCase):
    def test_walk_forward_returns_order_statistics_for_the_test_windows_only(self):
        import os
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        try:
            bars = walk(1800, seed=4, vol=1.1)
            wf = dict(train_min=500, test_len=250, holdout_frac=0.2, min_train_trades=3, warmup=60)
            r = bt.walk_forward(bars, "buy_stop", bt.costs(spread=0.2, slippage=0.05), dict(max_hold=20), wf, htf_sec=4 * 3600,
                                holdout_store=os.path.join(d, "h.json"), symbol="T", interval="60m")
            o = r["orders"]
            self.assertIn("fill_rate", o)
            self.assertEqual(o["filled"], r["oos"].get("n", 0))
            self.assertGreaterEqual(o["submitted"], o["filled"])
            self.assertEqual(o["filled"] + o["expired"] + o["cancelled"] + o["skipped"], o["submitted"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
