"""Read-only portfolio monitor and trading journal. Every position and trade here is SYNTHETIC."""
import os
import shutil
import tempfile
import unittest

from _load import load

pf = load("webui", "portfolio")
jr = load("webui", "journal")


def state(**kw):
    s = dict(equity=10000, positions=[], pending=[], limits=dict(max_open_risk_pct=2, daily_loss_limit_pct=3, max_positions=3))
    s.update(kw)
    return s


def pos(side="long", units=10, entry=2000, stop=1990, target=None, sym="GC=F", pid="p1"):
    return dict(id=pid, symbol=sym, side=side, units=units, entry=entry, stop=stop, target=target)


class Validate(unittest.TestCase):
    def test_defaults(self):
        s = pf.validate({})
        self.assertEqual(s["equity"], 2700.0)
        self.assertEqual(s["positions"], [])
        self.assertEqual(s["limits"]["max_positions"], 3)

    def test_clean_state_round_trips(self):
        s = pf.validate(state(positions=[pos(target=2030)], pending=[dict(symbol="gc=f", side="short", units=5, kind="limit", price=2050, stop=2060)]))
        self.assertEqual(s["positions"][0]["symbol"], "GC=F")
        self.assertEqual(s["pending"][0]["symbol"], "GC=F")
        self.assertEqual(pf.validate(s)["positions"], s["positions"])

    def test_bad_input_is_rejected_with_a_message(self):
        bad = [
            state(equity=-5), state(equity="abc"), state(equity=float("nan")), state(equity=float("inf")),
            state(positions=[pos(side="buy")]), state(positions=[pos(units=0)]), state(positions=[pos(entry=-1)]),
            state(positions=[dict(pos(), symbol="XAU;rm -rf")]), state(positions=[dict(pos(), note="<script>x</script>")]),
            state(positions=[pos()] * 51), state(limits=dict(max_open_risk_pct=150)), state(limits=dict(daily_loss_limit_pct=0)),
            state(pending=[dict(symbol="GC=F", side="long", units=1, kind="market", price=1)]), state(pending=[dict(symbol="GC=F", side="long", units=1)]),
            state(currency="usd"), state(positions="nope"), "not a dict", state(realised_date="yesterday"),
        ]
        for b in bad:
            with self.assertRaises(ValueError, msg=str(b)[:60]):
                pf.validate(b)

    def test_optional_fields_may_be_blank(self):
        s = pf.validate(state(positions=[dict(pos(), stop="", target=None)]))
        self.assertIsNone(s["positions"][0]["stop"])
        self.assertIsNone(s["positions"][0]["target"])

    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "x", "p.json")
            pf.save(state(positions=[pos()]), p)
            self.assertEqual(len(pf.load(p)["positions"]), 1)
            with self.assertRaises(ValueError):
                pf.save(state(equity=-1), os.path.join(d, "bad.json"))
            self.assertFalse(os.path.exists(os.path.join(d, "bad.json")))
            self.assertEqual(pf.load(os.path.join(d, "missing.json"))["equity"], 2700.0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Monitor(unittest.TestCase):
    def test_risk_budget_and_status(self):
        m = pf.monitor(state(positions=[pos()]), "GC=F", 2005, atr=5)
        self.assertAlmostEqual(m["open_risk"], 100)
        self.assertAlmostEqual(m["risk_budget"], 200)
        self.assertAlmostEqual(m["risk_remaining"], 100)
        self.assertEqual(m["status"], "OK")
        self.assertAlmostEqual(m["exposure"]["open_pnl"], 50)
        self.assertAlmostEqual(m["open_pnl_pct"], 0.5)

    def test_other_symbols_are_ignored(self):
        m = pf.monitor(state(positions=[pos(), pos(sym="BTC-USD", units=1, entry=65000, stop=60000, pid="p2")]), "GC=F", 2000)
        self.assertEqual(len(m["positions"]), 1)

    def test_unprotected_position_warns(self):
        m = pf.monitor(state(positions=[dict(pos(), stop=None)]), "GC=F", 2000)
        self.assertEqual(m["status"], "WARN")
        self.assertTrue(any("no stop" in a for a in m["alerts"]))

    def test_over_budget_is_a_limit(self):
        m = pf.monitor(state(positions=[pos(units=100, stop=1980)]), "GC=F", 2000)
        self.assertEqual(m["status"], "LIMIT")
        self.assertLess(m["risk_remaining"], 0)

    def test_daily_loss_lockout(self):
        m = pf.monitor(state(realised_today=-350), "GC=F", 2000)
        self.assertTrue(m["lockout"])
        self.assertEqual(m["status"], "LIMIT")
        self.assertFalse(pf.monitor(state(realised_today=-100), "GC=F", 2000)["lockout"])

    def test_hedge_and_conflicting_orders(self):
        m = pf.monitor(state(positions=[pos(), pos("short", 5, 2010, 2020, pid="p2")]), "GC=F", 2000)
        self.assertTrue(any("same time" in a for a in m["alerts"]))
        both = pf.monitor(state(pending=[dict(symbol="GC=F", side="long", units=1, kind="limit", price=1990, stop=1980),
                                         dict(symbol="GC=F", side="short", units=1, kind="limit", price=2020, stop=2030)]), "GC=F", 2000)
        self.assertTrue(any("both sides" in a for a in both["alerts"]))

    def test_duplicate_pending_orders_and_missing_stops(self):
        pend = [dict(id="a", symbol="GC=F", side="long", units=1, kind="limit", price=1990, stop=1980),
                dict(id="b", symbol="GC=F", side="long", units=1, kind="limit", price=1991, stop=None)]
        m = pf.monitor(state(pending=pend), "GC=F", 2000)
        self.assertTrue(any("within 0.2%" in a for a in m["alerts"]))
        self.assertTrue(any("pending order b has no stop" in a for a in m["alerts"]))

    def test_pending_order_that_would_hedge_a_position(self):
        m = pf.monitor(state(positions=[pos()], pending=[dict(id="a", symbol="GC=F", side="short", units=1, kind="stop", price=1980, stop=1990)]),
                       "GC=F", 2000)
        self.assertTrue(any("would hedge" in a for a in m["alerts"]))

    def test_net_exposure_limit(self):
        s = state(positions=[pos(units=30, stop=1999)], limits=dict(max_open_risk_pct=50, daily_loss_limit_pct=50, max_positions=3, max_net_units=20))
        self.assertTrue(any("net exposure" in a for a in pf.monitor(s, "GC=F", 2000)["alerts"]))

    def test_scenarios_are_arithmetic_on_your_positions(self):
        m = pf.monitor(state(positions=[pos(target=2030)]), "GC=F", 2000, atr=5)
        rows = {r["label"]: r for r in m["scenarios"]}
        self.assertAlmostEqual(rows["+2 ATR move to 2010.00"]["pnl"], 100)
        self.assertAlmostEqual(rows["-3 ATR move to 1985.00"]["pnl"], -150)
        self.assertAlmostEqual(rows["every stop is hit"]["pnl"], -100)
        self.assertAlmostEqual(rows["every target is hit"]["pnl"], 300)

    def test_short_scenarios(self):
        m = pf.monitor(state(positions=[pos("short", 10, 2000, 2010, 1970)]), "GC=F", 2000, atr=5)
        rows = {r["label"]: r for r in m["scenarios"]}
        self.assertAlmostEqual(rows["+1 ATR move to 2005.00"]["pnl"], -50)
        self.assertAlmostEqual(rows["every stop is hit"]["pnl"], -100)
        self.assertAlmostEqual(rows["every target is hit"]["pnl"], 300)

    def test_distance_to_stop_and_target_in_atr(self):
        m = pf.monitor(state(positions=[pos(target=2030)]), "GC=F", 2000, atr=5)
        self.assertAlmostEqual(m["positions"][0]["to_stop_atr"], 2.0)
        self.assertAlmostEqual(m["positions"][0]["to_target_atr"], 6.0)

    def test_empty_book_is_ok_and_says_it_is_read_only(self):
        m = pf.monitor(state(), "GC=F", 2000)
        self.assertEqual((m["status"], m["scenarios"], m["positions"]), ("OK", [], []))
        self.assertIn("sent to a broker", m["note"])

    def test_the_module_has_no_network_or_order_code(self):
        with open(pf.__file__, encoding="utf-8") as f:
            src = f.read()
        for word in ("urllib", "socket", "requests", "subprocess", "place_order", "submit"):
            self.assertNotIn(word, src)


def sig(i=1, side="long", entry=2000.0, stop=1990.0):
    return dict(id="range-60m-%d-%s-abc" % (1_700_000_000 + i, side), strategy="range", side=side, decided_t=1_700_000_000 + i, price=entry, stop=stop,
                targets=[2015.0, 2030.0], note="Rejected the lower edge", invalidation="Close below", regime="sideways")


CTX = dict(symbol="GC=F", interval="60m", code_version="abc123def456", strategy_status="NOT VALIDATED", model_id=None,
           data_ref=dict(sha256="f" * 64, first="2026-01-01T00:00:00+00:00", last="2026-02-01T00:00:00+00:00"))


class Journal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = os.path.join(self.dir, "j.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_signal_is_recorded_once_with_its_context(self):
        a = jr.add_signal(sig(), "NO TRADE", ["strategy not validated"], CTX, self.p)
        b = jr.add_signal(sig(), "TRADE", [], CTX, self.p)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["decision"], "NO TRADE")                       # the first record stands
        self.assertEqual(len(jr.load(self.p)), 1)
        e = jr.load(self.p)[0]
        self.assertEqual(e["versions"]["code"], "abc123def456")
        self.assertEqual(e["data_ref"]["sha256"], "f" * 64)
        self.assertAlmostEqual(e["planned"]["risk"], 10.0)
        self.assertEqual(e["reasons"], ["strategy not validated"])

    def test_decision_must_be_trade_or_no_trade(self):
        with self.assertRaises(ValueError):
            jr.add_signal(sig(), "MAYBE", [], CTX, self.p)

    def test_a_refused_signal_cannot_be_filled(self):
        jr.add_signal(sig(), "NO TRADE", ["x"], CTX, self.p)
        with self.assertRaises(ValueError):
            jr.record_fill(sig()["id"], 2000.0, 1.0, path=self.p)

    def test_fill_measures_slippage_and_realised_risk(self):
        jr.add_signal(sig(), "TRADE", [], CTX, self.p)
        e = jr.record_fill(sig()["id"], 2002.0, 5.0, path=self.p)
        self.assertAlmostEqual(e["fill"]["slippage"], 2.0)                # paid 2 more than planned
        self.assertAlmostEqual(e["fill"]["realised_risk"], 12.0)
        s = sig(2, "short", 2000.0, 2010.0)
        jr.add_signal(s, "TRADE", [], CTX, self.p)
        e2 = jr.record_fill(s["id"], 1999.0, 5.0, path=self.p)
        self.assertAlmostEqual(e2["fill"]["slippage"], 1.0)               # sold lower than planned
        self.assertAlmostEqual(e2["fill"]["realised_risk"], 11.0)

    def test_close_compares_planned_and_realised_risk(self):
        jr.add_signal(sig(), "TRADE", [], CTX, self.p)
        jr.record_fill(sig()["id"], 2002.0, 5.0, path=self.p)
        e = jr.close_trade(sig()["id"], 2014.0, notes="took T1", tags=["target"], path=self.p)
        c = e["close"]
        self.assertAlmostEqual(c["pnl_per_unit"], 12.0)
        self.assertAlmostEqual(c["pnl"], 60.0)
        self.assertAlmostEqual(c["r_vs_planned_risk"], 1.2)
        self.assertAlmostEqual(c["r_vs_realised_risk"], 1.0)
        self.assertAlmostEqual(c["risk_ratio"], 1.2)
        self.assertIn("target", e["tags"])

    def test_problems_are_flagged_automatically(self):
        jr.add_signal(sig(), "TRADE", [], CTX, self.p)
        jr.record_fill(sig()["id"], 2004.0, 1.0, path=self.p)                # big slippage, risk 14 vs 10
        e = jr.close_trade(sig()["id"], 1980.0, stop_moved=True, path=self.p)
        flags = e["close"]["post_trade"]["auto_flags"]
        for f in ("high slippage", "loss larger than planned", "stop moved", "risk larger than planned"):
            self.assertIn(f, flags)

    def test_closing_needs_a_fill_and_clean_input(self):
        jr.add_signal(sig(), "TRADE", [], CTX, self.p)
        with self.assertRaises(ValueError):
            jr.close_trade(sig()["id"], 2010.0, path=self.p)
        jr.record_fill(sig()["id"], 2000.0, 1.0, path=self.p)
        with self.assertRaises(ValueError):
            jr.close_trade(sig()["id"], 2010.0, tags=["Bad Tag!"], path=self.p)
        with self.assertRaises(ValueError):
            jr.close_trade(sig()["id"], 2010.0, notes="<script>", path=self.p)
        with self.assertRaises(KeyError):
            jr.close_trade("missing", 2010.0, path=self.p)

    def test_recurring_errors_and_averages(self):
        for i in (1, 2, 3):
            s = sig(i)
            jr.add_signal(s, "TRADE", [], CTX, self.p)
            jr.record_fill(s["id"], 2004.0, 1.0, path=self.p)
            jr.close_trade(s["id"], 1995.0, path=self.p)
        jr.add_signal(sig(9), "NO TRADE", ["x"], CTX, self.p)
        a = jr.analyse(jr.load(self.p))
        self.assertEqual((a["signals"], a["taken"], a["refused"], a["closed"]), (4, 3, 1, 3))
        self.assertIn("high slippage", {r["tag"] for r in a["recurring"]})
        self.assertAlmostEqual(a["mean_slippage_in_r"], 0.4)
        self.assertGreater(a["mean_risk_ratio"], 1.0)

    def test_open_trades_are_counted_and_recent_is_newest_first(self):
        for i in (1, 2):
            jr.add_signal(sig(i), "TRADE", [], CTX, self.p)
        jr.record_fill(sig(1)["id"], 2000.0, 1.0, path=self.p)
        a = jr.analyse(jr.load(self.p))
        self.assertEqual(a["open"], 1)
        self.assertEqual(jr.recent(jr.load(self.p), 5)[0]["id"], sig(2)["id"])

    def test_no_entries(self):
        self.assertEqual(jr.analyse([])["signals"], 0)
        self.assertEqual(jr.load(os.path.join(self.dir, "none.json")), [])


if __name__ == "__main__":
    unittest.main()
