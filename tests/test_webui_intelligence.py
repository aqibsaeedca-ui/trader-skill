"""Dashboard assembly: the NO TRADE gates, evidence labelling and honesty. All bars and records are SYNTHETIC."""
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load

it = load("webui", "intelligence")
bt = load("webui", "backtester")
ml = load("webui", "ml")
rs = load("webui", "research")

STEP = 3600
T0 = 1_700_006_400


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def flat(n, price=100.0, rng=2.0, t0=T0):
    return [bar(t0 + i * STEP, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


def breakout_scenario():
    bars = flat(130)
    t = bars[-1][0]
    bars.append(bar(t + STEP, 100.5, 103.2, 100.4, 103.0))
    bars.append(bar(t + 2 * STEP, 101.6, 102.6, 101.3, 102.4))
    return bars


def noise(n, seed=3):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, 1.0)
        out.append(bar(T0 + i * STEP, o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        p = c
    return out


def now_after(bars):
    return bars[-1][0] + STEP + 30


STATUS_OK = "VALIDATED (research, past data only)"


def fake_record(validated=("breakout_retest",), code=None, params=None):
    code = code or bt.code_version()
    strategies = {}
    for name in ("breakout_retest", "trend_continuation", "liquidity_sweep", "range"):
        ok = name in validated
        strategies[name] = dict(status=STATUS_OK if ok else "NOT VALIDATED", reasons=[] if ok else ["synthetic: not validated"],
                                oos=dict(n=150, expectancy=0.2, ci95=[0.05, 0.35]), holdout=dict(summary=dict(n=40, expectancy=0.1), attempts=1, params=params or {}),
                                chosen=params or {}, equity=[dict(t=1, r=0.1)], stress=[dict(multiple=2.0, expectancy=0.05)], by_regime={}, by_session={},
                                by_side={}, halves=[0.1, 0.3], bootstrap=dict(lo=0.05), dsr=dict(dsr=0.97), news_filter=dict(verdict="n/a"),
                                rejections={"false breakout (closed back inside the range)": 3}, signals_total=10, trades_total=7, holm_p=0.04)
    return dict(symbol="GC=F", interval="60m", created="2026-01-01T00:00:00Z", code_version=code, seconds=1.0,
                data=dict(source="synthetic", rows=1, sha256="a" * 64), costs=dict(spread=0.3, slippage=0.1, commission_per_unit=0.0, source="test"),
                walk_forward=dict(holdout_frac=0.2), strategies=strategies, ml=dict(decision=dict(status="REJECTED", reasons=["synthetic"])))


def portfolio(equity=1_000_000.0, **kw):
    s = dict(equity=equity, positions=[], pending=[], limits=dict(max_open_risk_pct=2, daily_loss_limit_pct=3, max_positions=3))
    s.update(kw)
    return s


class Builder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, bars=None, validation=None, pf_state=None, **kw):
        bars = bars or breakout_scenario()
        args = dict(symbol="GC=F", interval="60m", now=now_after(bars), bars={"60m": bars}, validation=validation if validation is not None else {},
                    portfolio_state=pf_state or portfolio(), events=[], model_dir=os.path.join(self.dir, "m"), journal_entries=[])
        args.update(kw)
        return it.build(**args)

    def val(self, **kw):
        return {"GC=F|60m": fake_record(**kw)}

    def test_nothing_validated_means_no_trade_with_reasons(self):
        d = self.build()
        self.assertEqual(d["decision"]["action"], "NO TRADE")
        self.assertTrue(any("not validated" in r for r in d["decision"]["reasons"]))
        for a in d["assessments"]:
            self.assertEqual(a["decision"], "NO TRADE")
            self.assertFalse(a["evidence"]["validated"])
        self.assertFalse(d["sample_data"])

    def test_a_candidate_is_shown_but_labelled_when_the_strategy_is_unvalidated(self):
        d = self.build()
        a = next(x for x in d["assessments"] if x["strategy"] == "breakout_retest")
        self.assertIsNotNone(a["candidate"])
        self.assertEqual(a["candidate"]["side"], "long")
        self.assertEqual(a["decision"], "NO TRADE")
        self.assertIsNone(a["candidate"]["expected_r"])                      # never shown without validation
        self.assertTrue(any("expected return is not shown" in i for i in a["info"]))
        self.assertNotIn("probability", a["candidate"])
        self.assertTrue(any("no calibrated probability" in i for i in a["info"]))

    def test_all_gates_passing_gives_a_trade(self):
        d = self.build(validation=self.val())
        self.assertEqual(d["decision"]["action"], "LONG")
        self.assertEqual(d["decision"]["strategy"], "breakout_retest")
        a = next(x for x in d["assessments"] if x["strategy"] == "breakout_retest")
        self.assertEqual(a["decision"], "LONG")
        self.assertEqual(a["blocking"], [])
        c = a["candidate"]
        self.assertLess(c["stop"], c["entry"])
        self.assertGreater(c["rr_net"], 0)
        self.assertLess(c["rr_net"], c["rr_gross"])                          # costs always shrink the ratio
        self.assertTrue(c["sizing"]["fits"])
        self.assertAlmostEqual(c["expected_r"], 0.2)
        self.assertEqual(a["evidence"]["oos_n"], 150)

    def test_a_validated_strategy_still_needs_a_trigger(self):
        d = self.build(bars=flat(140), validation=self.val())
        self.assertEqual(d["decision"]["action"], "NO TRADE")
        self.assertTrue(any("no confirmed trigger" in r for r in d["decision"]["reasons"]))

    def test_changed_code_invalidates_old_evidence(self):
        d = self.build(validation=self.val(code="0" * 12))
        a = next(x for x in d["assessments"] if x["strategy"] == "breakout_retest")
        self.assertTrue(a["evidence"]["status"].startswith("STALE"))
        self.assertFalse(a["evidence"]["validated"])
        self.assertEqual(d["decision"]["action"], "NO TRADE")

    def test_daily_loss_lockout_overrides_validation(self):
        d = self.build(validation=self.val(), pf_state=portfolio(realised_today=-40000))
        self.assertEqual(d["decision"]["action"], "NO TRADE")
        self.assertIn("daily loss limit reached", " ".join(d["decision"]["reasons"]))
        self.assertEqual(d["risk_monitor"]["status"], "LIMIT")

    def test_a_small_account_cannot_take_a_full_contract(self):
        d = self.build(validation=self.val(), pf_state=portfolio(equity=2700))
        a = next(x for x in d["assessments"] if x["strategy"] == "breakout_retest")
        self.assertEqual(a["decision"], "NO TRADE")
        self.assertTrue(any("minimum lot" in b for b in a["blocking"]))
        self.assertFalse(a["candidate"]["sizing"]["fits"])

    def test_open_hedge_blocks_a_new_trade(self):
        pos = [dict(id="p", symbol="GC=F", side="short", units=1, entry=100, stop=110)]
        d = self.build(validation=self.val(), pf_state=portfolio(positions=pos))
        self.assertEqual(d["decision"]["action"], "NO TRADE")
        self.assertTrue(any("hedge" in r for r in d["decision"]["reasons"]))

    def test_the_forming_bar_is_excluded_from_signals(self):
        bars = breakout_scenario()
        d = self.build(bars=bars, validation=self.val(), now=bars[-1][0] + 600)       # last bar is 10 minutes old
        self.assertTrue(d["overview"]["forming_bar_excluded"])
        self.assertLess(d["overview"]["last_closed_bar"], d["overview"]["quote_time"])
        self.assertEqual(d["decision"]["action"], "NO TRADE")                            # the retest bar is not closed yet

    def test_overview_fields(self):
        d = self.build()
        o = d["overview"]
        self.assertIn("Yahoo", o["provider"])
        self.assertIsNone(o["bid"])
        self.assertIn("not available", o["bid_ask_note"])
        self.assertIn(o["alignment"]["label"], ("aligned bullish", "aligned bearish", "mixed", "unknown"))
        self.assertIn("support", o["levels"])
        for k in ("bias", "last_bos", "sweeps", "fvg", "order_blocks"):
            self.assertIn(k, o["structure"])
        self.assertEqual(o["data_quality"]["series"][0]["interval"], "60m")

    def test_bid_ask_are_derived_only_from_a_real_spread_series(self):
        bars = breakout_scenario()
        d = self.build(bars=bars, spread={bars[-1][0]: 0.4})
        self.assertAlmostEqual(d["overview"]["ask"] - d["overview"]["bid"], 0.4)
        self.assertTrue(d["spread_analysis"]["available"])
        self.assertFalse(self.build()["spread_analysis"]["available"])

    def test_performance_is_empty_without_a_research_run(self):
        d = self.build()
        self.assertFalse(d["performance"]["available"])
        self.assertIn("No research run", d["performance"]["message"])

    def test_performance_comes_only_from_the_stored_record(self):
        d = self.build(validation=self.val())
        p = d["performance"]
        self.assertTrue(p["available"])
        self.assertEqual(p["strategies"]["breakout_retest"]["status"], STATUS_OK)
        self.assertEqual(p["strategies"]["breakout_retest"]["rejections"], {"false breakout (closed back inside the range)": 3})
        self.assertEqual(p["costs"]["source"], "test")

    def test_probability_appears_only_with_an_accepted_model(self):
        rnd = random.Random(7)
        d_feat = len(ml.FEATURES)
        rows = []
        for i in range(2400):
            x = [rnd.gauss(0, 1) for _ in range(d_feat)]
            y = 1 if 2.0 * x[1] + rnd.gauss(0, 0.4) > 0.4 else 0
            rows.append(dict(i=i, t=i, side=1, x=x, y=y, r_gross=1.5 if y else -1.0, r_net=(1.5 if y else -1.0) - 0.1, bars=5, mae_r=0.5, how="x", cost_r=0.1))
        ev = ml.evaluate_rows(rows, 10, gbm=dict(n_estimators=8, depth=2, min_leaf=20))
        self.assertTrue(ev["decision"]["status"].startswith("ACCEPTED"))
        body = ml.package(ev, rows, "GC=F", "60m", dict(horizon=10), 7)
        ml.save_model(body, os.path.join(self.dir, "m"))
        d = self.build(validation=self.val())
        a = next(x for x in d["assessments"] if x["strategy"] == "breakout_retest")
        p = a["candidate"]["probability"]
        self.assertTrue(0 < p["p"] < 1)
        self.assertEqual(p["model_id"], body["id"])
        self.assertIn("calibrated chance", p["meaning"])
        self.assertEqual(d["evidence"]["model"]["id"], body["id"])
        if p["p"] < p["threshold"]:
            self.assertEqual(a["decision"], "NO TRADE")

    def test_json_is_safe_and_roundtrips(self):
        d = rs._clean(self.build(validation=self.val()))
        json.dumps(d, allow_nan=False)

    def test_too_few_bars(self):
        with self.assertRaises(SystemExit):
            self.build(bars=flat(60))

    def test_unknown_symbol_is_an_error_not_a_guess(self):
        with self.assertRaises(Exception):
            self.build(symbol="NOPE-USD")

    def test_text_render_states_the_decision(self):
        text = it.render(self.build())
        self.assertIn("DECISION: NO TRADE", text)
        self.assertIn("not predictions", text)

    def test_notes_never_promise_profit(self):
        d = self.build(validation=self.val())
        blob = json.dumps(d).lower()
        self.assertNotIn("guaranteed", blob)
        self.assertNotIn("will profit", blob)


class Helpers(unittest.TestCase):
    def test_alignment(self):
        self.assertEqual(it.alignment({"1h": "strong_bull", "4h": "weak_bull", "1d": "strong_bull"})["label"], "aligned bullish")
        self.assertEqual(it.alignment({"1h": "strong_bear", "1d": "weak_bear"})["label"], "aligned bearish")
        self.assertEqual(it.alignment({"1h": "strong_bull", "1d": "weak_bear"})["label"], "mixed")
        self.assertEqual(it.alignment({"1h": "sideways", "1d": "strong_bull"})["label"], "mixed")
        self.assertEqual(it.alignment({"1h": "unknown"})["label"], "unknown")
        self.assertEqual(it.alignment({})["label"], "unknown")

    def test_spread_analysis(self):
        a = it.spread_analysis({1: 0.2, 2: 0.4, 3: 0.3, 4: 2.0})
        self.assertTrue(a["available"])
        self.assertEqual(a["worst"], 2.0)
        self.assertFalse(it.spread_analysis(None)["available"])

    def test_performance_none(self):
        self.assertFalse(it.performance(None)["available"])


if __name__ == "__main__":
    unittest.main()
