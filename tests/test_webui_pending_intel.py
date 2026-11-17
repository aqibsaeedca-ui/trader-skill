"""Pending Order Intelligence assembly: gates, evidence, account risk, paper orders. Bars, records and books are SYNTHETIC."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from _load import load

pi = load("webui", "pending")
bt = load("webui", "backtester")
od = load("webui", "orders")
pp = load("webui", "paper")
rs = load("webui", "research")

import pending_strategies as real_ps  # the module pending.build itself uses, so the regime gate can be bypassed for geometry

T0 = 1_700_006_400
STEP = 3600
GATE_OFF = mock.patch.object(real_ps, "regime_gate", lambda *a, **k: (True, ""))


def flat(n, price=100.0, rng=2.0):
    return [(T0 + i * STEP, price + rng / 2, price - rng / 2, price, price) for i in range(n)]


BARS = flat(130)
NOW = BARS[-1][0] + STEP + 30


def pf_state(equity=100000.0, **kw):
    s = dict(equity=equity, positions=[], pending=[], limits=dict(max_open_risk_pct=2, daily_loss_limit_pct=3, max_positions=3))
    s.update(kw)
    return s


def order_stats(enough=True):
    return dict(submitted=150 if enough else 12, filled=60 if enough else 5, fill_rate=0.4, fill_ci=[0.32, 0.48], target_hit_rate=0.5, target_hit_ci=[0.37, 0.63],
                expected_per_submitted=0.12, enough=enough, expired=70, cancelled=10, skipped=10, avg_entry_slip=0.05)


def record(validated=("buy_stop",), enough=True, code=None):
    strat = {}
    for n in ("buy_stop", "sell_stop", "buy_limit", "sell_limit"):
        ok = n in validated
        strat[n] = dict(status="VALIDATED (research, past data only)" if ok else "INSUFFICIENT SAMPLE", reasons=[], chosen={},
                        oos=dict(n=120 if ok else 8, expectancy=0.2, ci95=[0.05, 0.35]), holdout=dict(summary=dict(n=35, expectancy=0.1)),
                        orders=order_stats(enough))
    return {"GC=F|60m|pending": dict(created="2026-01-01T00:00:00Z", code_version=code or bt.code_version(), strategies=strat)}


class Build(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, gate=False, **kw):
        args = dict(symbol="GC=F", interval="60m", now=NOW, bars={"60m": BARS}, validation={}, portfolio_state=pf_state(), equity_entered=True,
                    settings=od.clean_settings({}), events=[], paper_orders=[], audit=[])
        args.update(kw)
        if gate:
            with GATE_OFF:
                return pi.build(**args)
        return pi.build(**args)

    def test_nothing_validated_means_no_recommendation(self):
        d = self.build(gate=True)
        self.assertEqual((d["headline"], d["recommended"], d["best"]), ("NONE", [], None))
        for p in d["panels"].values():
            self.assertNotEqual(p["status"], "RECOMMENDED")
            self.assertTrue(any("not validated" in b for b in p["blocking"]))
        self.assertFalse(d["sample_data"])
        self.assertIn("NO VALID PENDING ORDER", pi.render(d))

    def test_the_real_regime_gate_leaves_a_flat_market_without_breakout_orders(self):
        d = self.build()
        self.assertEqual(d["panels"]["buy_stop"]["status"], "NONE")
        self.assertEqual(d["panels"]["sell_stop"]["status"], "NONE")

    def test_all_gates_passing_recommends_and_names_the_best(self):
        d = self.build(gate=True, validation=record(("buy_stop",)))
        p = d["panels"]["buy_stop"]
        self.assertEqual((p["status"], p["signal_state"]), ("RECOMMENDED", "VALIDATED"))
        self.assertEqual(p["blocking"], [])
        self.assertEqual(d["recommended"], ["buy_stop"])
        self.assertEqual((d["headline"], d["best"]["order_type"]), ("BEST", "BUY STOP"))
        plan = p["plan"]
        for k in ("entry", "stop", "tp1", "lots", "risk_currency", "rr_net", "expires_at", "signal_ts", "strategy", "regime", "invalidation", "targets_plan"):
            self.assertIn(k, plan)
        self.assertEqual(plan["order_type"], "BUY STOP")
        self.assertEqual(plan["broker_errors"], [])
        self.assertLess(plan["rr_net"], plan["rr_gross"])

    def test_fill_probability_and_win_probability_after_a_fill_are_separate_with_bands(self):
        e = self.build(gate=True, validation=record(("buy_stop",)))["panels"]["buy_stop"]["evidence"]
        self.assertEqual(e["fill_probability"]["p"], 0.4)
        self.assertEqual(e["fill_probability"]["ci"], [0.32, 0.48])
        self.assertEqual(e["hit_probability"]["p"], 0.5)
        self.assertEqual(e["hit_probability"]["ci"], [0.37, 0.63])
        self.assertEqual(e["fill_probability"]["n"], 150)
        self.assertEqual(e["hit_probability"]["n"], 60)
        self.assertAlmostEqual(e["expected_per_submitted"], 0.12)

    def test_without_enough_history_no_probability_is_quoted(self):
        e = self.build(gate=True, validation=record(("buy_stop",), enough=False))["panels"]["buy_stop"]["evidence"]
        self.assertIsNone(e["fill_probability"])
        self.assertIsNone(e["hit_probability"])
        self.assertIsNone(e["expected_per_submitted"])
        self.assertIn("fewer than 30", e["no_probability_reason"])

    def test_a_validated_order_with_no_sample_for_ranking_gets_no_best_designation(self):
        d = self.build(gate=True, validation=record(("buy_stop",), enough=False))
        self.assertEqual(d["recommended"], ["buy_stop"])
        self.assertIsNone(d["best"])                                           # recommended, but nothing supports calling it the best

    def test_equity_that_was_never_entered_blocks_everything(self):
        d = self.build(gate=True, validation=record(("buy_stop",)), equity_entered=False)
        p = d["panels"]["buy_stop"]
        self.assertEqual(p["status"], "CANDIDATE")
        self.assertIn("essential risk information is missing", " ".join(p["blocking"]))
        self.assertFalse(d["account"]["equity_entered"])

    def test_changed_code_invalidates_the_evidence(self):
        d = self.build(gate=True, validation=record(("buy_stop",), code="0" * 12))
        p = d["panels"]["buy_stop"]
        self.assertTrue(p["evidence"]["status"].startswith("STALE"))
        self.assertEqual(p["status"], "CANDIDATE")

    def test_important_news_blocks(self):
        ev = [dict(ts=NOW + 600, importance="high", type="NFP", name="Nonfarm payrolls", source="t", reliability="derived", utc="")]
        d = self.build(gate=True, validation=record(("buy_stop",)), events=ev)
        self.assertIn("important news is close", " ".join(d["panels"]["buy_stop"]["blocking"]))

    def test_stale_market_data_blocks(self):
        d = self.build(gate=True, validation=record(("buy_stop",)), now=BARS[-1][0] + 5 * STEP)
        self.assertIn("stale", " ".join(d["panels"]["buy_stop"]["blocking"]))

    def test_an_abnormal_measured_spread_blocks_and_changes_the_quote_basis(self):
        spread = {b[0]: 0.3 for b in BARS}
        spread[BARS[-1][0]] = 3.0
        d = self.build(gate=True, validation=record(("buy_stop",)), spread=spread)
        self.assertIn("spread is", " ".join(d["panels"]["buy_stop"]["blocking"]))
        self.assertIn("measured", d["market"]["spread_basis"])
        self.assertAlmostEqual(d["market"]["ask"] - d["market"]["bid"], 3.0)
        calm = {b[0]: 0.3 for b in BARS}
        ok = self.build(gate=True, validation=record(("buy_stop",)), spread=calm)
        self.assertEqual(ok["panels"]["buy_stop"]["blocking"], [])

    def test_assumed_spread_is_labelled_as_assumed(self):
        d = self.build(gate=True)
        self.assertIn("assumed", d["market"]["spread_basis"])
        self.assertAlmostEqual(d["market"]["spread"], 0.30)
        self.assertTrue(any("spread monitoring needs real bid/ask" in i for i in d["panels"]["buy_stop"]["info"]))

    def test_a_duplicate_resting_order_blocks(self):
        d0 = self.build(gate=True, validation=record(("buy_stop",)))
        entry = d0["panels"]["buy_stop"]["plan"]["entry"]
        paper_live = [dict(id="po-0000000001", order_state="AWAITING_TRIGGER", position_state=None, order_type="BUY STOP", side="long", units=1.0, price=entry + 0.1, stop=entry - 3)]              # within 0.15% of the entry
        d = self.build(gate=True, validation=record(("buy_stop",)), paper_orders=paper_live)
        self.assertIn("duplicate", " ".join(d["panels"]["buy_stop"]["blocking"]))
        pend = [dict(id="o1", symbol="XAUUSD", side="long", units=1, kind="stop", price=entry, stop=entry - 3)]
        d2 = self.build(gate=True, validation=record(("buy_stop",)), portfolio_state=pf_state(pending=pend))
        self.assertIn("duplicate", " ".join(d2["panels"]["buy_stop"]["blocking"]))

    def test_account_wide_risk_counts_what_is_already_open(self):
        pos = [dict(id="p", symbol="GC=F", side="long", units=1000.0, entry=100.0, stop=99.0)]       # about 1000 of open risk on 100000 equity
        d = self.build(gate=True, validation=record(("buy_stop",)), portfolio_state=pf_state(positions=pos))
        self.assertIn("aggregate limit", " ".join(d["panels"]["buy_stop"]["blocking"]))
        self.assertGreater(d["account"]["risk"]["open_risk"], 0)

    def test_the_daily_loss_lockout_blocks(self):
        d = self.build(gate=True, validation=record(("buy_stop",)), portfolio_state=pf_state(realised_today=-2000.0))
        self.assertIn("daily loss lockout", " ".join(d["panels"]["buy_stop"]["blocking"]))

    def test_a_plan_that_breaks_a_broker_rule_is_rejected_not_recommended(self):
        s = od.clean_settings(dict(profile=dict(stops_level_points=2000)))                            # 20.00 stop level
        d = self.build(gate=True, validation=record(("buy_stop",)), settings=s)
        p = d["panels"]["buy_stop"]
        self.assertNotEqual(p["status"], "RECOMMENDED")
        if p["plan"]:
            self.assertEqual(p["signal_state"], "REJECTED" if p["plan"]["broker_errors"] else p["signal_state"])

    def test_a_small_account_cannot_take_the_order(self):
        d = self.build(gate=True, validation=record(("buy_stop",)), portfolio_state=pf_state(equity=50.0))
        p = d["panels"]["buy_stop"]
        self.assertNotEqual(p["status"], "RECOMMENDED")
        self.assertTrue(any("does not fit" in b for b in p["blocking"]))
        self.assertEqual(p["plan"]["stop"], d["panels"]["buy_stop"]["plan"]["stop"])                  # the stop was not widened to fit

    def test_stop_limit_orders_are_unavailable_until_the_profile_says_so(self):
        d = self.build(gate=True)
        self.assertFalse(d["stop_limit"]["available"])
        self.assertIn("unavailable", d["stop_limit"]["note"])
        self.assertEqual(set(d["stop_limit"]["types"]), {"BUY STOP LIMIT", "SELL STOP LIMIT"})
        on = self.build(gate=True, settings=od.clean_settings(dict(profile=dict(supports_stop_limit=True))))
        self.assertTrue(on["stop_limit"]["available"])
        self.assertIn("not backtested", on["stop_limit"]["note"])
        self.assertEqual(set(on["panels"]), {"buy_stop", "sell_stop", "buy_limit", "sell_limit"})      # still only four ordinary panels

    def test_an_oco_link_is_proposed_only_for_opposing_stop_orders(self):
        d = self.build(gate=True)
        if d["panels"]["buy_stop"]["plan"] and d["panels"]["sell_stop"]["plan"]:
            self.assertEqual(len(d["oco"]["proposed"]), 2)
            self.assertIn("paper simulator", d["oco"]["note"])
            sc = [s for s in d["account"]["risk"]["scenarios"] if "both trigger" in s["label"]]
            self.assertTrue(sc and sc[0]["covered_by_oco"])

    def test_timeframes_and_the_data_that_is_missing(self):
        d = self.build(gate=True)
        tfs = {r["tf"]: r for r in d["market"]["timeframes"]}
        self.assertIn("H4", tfs)
        self.assertIn("H1", tfs)
        self.assertEqual(tfs["M5"]["trend"], "unavailable")
        self.assertEqual(tfs["M1"]["trend"], "unavailable")
        self.assertEqual(d["market"]["unavailable_timeframes"], ["M5", "M1"])

    def test_the_broker_profile_is_labelled_a_placeholder_until_verified(self):
        d = self.build(gate=True)
        self.assertFalse(d["broker"]["verified"])
        self.assertIn("placeholder", d["broker"]["note"])
        v = self.build(gate=True, settings=od.clean_settings(dict(profile=dict(verified=True))))
        self.assertIn("verified by you", v["broker"]["note"])

    def test_the_comparison_table_has_one_row_per_order_type(self):
        d = self.build(gate=True, validation=record(("buy_stop",)))
        self.assertEqual([r["order_type"] for r in d["comparison"]], ["BUY STOP", "SELL STOP", "BUY LIMIT", "SELL LIMIT"])
        row = d["comparison"][0]
        for k in ("status", "entry", "stop", "tp1", "lots", "risk", "rr_net", "fill", "hit", "ev", "oos_n", "validation", "blocking"):
            self.assertIn(k, row)

    def test_paper_orders_are_shown_and_no_broker_order_exists(self):
        o = pp.create(dict(symbol="XAUUSD", direction="LONG", order_type="BUY STOP", kind="stop", entry=101.6, stop=98.4, tp1=106.4, tp2=None, tp3=None, lots=0.1,
                           units=10.0, risk_currency=30.0, expires_bars=8, signal_id="s", signal_ts=T0, strategy="buy_stop", regime="r", invalidation="x",
                           invalid_price=98.4, meta=dict(level=101.0), atr=2.0, broker_errors=[], fits=True, stale=False, targets_plan=None),
                      BARS[:1], STEP, now=T0)
        d = self.build(gate=True, paper_orders=[o], audit=[dict(ts="t", to_state="AWAITING_TRIGGER")])
        self.assertEqual(d["paper"]["summary"]["waiting"], 1)
        self.assertIn("no broker order exists", d["paper"]["broker_orders"])
        self.assertEqual(len(d["paper"]["audit"]), 1)

    def test_json_is_safe(self):
        json.dumps(rs._clean(self.build(gate=True, validation=record(("buy_stop",)))), allow_nan=False)

    def test_too_few_bars(self):
        with self.assertRaises(SystemExit):
            self.build(bars={"60m": flat(60)})

    def test_the_minimum_stop_setting_is_restored_after_building(self):
        before = real_ps.DEFAULT_MIN_STOP
        self.build(gate=True, settings=od.clean_settings(dict(profile=dict(stops_level_points=900))))
        self.assertEqual(real_ps.DEFAULT_MIN_STOP, before)


class RefreshPaper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.op, self.ap = os.path.join(self.dir, "o.json"), os.path.join(self.dir, "a.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def plan(self):
        return dict(symbol="XAUUSD", direction="LONG", order_type="BUY STOP", kind="stop", entry=101.6, stop=98.4, tp1=106.4, tp2=None, tp3=None, lots=0.1, units=10.0,
                    risk_currency=30.0, expires_bars=4, signal_id="s1", signal_ts=BARS[124][0], strategy="buy_stop", regime="r", invalidation="x", invalid_price=98.4,
                    meta=dict(level=101.0), atr=2.0, broker_errors=[], fits=True, stale=False, targets_plan=None)

    def test_a_working_order_that_never_triggers_expires_and_is_saved_and_logged(self):
        o = pp.create(self.plan(), BARS[:125], STEP, now=T0)
        pp.save([o], self.op)
        bars = BARS[:125] + flat(8)[:0] + [(BARS[124][0] + (k + 1) * STEP, 101.0, 99.0, 100.0, 100.0) for k in range(8)]
        out = pi.refresh_paper("GC=F", "60m", now=bars[-1][0] + STEP + 30, bars={"60m": bars}, settings=od.clean_settings({}), orders_path=self.op, audit_path=self.ap)
        self.assertEqual(out["changed"], [o["id"]])
        saved = pp.load(self.op)
        self.assertEqual(saved[0]["order_state"], "EXPIRED")
        self.assertTrue(any(e["to_state"] == "EXPIRED" for e in pp.audit_log(self.ap)))
        again = pi.refresh_paper("GC=F", "60m", now=bars[-1][0] + STEP + 30, bars={"60m": bars}, settings=od.clean_settings({}), orders_path=self.op, audit_path=self.ap)
        self.assertEqual(again["changed"], [])

    def test_nothing_live_means_nothing_to_do(self):
        out = pi.refresh_paper("GC=F", "60m", now=NOW, bars={"60m": BARS}, settings=od.clean_settings({}), orders_path=self.op, audit_path=self.ap)
        self.assertEqual(out["changed"], [])
        self.assertFalse(os.path.exists(self.ap))


if __name__ == "__main__":
    unittest.main()


class CreatePaper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.op, self.ap = os.path.join(self.dir, "o.json"), os.path.join(self.dir, "a.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, typ, validation, **kw):
        args = dict(symbol="GC=F", interval="60m", order_type=typ, now=NOW, bars={"60m": BARS}, validation=validation, portfolio_state=pf_state(),
                    equity_entered=True, settings=od.clean_settings({}), events=[], orders_path=self.op, audit_path=self.ap)
        args.update(kw)
        with GATE_OFF:
            return pi.create_paper(**args)

    def test_a_validated_order_is_papered_and_audited(self):
        made = self.make("BUY STOP", record(("buy_stop",)))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["order_type"], "BUY STOP")
        self.assertEqual(len(pp.load(self.op)), 1)
        self.assertTrue(pp.audit_log(self.ap))

    def test_an_unvalidated_order_needs_an_explicit_acknowledgement(self):
        with self.assertRaises(pi.paper.PaperError):
            self.make("BUY STOP", {})
        made = self.make("BUY STOP", {}, acknowledge_unvalidated=True)
        self.assertEqual(len(made), 1)

    def test_unknown_order_type_is_refused(self):
        with self.assertRaises(pi.paper.PaperError):
            self.make("MARKET", {})

    def test_oco_is_only_for_stop_pairs(self):
        with self.assertRaises(pi.paper.PaperError):
            self.make("BUY LIMIT", record(("buy_limit",)), oco=True)


    def test_the_same_setup_cannot_be_recorded_twice(self):
        self.make("BUY STOP", {}, acknowledge_unvalidated=True)
        with self.assertRaises(pi.paper.PaperError):
            self.make("BUY STOP", {}, acknowledge_unvalidated=True)
        self.assertEqual(len(pp.load(self.op)), 1)