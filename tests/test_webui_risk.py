"""Position sizing, costs, P/L, exposure and risk lockouts. Inputs are SYNTHETIC test numbers."""
import unittest

from _load import load

rk = load("webui", "risk")


class Contracts(unittest.TestCase):
    def test_gold_futures_and_micro(self):
        self.assertEqual(rk.contract("GC=F")["units_per_lot"], 100)
        self.assertEqual(rk.contract("MGC=F")["units_per_lot"], 10)

    def test_broker_differences_via_overrides(self):
        a = rk.contract("XAUUSD")
        b = rk.contract("XAUUSD", units_per_lot=1, min_lot=0.1, lot_step=0.1)
        self.assertEqual((a["units_per_lot"], b["units_per_lot"]), (100, 1))
        self.assertTrue(a["broker_specific"])
        self.assertEqual(rk.CONTRACTS["XAUUSD"]["units_per_lot"], 100)       # the table is not mutated

    def test_bad_overrides_are_rejected(self):
        with self.assertRaises(rk.RiskError):
            rk.contract("XAUUSD", units_per_lot=0)
        with self.assertRaises(rk.RiskError):
            rk.contract("XAUUSD", leverage=500)
        with self.assertRaises(rk.RiskError):
            rk.contract("NOPE")


class Costs(unittest.TestCase):
    def test_round_trip_cost(self):
        spec = rk.contract("XAUUSD")
        c = rk.costs(spread=0.30, slippage=0.10, commission_per_lot=7.0)
        self.assertAlmostEqual(rk.round_trip_cost_per_unit(c, spec), 0.30 + 0.20 + 0.07)

    def test_swap_scales_with_days(self):
        spec = rk.contract("XAUUSD")
        c = rk.costs(swap_per_unit_day=0.05)
        self.assertAlmostEqual(rk.round_trip_cost_per_unit(c, spec, days_held=3), 0.15)

    def test_negative_costs_rejected(self):
        with self.assertRaises(rk.RiskError):
            rk.costs(spread=-0.1)

    def test_no_costs_is_zero(self):
        self.assertEqual(rk.round_trip_cost_per_unit(None, rk.contract("GC=F")), 0.0)


class Sizing(unittest.TestCase):
    def test_sizes_to_risk_and_rounds_down(self):
        spec = rk.contract("XAUUSD")
        s = rk.position_size(10000, 1.0, 2000, 1990, spec)                # $100 risk, $10 stop, 100 oz per lot
        self.assertAlmostEqual(s["lots"], 0.1)
        self.assertLessEqual(s["risk_at_stop"], 100.0 + 1e-9)
        self.assertTrue(s["fits"])

    def test_costs_reduce_the_size(self):
        spec = rk.contract("XAUUSD")
        free = rk.position_size(10000, 1.0, 2000, 1990, spec)
        paid = rk.position_size(10000, 1.0, 2000, 1990, spec, rk.costs(spread=0.5, slippage=0.25))
        self.assertLess(paid["lots"], free["lots"] + 1e-12)
        self.assertLessEqual(paid["risk_at_stop"], 100.0 + 1e-9)

    def test_account_too_small_for_the_minimum_lot(self):
        spec = rk.contract("GC=F")                                         # 1 lot = 100 oz
        s = rk.position_size(2700, 1.0, 3900, 3880, spec)                  # $27 risk vs $2000 at one lot
        self.assertFalse(s["fits"])
        self.assertEqual(s["lots"], 0.0)
        self.assertGreater(s["risk_at_min_lot_pct"], 50)
        self.assertIn("minimum lot", s["reason"])

    def test_micro_contract_fits_where_full_does_not(self):
        s = rk.position_size(50000, 1.0, 3900, 3880, rk.contract("MGC=F"))  # $500 risk, 10 oz x $20 = $200 per lot
        self.assertEqual(s["lots"], 2.0)

    def test_fractional_btc(self):
        spec = rk.contract("BTC-USD")
        s = rk.position_size(2700, 1.0, 65000, 63000, spec)               # $27 risk / $2000 = 0.0135 BTC
        self.assertAlmostEqual(s["lots"], 0.0135, places=4)
        self.assertTrue(s["fits"])

    def test_short_sizes_like_long(self):
        spec = rk.contract("XAUUSD")
        a = rk.position_size(10000, 1.0, 2000, 1990, spec)
        b = rk.position_size(10000, 1.0, 2000, 2010, spec)
        self.assertEqual(a["lots"], b["lots"])

    def test_invalid_inputs(self):
        spec = rk.contract("XAUUSD")
        for args in ((0, 1, 2000, 1990), (1000, 0, 2000, 1990), (1000, 101, 2000, 1990), (1000, 1, 2000, 2000),
                     (1000, 1, -1, 1990)):
            with self.assertRaises(rk.RiskError):
                rk.position_size(*args, spec)

    def test_margin_when_leverage_given(self):
        s = rk.position_size(10000, 1.0, 2000, 1990, rk.contract("XAUUSD"), leverage=100)
        self.assertAlmostEqual(s["margin"], s["notional"] / 100)

    def test_lot_step_decimals(self):
        s = rk.position_size(10000, 1.0, 2000, 1993, rk.contract("XAUUSD"))
        self.assertAlmostEqual(round(s["lots"] / 0.01), s["lots"] / 0.01, places=6)


class Pnl(unittest.TestCase):
    def test_long_and_short(self):
        self.assertAlmostEqual(rk.pnl("long", 2000, 2010, 10)["gross"], 100)
        self.assertAlmostEqual(rk.pnl("short", 2000, 2010, 10)["gross"], -100)
        self.assertAlmostEqual(rk.pnl("short", 2000, 1990, 10)["gross"], 100)

    def test_net_after_costs(self):
        spec = rk.contract("XAUUSD")
        c = rk.costs(spread=0.5)
        r = rk.pnl("long", 2000, 2010, 10, c, spec)
        self.assertAlmostEqual(r["cost"], 5.0)
        self.assertAlmostEqual(r["net"], 95.0)

    def test_r_multiple(self):
        self.assertAlmostEqual(rk.r_multiple("long", 2000, 1990, 2020), 2.0)
        self.assertAlmostEqual(rk.r_multiple("short", 2000, 2010, 1980), 2.0)
        self.assertAlmostEqual(rk.r_multiple("long", 2000, 1990, 1990), -1.0)
        self.assertAlmostEqual(rk.r_multiple("long", 2000, 1990, 2020, cost_per_unit=2), 1.8)

    def test_wrong_side_stop_is_rejected(self):
        with self.assertRaises(rk.RiskError):
            rk.r_multiple("long", 2000, 2010, 2020)

    def test_net_reward_risk_shrinks_with_costs(self):
        free = rk.net_reward_risk("long", 2000, 1990, 2020)
        paid = rk.net_reward_risk("long", 2000, 1990, 2020, cost_per_unit=2)
        self.assertAlmostEqual(free["gross"], 2.0)
        self.assertAlmostEqual(paid["net"], 18 / 12.0)
        self.assertLess(paid["net"], free["net"])

    def test_bad_side(self):
        with self.assertRaises(rk.RiskError):
            rk.pnl("buy", 1, 2, 1)


class Exposure(unittest.TestCase):
    P = [dict(side="long", units=100, entry=2000, stop=1990),
         dict(side="short", units=40, entry=2005, stop=2015)]

    def test_hedged_exposure(self):
        e = rk.exposure(self.P, 2010)
        self.assertEqual(e["net_units"], 60)
        self.assertEqual(e["gross_units"], 140)
        self.assertEqual(e["hedged_units"], 40)
        self.assertTrue(e["hedged"])
        self.assertAlmostEqual(e["net_notional"], 60 * 2010)
        self.assertAlmostEqual(e["open_pnl"], 100 * 10 + 40 * -5)

    def test_unprotected_positions_are_listed(self):
        e = rk.exposure([dict(side="long", units=1, entry=1), dict(side="long", units=1, entry=1, stop=0.5)], 1)
        self.assertEqual(e["unprotected"], [0])

    def test_empty_book(self):
        e = rk.exposure([], 2000)
        self.assertEqual((e["net_units"], e["open_pnl"], e["hedged"]), (0, 0, False))


class Budget(unittest.TestCase):
    def test_open_risk_is_worst_case_from_entry(self):
        pos = [dict(side="long", units=10, entry=2000, stop=1990)]
        for price in (1992, 2005):                          # the same whatever the price does meanwhile
            rb = rk.risk_budget(10000, pos, price, max_open_risk_pct=2)
            self.assertAlmostEqual(rb["open_risk"], 100)
            self.assertAlmostEqual(rb["budget"], 200)
            self.assertAlmostEqual(rb["remaining"], 100)

    def test_stop_past_breakeven_adds_no_risk(self):
        pos = [dict(side="long", units=10, entry=2000, stop=2004)]
        self.assertEqual(rk.risk_budget(10000, pos, 2010)["open_risk"], 0.0)

    def test_short_open_risk(self):
        pos = [dict(side="short", units=10, entry=2000, stop=2010)]
        self.assertAlmostEqual(rk.risk_budget(10000, pos, 1995)["open_risk"], 100)

    def test_daily_loss_lockout(self):
        rb = rk.risk_budget(10000, [], 2000, daily_loss_limit_pct=3, realised_today=-300)
        self.assertTrue(rb["lockout"])
        self.assertIn("daily loss limit", " ".join(rb["alerts"]))
        self.assertFalse(rk.risk_budget(10000, [], 2000, daily_loss_limit_pct=3, realised_today=-299)["lockout"])

    def test_open_loss_counts_toward_the_daily_limit(self):
        pos = [dict(side="long", units=100, entry=2000, stop=1900)]
        rb = rk.risk_budget(10000, pos, 1975, daily_loss_limit_pct=3, realised_today=0)
        self.assertTrue(rb["lockout"])                                    # -2500 open loss, limit 300

    def test_unprotected_alert(self):
        rb = rk.risk_budget(10000, [dict(side="long", units=1, entry=2000)], 2000)
        self.assertIn("no stop", " ".join(rb["alerts"]))

    def test_position_limit_alert(self):
        pos = [dict(side="long", units=1, entry=1, stop=0.5)] * 2
        self.assertIn("position count", " ".join(rk.risk_budget(1000, pos, 1, max_positions=2)["alerts"]))


class Decision(unittest.TestCase):
    SPEC = rk.contract("XAUUSD")

    def ev(self, **kw):
        base = dict(equity=10000, positions=[], price=2000, side="long", entry=2000, stop=1990, target=2030,
                    spec=self.SPEC)
        base.update(kw)
        return rk.evaluate_new_trade(**base)

    def test_clean_plan_is_a_trade(self):
        d = self.ev()
        self.assertEqual(d["decision"], "TRADE")
        self.assertEqual(d["reasons"], [])

    def test_lockout_blocks(self):
        d = self.ev(realised_today=-400)
        self.assertEqual(d["decision"], "NO TRADE")
        self.assertIn("daily loss limit reached", d["reasons"])

    def test_costs_can_make_it_no_trade(self):
        d = self.ev(target=2012, c=rk.costs(spread=1.0, slippage=0.5), min_net_rr=1.0)
        self.assertEqual(d["decision"], "NO TRADE")
        self.assertTrue(any("after costs" in r for r in d["reasons"]))

    def test_budget_exceeded(self):
        pos = [dict(side="long", units=100, entry=2000, stop=1980)]       # already $2000 at risk
        d = self.ev(positions=pos)
        self.assertEqual(d["decision"], "NO TRADE")
        self.assertTrue(any("remaining budget" in r for r in d["reasons"]))

    def test_unprotected_position_blocks(self):
        d = self.ev(positions=[dict(side="long", units=1, entry=2000)])
        self.assertIn("an open position has no stop", d["reasons"])

    def test_hedge_blocked_unless_allowed(self):
        pos = [dict(side="short", units=1, entry=2000, stop=2050)]
        self.assertTrue(any("hedge" in r for r in self.ev(positions=pos)["reasons"]))
        self.assertFalse(any("hedge" in r for r in self.ev(positions=pos, allow_hedge=True)["reasons"]))

    def test_account_too_small(self):
        d = rk.evaluate_new_trade(2700, [], 3900, "long", 3900, 3880, 3960, rk.contract("GC=F"))
        self.assertEqual(d["decision"], "NO TRADE")
        self.assertTrue(any("minimum lot" in r for r in d["reasons"]))

    def test_invalid_plan_raises(self):
        with self.assertRaises(rk.RiskError):
            self.ev(stop=2010)


if __name__ == "__main__":
    unittest.main()
