"""Pending-order rules, plans and account-wide risk. Prices and accounts here are SYNTHETIC."""
import os
import shutil
import tempfile
import unittest

from _load import load

od = load("webui", "orders")

P = od.clean_settings({})["profile"]
L = od.clean_settings({})["limits"]
BID, ASK = 2000.00, 2000.30


class Settings(unittest.TestCase):
    def test_defaults_are_placeholders_not_verified(self):
        s = od.clean_settings({})
        self.assertFalse(s["profile"]["verified"])
        self.assertFalse(s["profile"]["supports_stop_limit"])
        self.assertEqual(s["limits"]["risk_pct"], 0.5)
        self.assertEqual(s["limits"]["max_aggregate_risk_pct"], 1.0)
        self.assertEqual(s["limits"]["daily_loss_pct"], 1.5)
        self.assertEqual(s["profile"]["account_mode"], "hedging")

    def test_bad_values_are_rejected_with_a_message(self):
        bad = [dict(profile=dict(tick_size=0)), dict(profile=dict(min_lot="x")), dict(profile=dict(account_mode="mixed")),
               dict(profile=dict(symbol="XAU;rm")), dict(profile=dict(verified="yes")), dict(profile=dict(min_lot=5, max_lot=1)),
               dict(limits=dict(risk_pct=50)), dict(limits=dict(risk_pct=2, max_aggregate_risk_pct=1)), dict(limits=dict(daily_loss_pct=0)),
               dict(limits=dict(require_oco_for_opposing="no")), dict(profile=dict(spread=float("nan")))]
        for b in bad:
            with self.assertRaises(od.OrderError, msg=str(b)):
                od.clean_settings(b)

    def test_overrides_and_round_trip(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "s.json")
            self.assertFalse(od.settings_saved(p))
            saved = od.save_settings(dict(profile=dict(symbol="xauusd", stops_level_points=50, account_mode="netting", verified=True),
                                          limits=dict(risk_pct=0.25)), p)
            self.assertTrue(od.settings_saved(p))
            back = od.load_settings(p)
            self.assertEqual(back, saved)
            self.assertEqual((back["profile"]["symbol"], back["profile"]["stops_level_points"], back["profile"]["account_mode"]), ("XAUUSD", 50, "netting"))
            self.assertEqual(od.load_settings(os.path.join(d, "none.json")), od.clean_settings({}))
            with self.assertRaises(od.OrderError):
                od.save_settings(dict(limits=dict(risk_pct=99)), os.path.join(d, "bad.json"))
            self.assertFalse(os.path.exists(os.path.join(d, "bad.json")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Ticks(unittest.TestCase):
    def test_rounding_modes(self):
        self.assertEqual(od.round_to_tick(2000.123, 0.01, "up"), 2000.13)
        self.assertEqual(od.round_to_tick(2000.123, 0.01, "down"), 2000.12)
        self.assertEqual(od.round_to_tick(2000.126, 0.01, "nearest"), 2000.13)
        self.assertEqual(od.round_to_tick(2000.12, 0.01, "up"), 2000.12)           # already on the tick: not pushed up
        self.assertEqual(od.round_to_tick(2000.2, 0.5, "up"), 2000.5)
        self.assertEqual(od.round_to_tick(2000.2, 0.5, "down"), 2000.0)

    def test_on_tick(self):
        self.assertTrue(od.on_tick(2000.13, 0.01))
        self.assertFalse(od.on_tick(2000.135, 0.01))


def order(t, price, stop, targets=(), lots=0.1, **kw):
    return dict(type=t, price=price, stop=stop, targets=list(targets), lots=lots, **kw)


class Rules(unittest.TestCase):
    D = od.min_distance(P)                                        # 30 points x 0.01 = 0.30

    def ok(self, o, profile=P):
        errs, notes = od.validate_order(profile, o, BID, ASK)
        self.assertEqual(errs, [], errs)
        return notes

    def bad(self, o, text, profile=P):
        errs, _ = od.validate_order(profile, o, BID, ASK)
        self.assertTrue(any(text in e for e in errs), errs)

    def test_each_order_type_must_sit_on_its_own_side_of_the_market(self):
        self.ok(order("BUY STOP", 2010.00, 2000.00, [2025, 2040]))
        self.ok(order("SELL STOP", 1990.00, 2000.30, [1975, 1960]))
        self.ok(order("BUY LIMIT", 1995.00, 1988.00, [2010, 2020]))
        self.ok(order("SELL LIMIT", 2006.00, 2013.00, [1990, 1980]))
        self.bad(order("BUY STOP", 2000.50, 1990.0), "BUY STOP must be at least")                 # needs Ask + 0.30 = 2000.60
        self.bad(order("SELL STOP", 1999.80, 2010.0), "SELL STOP must be at least")               # needs Bid - 0.30 = 1999.70
        self.bad(order("BUY LIMIT", 2000.20, 1990.0), "BUY LIMIT must be at least")               # needs Ask - 0.30 = 1999.99 or lower
        self.bad(order("SELL LIMIT", 2000.20, 2010.0), "SELL LIMIT must be at least")             # needs Bid + 0.30 = 2000.30 or higher
        self.bad(order("BUY STOP", 1990.00, 1980.0), "BUY STOP must be at least")                 # a buy stop below the market is a limit order

    def test_exact_boundary_is_allowed(self):
        self.ok(order("BUY STOP", 2000.60, 1990.0, [2020]))
        self.ok(order("SELL LIMIT", 2000.30, 2010.0, [1990]))

    def test_stop_loss_and_targets(self):
        self.bad(order("BUY STOP", 2010.0, 2010.0), "stop loss must be at least")
        self.bad(order("BUY STOP", 2010.0, 2020.0), "stop loss must be at least")                 # stop above a buy
        self.bad(order("SELL STOP", 1990.0, 1990.0), "stop loss must be at least")
        self.bad(order("BUY STOP", 2010.0, 2000.0, [2005.0]), "TP1")
        self.ok(order("BUY STOP", 2010.0, 2000.0, [2020.0]))
        self.bad(order("BUY STOP", 2010.0, 2000.0, [2020.0, 2015.0]), "TP2 must be further than TP1")
        self.bad(order("SELL STOP", 1990.0, 2000.0, [1980.0, 1985.0]), "TP2 must be further than TP1")

    def test_tick_and_volume_rules(self):
        self.bad(order("BUY STOP", 2010.005, 2000.0), "tick size")
        self.bad(order("BUY STOP", 2010.0, 2000.003), "stop loss is not on the tick size")
        self.bad(order("BUY STOP", 2010.0, 2000.0, lots=0.005), "outside the broker's")
        self.bad(order("BUY STOP", 2010.0, 2000.0, lots=0.105), "lot step")
        self.bad(order("BUY STOP", 2010.0, 2000.0, lots=500), "outside the broker's")

    def test_invalid_quotes_are_refused(self):
        errs, _ = od.validate_order(P, order("BUY STOP", 2010.0, 2000.0), 2000.5, 2000.0)         # ask below bid
        self.assertTrue(errs)
        errs, _ = od.validate_order(P, order("BUY STOP", 2010.0, 2000.0), 0, 0)
        self.assertTrue(errs)

    def test_freeze_level_is_a_note_not_an_error(self):
        prof = dict(P, freeze_level_points=100)
        notes = self.ok(order("BUY STOP", 2000.70, 1990.0, [2020]), prof)
        self.assertTrue(any("freeze level" in n for n in notes))

    def test_a_bigger_stop_level_is_respected(self):
        prof = dict(P, stops_level_points=500)                                                      # 5.00
        self.bad(order("BUY STOP", 2004.0, 1990.0), "BUY STOP must be at least", prof)
        self.ok(order("BUY STOP", 2006.0, 1990.0, [2030]), prof)

    def test_stop_limit_is_unavailable_until_the_profile_says_so(self):
        o = order("BUY STOP LIMIT", 2010.0, 1995.0, [2030], stoplimit=2006.0)
        errs, _ = od.validate_order(P, o, BID, ASK)
        self.assertEqual(len(errs), 1)
        self.assertIn("unavailable", errs[0])
        prof = dict(P, supports_stop_limit=True)
        errs, notes = od.validate_order(prof, o, BID, ASK)
        self.assertEqual(errs, [])
        self.assertTrue(any("may stay unfilled" in n.lower() or "stay unfilled" in n for n in notes))

    def test_stop_limit_needs_a_sensible_limit_price(self):
        prof = dict(P, supports_stop_limit=True)
        errs, _ = od.validate_order(prof, order("BUY STOP LIMIT", 2010.0, 1995.0, [2030]), BID, ASK)
        self.assertTrue(any("needs a limit price" in e for e in errs))
        errs, _ = od.validate_order(prof, order("BUY STOP LIMIT", 2010.0, 1995.0, [2030], stoplimit=2012.0), BID, ASK)
        self.assertTrue(any("limit price must sit beyond the trigger" in e for e in errs))
        errs, _ = od.validate_order(prof, order("SELL STOP LIMIT", 1990.0, 2005.0, [1970], stoplimit=1994.0), BID, ASK)
        self.assertEqual(errs, [])
        self.assertNotIn("BUY STOP LIMIT", od.ORDER_TYPES)
        self.assertEqual({"BUY STOP LIMIT", "SELL STOP LIMIT"}, set(od.ADVANCED_TYPES))

    def test_unknown_type(self):
        errs, _ = od.validate_order(P, order("MARKET", 2000.0, 1990.0), BID, ASK)
        self.assertTrue(errs)


class Targets(unittest.TestCase):
    TPS = [2020.0, 2030.0, 2040.0]

    def test_hedging_account_with_enough_volume_splits_into_orders(self):
        t = od.target_plan(P, 0.10, self.TPS)
        self.assertEqual((t["mode"], t["available"]), ("split_orders", True))
        self.assertAlmostEqual(sum(l["lots"] for l in t["legs"]), 0.10)
        self.assertTrue(all(l["lots"] >= P["min_lot"] for l in t["legs"]))
        self.assertEqual([l["target"] for l in t["legs"]], self.TPS)

    def test_a_volume_too_small_to_split_is_not_pretended(self):
        t = od.target_plan(P, 0.02, self.TPS)
        self.assertEqual((t["mode"], t["available"]), ("client_side_partial_exits", False))
        self.assertIn("NOT available", t["note"])

    def test_netting_never_offers_separate_take_profits(self):
        t = od.target_plan(dict(P, account_mode="netting"), 1.0, self.TPS)
        self.assertEqual((t["mode"], t["available"]), ("client_side_partial_exits", False))
        self.assertIn("netting", t["note"])

    def test_single_target(self):
        t = od.target_plan(P, 0.1, [2020.0])
        self.assertEqual(t["mode"], "single_target")
        self.assertEqual(t["legs"][0]["lots"], 0.1)

    def test_two_targets_split(self):
        t = od.target_plan(P, 0.10, self.TPS[:2])
        self.assertEqual(t["mode"], "split_orders")
        self.assertEqual(len(t["legs"]), 2)
        self.assertAlmostEqual(sum(l["lots"] for l in t["legs"]), 0.10)


def sig(order_type="BUY STOP", price=2010.004, stop=1999.996, targets=(2025.0, 2038.0, 2050.0), expires=8, decided_t=1_700_000_000, i=1):
    side = "long" if order_type.startswith("BUY") else "short"
    return dict(id="%s-60m-%d-%s-abc123" % (order_type.lower().replace(" ", "_"), decided_t + i, side), strategy=order_type.lower().replace(" ", "_"),
                side=side, kind=od.KIND[order_type], price=price, stop=stop, targets=list(targets), expires=expires, decided_i=10, decided_t=decided_t,
                regime="strong_bull", note="test", invalidation="close below 2000", atr=5.0, risk=abs(price - stop),
                meta=dict(order_type=order_type, invalid_price=stop))


class Plans(unittest.TestCase):
    NOW = 1_700_000_000 + 600

    def plan(self, s=None, equity=10000.0, now=None, profile=P, **kw):
        return od.make_plan(s or sig(), profile, L, BID, ASK, equity, now or self.NOW)

    def test_every_required_field_is_present(self):
        p = self.plan()
        for k in ("symbol", "direction", "order_type", "entry", "stop", "tp1", "tp2", "tp3", "risk_currency", "net_reward_currency", "rr_net",
                  "lots", "expires_at", "signal_ts", "strategy", "regime", "invalidation", "targets_plan", "broker_errors"):
            self.assertIn(k, p)
        self.assertEqual((p["direction"], p["order_type"], p["kind"]), ("LONG", "BUY STOP", "stop"))

    def test_prices_are_snapped_to_the_safe_side_of_the_tick(self):
        p = self.plan()
        self.assertEqual(p["entry"], 2010.01)                        # a buy stop rounds UP, never into the market
        self.assertEqual(p["stop"], 1999.99)                         # a long stop rounds DOWN, never tighter
        s = self.plan(sig("SELL LIMIT", price=2006.004, stop=2013.996, targets=(1995.0, 1985.0)))
        self.assertEqual((s["entry"], s["stop"]), (2006.01, 2014.0))
        b = self.plan(sig("BUY LIMIT", price=1995.006, stop=1987.996, targets=(2005.0, 2015.0)))
        self.assertEqual((b["entry"], b["stop"]), (1995.0, 1987.99))

    def test_risk_is_sized_to_the_per_setup_limit_after_costs(self):
        p = self.plan()
        self.assertLessEqual(p["risk_currency"], 10000 * 0.5 / 100 + 1e-6)
        self.assertGreater(p["risk_currency"], 10000 * 0.5 / 100 * 0.7)
        self.assertTrue(p["fits"])
        self.assertAlmostEqual(p["lots"] / 0.01, round(p["lots"] / 0.01), places=6)

    def test_net_reward_is_after_costs(self):
        p = self.plan()
        self.assertLess(p["rr_net"], p["rr_gross"])
        self.assertGreater(p["net_reward_currency"], 0)
        self.assertAlmostEqual(p["cost_per_unit"], 0.30 + 2 * 0.10)

    def test_a_small_account_gets_a_stated_problem_not_a_widened_stop(self):
        p = self.plan(equity=30.0)
        self.assertFalse(p["fits"])
        self.assertIn("minimum lot", p["size_problem"])
        self.assertIsNone(p["targets_plan"])
        self.assertEqual(p["stop"], 1999.99)                         # the stop is never widened to make the size fit

    def test_expiry_and_staleness(self):
        p = self.plan()
        self.assertEqual(p["expires_at"], 1_700_000_000 + 8 * 3600)
        self.assertFalse(p["stale"])
        self.assertTrue(self.plan(now=1_700_000_000 + 9 * 3600)["stale"])

    def test_broker_rule_violations_travel_with_the_plan(self):
        near = self.plan(sig(price=2000.40, stop=1990.0))             # a buy stop only 0.10 above the Ask
        self.assertTrue(any("BUY STOP must be at least" in e for e in near["broker_errors"]))
        self.assertEqual(self.plan()["broker_errors"], [])

    def test_multi_target_mode_follows_the_account(self):
        big = self.plan(equity=50000.0)                              # about 0.23 lots: enough for three legs of the minimum lot
        self.assertEqual(big["targets_plan"]["mode"], "split_orders")
        self.assertAlmostEqual(sum(l["lots"] for l in big["targets_plan"]["legs"]), big["lots"])
        small = self.plan()                                          # about 0.04 lots: too small to split three ways
        self.assertEqual((small["targets_plan"]["mode"], small["targets_plan"]["available"]), ("client_side_partial_exits", False))
        net = self.plan(equity=50000.0, profile=dict(P, account_mode="netting"))
        self.assertEqual(net["targets_plan"]["mode"], "client_side_partial_exits")
        self.assertFalse(net["targets_plan"]["available"])

    def test_up_to_three_targets_only(self):
        p = self.plan(sig(targets=(2015.0, 2020.0, 2030.0, 2040.0, 2050.0)))
        self.assertEqual([p["tp1"], p["tp2"], p["tp3"]], [2015.0, 2020.0, 2030.0])

    def test_short_plans(self):
        p = self.plan(sig("SELL STOP", price=1989.996, stop=2000.004, targets=(1975.0, 1962.0)))
        self.assertEqual(p["direction"], "SHORT")
        self.assertEqual(p["entry"], 1989.99)                         # a sell stop rounds DOWN
        self.assertEqual(p["stop"], 2000.01)                          # a short stop rounds UP
        self.assertIsNone(p["tp3"])
        self.assertEqual(p["broker_errors"], [])


def plan_for(i, order_type="BUY STOP", risk=5.0):
    p = od.make_plan(sig(order_type, i=i), P, L, BID, ASK, 10000.0, 1_700_000_100)
    p["risk_currency"] = risk
    return p


class AccountRisk(unittest.TestCase):
    def ar(self, plans, positions=(), resting=(), equity=10000.0, known=True, groups=None, realised=0.0, limits=None):
        return od.account_risk(equity, known, list(positions), list(resting), plans, limits or L, 2000.0, realised, groups)

    def test_clean_case(self):
        r = self.ar([plan_for(1, risk=40)])
        self.assertTrue(r["allowed"], r["reasons"])
        self.assertAlmostEqual(r["budget"], 100.0)
        self.assertAlmostEqual(r["remaining"], 60.0)

    def test_missing_equity_blocks(self):
        r = self.ar([plan_for(1)], known=False)
        self.assertFalse(r["allowed"])
        self.assertIn("essential risk information is missing", " ".join(r["reasons"]))
        self.assertFalse(self.ar([plan_for(1)], equity=0)["allowed"])

    def test_aggregate_limit_counts_open_resting_and_new_risk(self):
        pos = [dict(id="p", side="long", units=1.0, entry=2000.0, stop=1970.0)]               # 30 open
        rest = [dict(order_type="BUY LIMIT", side="long", units=1.0, price=1990.0, stop=1970.0)]  # 20 resting
        self.assertTrue(self.ar([plan_for(1, risk=40)], pos, rest)["allowed"])                 # 90 of 100
        r = self.ar([plan_for(1, risk=60)], pos, rest)
        self.assertFalse(r["allowed"])
        self.assertIn("aggregate limit", " ".join(r["reasons"]))
        self.assertAlmostEqual(r["planned_total"], 110.0)

    def test_oco_counts_only_the_larger_leg(self):
        buy, sell = plan_for(1, "BUY STOP", 50), plan_for(2, "SELL STOP", 45)
        without = self.ar([buy, sell])
        self.assertAlmostEqual(without["plan_risk"], 95.0)
        linked = self.ar([buy, sell], groups=[[buy["signal_id"], sell["signal_id"]]])
        self.assertAlmostEqual(linked["plan_risk"], 50.0)
        self.assertTrue(linked["allowed"], linked["reasons"])

    def test_both_sides_without_oco_is_blocked_with_the_scenario(self):
        r = self.ar([plan_for(1, "BUY STOP", 30), plan_for(2, "SELL STOP", 30)])
        self.assertFalse(r["allowed"])
        self.assertIn("no OCO link", " ".join(r["reasons"]))
        sc = [s for s in r["scenarios"] if "both trigger" in s["label"]][0]
        self.assertAlmostEqual(sc["worst_case_loss"], 60.0)
        self.assertFalse(sc["covered_by_oco"])

    def test_the_oco_note_says_it_is_simulator_only(self):
        buy, sell = plan_for(1, "BUY STOP", 30), plan_for(2, "SELL STOP", 30)
        r = self.ar([buy, sell], groups=[[buy["signal_id"], sell["signal_id"]]])
        sc = [s for s in r["scenarios"] if "both trigger" in s["label"]][0]
        self.assertTrue(sc["covered_by_oco"])
        self.assertIn("paper simulator only", sc["note"])

    def test_the_oco_requirement_can_be_switched_off(self):
        r = self.ar([plan_for(1, "BUY STOP", 30), plan_for(2, "SELL STOP", 30)], limits=dict(L, require_oco_for_opposing=False, max_aggregate_risk_pct=2))
        self.assertTrue(r["allowed"], r["reasons"])

    def test_daily_lockout(self):
        r = self.ar([plan_for(1, risk=10)], realised=-160.0)
        self.assertFalse(r["allowed"])
        self.assertTrue(r["lockout"])
        self.assertIn("daily loss lockout", " ".join(r["reasons"]))
        self.assertTrue(self.ar([plan_for(1, risk=10)], realised=-100.0)["allowed"])

    def test_unprotected_orders_and_positions_block(self):
        r = self.ar([plan_for(1)], resting=[dict(order_type="BUY LIMIT", side="long", units=1.0, price=1990.0, stop=None)])
        self.assertIn("no stop loss", " ".join(r["reasons"]))
        r2 = self.ar([plan_for(1)], positions=[dict(id="p", side="long", units=1.0, entry=2000.0, stop=None)])
        self.assertIn("open position(s) have no stop", " ".join(r2["reasons"]))

    def test_per_setup_limit_is_flagged(self):
        r = self.ar([plan_for(1, risk=80)])
        self.assertTrue(any("per-setup limit" in e for e in r["effects"][plan_for(1)["signal_id"]]))

    def test_effects_on_existing_exposure(self):
        longpos = [dict(id="p", side="long", units=1.0, entry=2000.0, stop=1980.0)]
        buy, sell = plan_for(1, "BUY STOP", 10), plan_for(2, "SELL STOP", 10)
        self.assertIn("increases current long exposure", self.ar([buy], longpos)["effects"][buy["signal_id"]][-1])
        e = self.ar([sell], longpos)["effects"][sell["signal_id"]][-1]
        self.assertTrue("reduces" in e or "reverses" in e, e)
        self.assertIn("opens a new long exposure", self.ar([buy])["effects"][buy["signal_id"]][-1])
        flat = [dict(id="a", side="long", units=1.0, entry=2000.0, stop=1980.0), dict(id="b", side="short", units=1.0, entry=2000.0, stop=2020.0)]
        self.assertIn("opposing hedge", self.ar([buy], flat)["effects"][buy["signal_id"]][-1])

    def test_margin_note_only_with_leverage(self):
        buy = plan_for(1)
        eff = od.account_risk(10000.0, True, [], [], [buy], L, 2000.0, 0.0, None, None, 100)["effects"][buy["signal_id"]]
        self.assertTrue(any("margin" in e for e in eff))
        self.assertFalse(any("margin" in e for e in self.ar([buy])["effects"][buy["signal_id"]]))

    def test_duplicates(self):
        p = plan_for(1)
        near = dict(order_type="BUY STOP", price=p["entry"] + 1.0)
        far = dict(order_type="BUY STOP", price=p["entry"] + 20.0)
        other = dict(order_type="SELL STOP", price=p["entry"])
        self.assertEqual(od.duplicates(p, [near, far, other], 2000.0, 0.15), [near])
        self.assertEqual(od.duplicates(p, [], 2000.0, 0.15), [])

    def test_no_order_is_ever_sent(self):
        with open(od.__file__, encoding="utf-8") as f:
            src = f.read()
        for w in ("urllib", "socket", "subprocess", "MetaTrader5", "order_send", "import requests"):
            self.assertNotIn(w, src)


if __name__ == "__main__":
    unittest.main()
