"""Gold Trading Command Centre: hard gates, quote handling, news status, confirmation, plan states. SYNTHETIC inputs only."""
import json
import os
import shutil
import tempfile
import unittest

from _load import load

cc = load("webui", "command_centre")
orders = load("webui", "orders")

NOW = 1_800_000_000
PROFILE = dict(orders.DEFAULT_PROFILE)
LIMITS = dict(orders.DEFAULT_LIMITS, **cc.CC_LIMITS)


def quote(status="ok", bid=2000.0, ask=2000.3):
    return dict(status=status, bid=bid, ask=ask, spread=ask - bid, source="test", time="x", age_sec=1, message="Quote is fresh." if status == "ok" else "no quote")


def cand(side="long", kind="pullback"):
    return dict(name="LONG on a pullback", side=side, kind=kind, tf="1h", entry=2000.0, stop=1990.0, entry_zone=[1998.0, 2001.0], dist_atr=0.5, score=7, grade="A",
                targets=[dict(price=2015.0), dict(price=2025.0), dict(price=2040.0)], cancel="Cancel if a 1h candle closes below 1990.")


def facts(**kw):
    c = cc_base()
    c.update(kw)
    return c


def cc_base():
    plan = dict(order_type="BUY LIMIT", entry=2000.0, stop=1990.0, tp1=2015.0, tp2=2025.0, tp3=2040.0, spread=0.3, rr_net=1.4, fits=True, lots=0.2, max_loss=50.0, broker_errors=[])
    return dict(data=dict(ok=True, message="Research bars are fresh."), quote=quote(), session=dict(session="london", label="London"),
                news=dict(status="CLEAR", message="No high-impact event inside the block window."), h4="bullish", h1="bullish", candidate=cand(),
                evidence=dict(grade="A", reason="validated"), confirmed=(True, "zone touched and closed back above it"), plan=plan, account=dict(allowed=True, reasons=[]),
                duplicate=False, limits=LIMITS)


def gate(out, gid):
    return next(g for g in out["gates"] if g["id"] == gid)


class Decision(unittest.TestCase):
    def test_everything_passing_gives_one_directional_decision(self):
        out = cc.decide(facts())
        self.assertEqual(out["decision"], "BUY")
        out = cc.decide(facts(candidate=cand("short"), h1="bearish", h4="bearish"))
        self.assertEqual(out["decision"], "SELL")

    def test_each_hard_gate_alone_forces_no_trade(self):
        cases = {"data": dict(data=dict(ok=False, message="stale")), "quote": dict(quote=quote("missing")), "session": dict(session=dict(session="closed", label="Market closed")),
                 "news": dict(news=dict(status="BLOCKED", message="x")), "evidence": dict(evidence=dict(grade="B", reason="not enough")),
                 "account": dict(account=dict(allowed=False, reasons=["limit"])), "duplicate": dict(duplicate=True)}
        for gid, kw in cases.items():
            out = cc.decide(facts(**kw))
            self.assertEqual(out["decision"], "NO TRADE", gid)
            self.assertIn(gid, out["blocked_by"])

    def test_unknown_news_is_never_treated_as_safe(self):
        out = cc.decide(facts(news=dict(status="UNKNOWN", message="NEWS STATUS UNKNOWN")))
        self.assertEqual(out["decision"], "NO TRADE")
        self.assertEqual(gate(out, "news")["status"], "unknown")

    def test_soft_gates_give_wait_only_when_every_hard_gate_passes(self):
        out = cc.decide(facts(confirmed=(False, "waiting")))
        self.assertEqual(out["decision"], "WAIT")
        out = cc.decide(facts(h4="bearish", h1="bullish"))
        self.assertEqual(out["decision"], "WAIT")
        out = cc.decide(facts(confirmed=(False, "waiting"), quote=quote("missing")))
        self.assertEqual(out["decision"], "NO TRADE")

    def test_no_candidate_is_wait_not_a_forced_opinion(self):
        out = cc.decide(facts(candidate=None, plan=None, evidence=None))
        self.assertEqual(out["decision"], "WAIT")

    def test_counter_trend_candidate_waits(self):
        out = cc.decide(facts(candidate=cand("short"), h1="bullish", h4="bullish"))
        self.assertEqual(out["decision"], "WAIT")

    def test_execution_gate_catches_spread_cost_and_broker_rules(self):
        p = dict(facts()["plan"], spread=5.0)
        self.assertEqual(gate(cc.decide(facts(plan=p)), "execution")["status"], "fail")
        p = dict(facts()["plan"], rr_net=0.5)
        self.assertEqual(gate(cc.decide(facts(plan=p)), "execution")["status"], "fail")
        p = dict(facts()["plan"], broker_errors=["stop loss must be at least 0.30 below the order price"])
        self.assertEqual(cc.decide(facts(plan=p))["decision"], "NO TRADE")
        p = dict(facts()["plan"], fits=False, size_problem="too small")
        self.assertEqual(cc.decide(facts(plan=p))["decision"], "NO TRADE")

    def test_a_high_score_cannot_bypass_a_gate(self):
        c = cand()
        c["score"], c["grade"] = 99, "A"
        out = cc.decide(facts(candidate=c, evidence=dict(grade="C", reason="unvalidated")))
        self.assertEqual(out["decision"], "NO TRADE")

    def test_reasons_are_at_most_three(self):
        out = cc.decide(facts(data=dict(ok=False, message="a"), quote=quote("missing"), news=dict(status="BLOCKED", message="c"), evidence=dict(grade="C", reason="d")))
        self.assertLessEqual(len(out["reasons"]), 3)


class QuoteFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "q.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_missing_stale_invalid_and_fresh(self):
        self.assertEqual(cc.load_quote(NOW, self.p)["status"], "missing")
        cc.save_quote(2000.0, 2000.3, now=NOW, path=self.p)
        self.assertEqual(cc.load_quote(NOW + 5, self.p)["status"], "ok")
        self.assertEqual(cc.load_quote(NOW + 31, self.p)["status"], "stale")
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump(dict(bid=2001.0, ask=2000.0, time=NOW, source="x"), f)
        self.assertEqual(cc.load_quote(NOW, self.p)["status"], "invalid")
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("junk")
        self.assertEqual(cc.load_quote(NOW, self.p)["status"], "missing")

    def test_a_quote_from_the_future_is_invalid(self):
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump(dict(bid=2000.0, ask=2000.3, time=NOW + 3600, source="x"), f)
        self.assertEqual(cc.load_quote(NOW, self.p)["status"], "invalid")

    def test_save_rejects_nonsense(self):
        for bid, ask in ((0, 1), (2001, 2000), ("a", 1), (2000, 2100), (float("nan"), 1)):
            with self.assertRaises(ValueError):
                cc.save_quote(bid, ask, path=self.p)


class News(unittest.TestCase):
    def ev(self, ts, imp="high", **kw):
        e = dict(ts=ts, utc="2027-01-15T13:30:00+00:00", name="US CPI", type="CPI", importance=imp, source="test", reliability="manual", actual=None, forecast=None, previous=None, observed_at="x")
        e.update(kw)
        return e

    def test_no_future_events_means_unknown_never_clear(self):
        p = cc.news_panel([self.ev(NOW - 86400 * 3)], NOW)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(cc.news_panel([], NOW)["status"], "UNKNOWN")

    def test_block_window_and_countdown(self):
        p = cc.news_panel([self.ev(NOW + 15 * 60)], NOW)
        self.assertEqual((p["status"], p["next"]["minutes"]), ("BLOCKED", 15))
        p = cc.news_panel([self.ev(NOW + 5 * 3600)], NOW)
        self.assertEqual(p["status"], "CLEAR")

    def test_figures_are_shown_only_when_stored(self):
        p = cc.news_panel([self.ev(NOW + 3600)], NOW)
        self.assertIsNone(p["next"]["forecast"])
        self.assertIsNone(p["next"]["actual"])
        p = cc.news_panel([self.ev(NOW + 3600, forecast=3.1, previous=3.0)], NOW)
        self.assertEqual((p["next"]["forecast"], p["next"]["actual"]), (3.1, None))


class Confirmation(unittest.TestCase):
    def bars(self, closes_highs_lows):
        t0 = NOW - 10 * 900
        return [(t0 + i * 900, h, l, c, c) for i, (c, h, l) in enumerate(closes_highs_lows)]

    def test_pullback_needs_a_touch_and_a_close_back_beyond_the_zone(self):
        p = cand()
        ok, _ = cc.confirmation(p, self.bars([(2010, 2012, 2009)] * 6 + [(1999, 2000, 1997), (2000, 2002, 1999), (2002.5, 2003, 2000)]), NOW)
        self.assertTrue(ok)
        ok, _ = cc.confirmation(p, self.bars([(2010, 2012, 2009)] * 9), NOW)
        self.assertFalse(ok)

    def test_breakout_needs_a_close_beyond_the_entry(self):
        p = cand(kind="breakout")
        self.assertTrue(cc.confirmation(p, self.bars([(1995, 1996, 1994)] * 7 + [(2001, 2002, 1999)] * 2), NOW)[0])
        self.assertFalse(cc.confirmation(p, self.bars([(1995, 2003, 1994)] * 9), NOW)[0])    # a wick through the level does not count

    def test_forming_bar_is_ignored_and_thin_history_does_not_confirm(self):
        self.assertFalse(cc.confirmation(cand(), [], NOW)[0])


class PlanLog(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "plans.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def plan(self):
        return dict(decision="BUY", entry=2000.0, stop=1990.0, expires_at=NOW + 3600, status="Awaiting confirmation", strategy="x")

    def test_record_once_and_not_twice(self):
        self.assertTrue(cc.record_plan(self.plan(), NOW, self.p))
        self.assertFalse(cc.record_plan(self.plan(), NOW + 60, self.p))
        self.assertEqual(len(cc.load_plans(self.p)), 1)

    def test_states_move_to_expired_or_invalidated_only(self):
        cc.record_plan(self.plan(), NOW, self.p)
        plans = cc.load_plans(self.p)
        self.assertEqual(cc.refresh_states(plans, 1995.0, NOW + 60), [])
        self.assertEqual(plans[0]["status"], "Awaiting confirmation")
        cc.refresh_states(plans, 1989.0, NOW + 120)
        self.assertEqual(plans[0]["status"], "Invalidated")
        plans2 = [dict(self.plan(), id="a")]
        cc.refresh_states(plans2, 2000.0, NOW + 7200)
        self.assertEqual(plans2[0]["status"], "Expired")

    def test_sell_plan_invalidates_above_the_stop(self):
        p = dict(self.plan(), decision="SELL", entry=2000.0, stop=2010.0)
        cc.refresh_states([p], 2011.0, NOW + 60)
        self.assertEqual(p["status"], "Invalidated")


class Sizing(unittest.TestCase):
    def test_no_numbers_without_a_fresh_quote(self):
        self.assertIsNone(cc.size_plan(cand(), quote("missing"), 10000.0, PROFILE, LIMITS))

    def test_lots_follow_the_quarter_percent_risk_and_broker_contract(self):
        pl = cc.size_plan(cand(), quote(), 10000.0, PROFILE, LIMITS)
        self.assertEqual(pl["order_type"], "BUY LIMIT")
        self.assertTrue(pl["fits"])
        self.assertLessEqual(pl["max_loss"], 10000.0 * 0.25 / 100 + 0.01)
        self.assertEqual(pl["entry"], 2000.0)

    def test_unknown_equity_means_no_size(self):
        pl = cc.size_plan(cand(), quote(), None, PROFILE, LIMITS)
        self.assertFalse(pl["fits"])
        self.assertIsNone(pl["lots"])

    def test_breakout_uses_stop_orders(self):
        self.assertEqual(cc.size_plan(cand("short", "breakout"), quote(), 10000.0, PROFILE, LIMITS)["order_type"], "SELL STOP")


class CoreCandidate(unittest.TestCase):
    def core(self, eligible=False, side="long", exit_="2R", edge=None):
        return dict(strategy="A1", name="Trend continuation: Donchian 20 breakout, 2R exit", family="A", side=side, exit=exit_, stop=1960.0, ref_close=2000.0, atr=20.0, eligible=eligible, opp_edge=edge)

    RESULT = dict(hypotheses=[dict(id="A1", decision="INSUFFICIENT SAMPLE", reasons=["only 42 completed trades"], development=dict(trades=42, mean_r=0.27, band_corrected=[-0.4, 0.9]))], eligible=[])

    def test_not_eligible_means_grade_c_and_the_gate_blocks(self):
        c = cc.adapt_core(self.core(), self.RESULT)
        self.assertEqual(c["evidence"]["grade"], "C")
        self.assertEqual(c["evidence"]["quality"], "Insufficient")
        self.assertIn("INSUFFICIENT SAMPLE", c["evidence"]["reason"])
        out = cc.decide(facts(candidate=c, evidence=c["evidence"]))
        self.assertEqual(out["decision"], "NO TRADE")
        self.assertIn("evidence", out["blocked_by"])

    def test_eligible_means_grade_a(self):
        c = cc.adapt_core(self.core(eligible=True), self.RESULT)
        self.assertEqual((c["evidence"]["grade"], c["evidence"]["quality"]), ("A", "Validated"))

    def test_distances_are_offsets_not_gcf_prices(self):
        c = cc.adapt_core(self.core(), self.RESULT)
        self.assertEqual((c["stop_distance"], c["tp1_distance"]), (40.0, 80.0))
        c = cc.adapt_core(self.core(exit_="structure", edge=2100.0), self.RESULT)
        self.assertEqual(c["tp1_distance"], 100.0)
        self.assertIsNone(cc.adapt_core(None, self.RESULT))

    def test_market_plan_uses_the_executable_side_of_the_quote(self):
        c = cc.adapt_core(self.core(eligible=True), self.RESULT)
        long = cc.size_plan(c, quote(bid=3000.0, ask=3000.4), 10000.0, PROFILE, LIMITS)
        self.assertEqual((long["order_type"], long["entry"]), ("BUY MARKET", 3000.4))
        self.assertAlmostEqual(long["stop"], 2960.4)           # the 40.0 offset from the entry, not the GC=F stop of 1960
        self.assertAlmostEqual(long["tp1"], 3080.4)
        short = cc.size_plan(cc.adapt_core(self.core(side="short"), self.RESULT), quote(bid=3000.0, ask=3000.4), 10000.0, PROFILE, LIMITS)
        self.assertEqual((short["order_type"], short["entry"]), ("SELL MARKET", 3000.0))
        self.assertAlmostEqual(short["stop"], 3040.0)
        self.assertLessEqual(long["max_loss"], 10000.0 * 0.0025 + 0.01)

    def test_no_quote_no_market_plan(self):
        self.assertIsNone(cc.size_plan(cc.adapt_core(self.core(), self.RESULT), quote("stale"), 10000.0, PROFILE, LIMITS))


class Library(unittest.TestCase):
    def test_every_strategy_has_the_required_fields_and_none_claims_validation(self):
        for s in cc.LIBRARY:
            for k in ("id", "name", "author", "original", "adaptation", "sessions", "regime", "status", "evidence", "note"):
                self.assertIn(k, s)
            self.assertEqual(s["evidence"], "Insufficient")
            self.assertNotIn("VALIDATED", s["status"].upper())
        self.assertEqual([s["id"] for s in cc.LIBRARY], list("ABCDEF"))


class FullBuild(unittest.TestCase):
    """The whole document from injected bars, so no network is touched."""

    def test_light_mode_has_no_decision_and_never_executes(self):
        import random
        rnd = random.Random(1)
        def mk(step, n):
            out, px = [], 2000.0
            t0 = (NOW // step - n) * step
            for i in range(n):
                op = px
                px += rnd.uniform(-2, 2)
                out.append((t0 + i * step, max(op, px) + 0.5, min(op, px) - 0.5, px, op))
            return out
        bars = {"15m": mk(900, 300), "60m": mk(3600, 400), "1d": mk(86400, 100), "1wk": mk(604800, 60)}
        d = cc.build(now=NOW, light=True, bars=bars, events=[], quote=quote("missing"))
        self.assertTrue(d["light"])
        self.assertEqual(d["mode"], "SIGNAL ONLY")
        self.assertEqual(d["news"]["status"], "UNKNOWN")
        self.assertNotIn("final", d)


if __name__ == "__main__":
    unittest.main()
