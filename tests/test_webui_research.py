"""Research runner: reproducibility, honesty of statuses, data sourcing and the validation store. Data is SYNTHETIC."""
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load

rs = load("webui", "research")
md = load("webui", "marketdata")
bt = load("webui", "backtester")


def noise(n, seed=1, vol=1.0, step=3600, t0=1_700_006_400):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        out.append((t0 + i * step, max(o, c) + abs(rnd.gauss(0, vol / 3)), min(o, c) - abs(rnd.gauss(0, vol / 3)), c, o))
        p = c
    return out


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def forget_holdout(self):
        try:
            os.remove(os.path.join(self.dir, "h.json"))
        except FileNotFoundError:
            pass                                    # no strategy had training trades, so nothing was logged

    def go(self, bars=None, seed=7, **kw):
        return rs.run("GC=F", "60m", seed=seed, quick=True, bars_override=bars or noise(1500, seed=4), events=[],
                      out_path=os.path.join(self.dir, "v.json"), holdout_store=os.path.join(self.dir, "h.json"),
                      model_dir=os.path.join(self.dir, "m"), **kw)

    def test_structure_and_json_safety(self):
        rec = self.go()
        json.dumps(rec, allow_nan=False)                                  # no NaN or infinity anywhere
        self.assertEqual(set(rec["strategies"]), {"breakout_retest", "trend_continuation", "liquidity_sweep", "range"})
        for s in rec["strategies"].values():
            for k in ("status", "reasons", "oos", "holdout", "stress", "equity", "by_side", "by_session", "by_regime", "news_filter", "record", "holm_p"):
                self.assertIn(k, s)
        self.assertFalse(rec["sample_data"])
        self.assertIn("PLACEHOLDER", rec["costs"]["source"])

    def test_noise_is_never_validated_and_ml_is_rejected(self):
        rec = self.go()
        for name, s in rec["strategies"].items():
            self.assertFalse(s["status"].startswith("VALIDATED"), name)
            self.assertTrue(s["reasons"], name)
        self.assertEqual(rec["ml"]["decision"]["status"], "REJECTED")

    def test_runs_are_reproducible(self):
        bars = noise(1500, seed=9)
        a = self.go(bars)
        self.forget_holdout()
        b = self.go(bars)

        def strip(o):                                   # wall-clock metadata is the only thing allowed to differ
            if isinstance(o, dict):
                return {k: strip(v) for k, v in o.items() if k not in ("created", "seconds", "path")}
            if isinstance(o, list):
                return [strip(v) for v in o]
            return o
        self.assertEqual(strip(a), strip(b))

    def test_record_holds_what_is_needed_to_reproduce(self):
        rec = self.go()
        r = rec["strategies"]["range"]["record"]
        self.assertEqual(r["seed"], 7)
        self.assertEqual(r["dataset"]["sha256"], rec["data"]["sha256"])
        self.assertEqual(len(r["code_version"]), 12)
        self.assertIn("costs", r)
        self.assertIn("walk_forward", r)
        self.assertEqual(rec["code_version"], r["code_version"])

    def test_different_seeds_change_the_bootstrap_not_the_trades(self):
        bars = noise(1500, seed=5)
        a = self.go(bars, seed=1)
        self.forget_holdout()
        b = self.go(bars, seed=2)
        sa, sb = a["strategies"]["range"], b["strategies"]["range"]
        self.assertEqual(sa["oos"], sb["oos"])
        if sa["bootstrap"] and sb["bootstrap"]:
            self.assertNotEqual(sa["bootstrap"]["lo"], sb["bootstrap"]["lo"])

    def test_the_validation_file_is_written_and_keyed_by_market(self):
        self.go()
        data = rs.load_validation(os.path.join(self.dir, "v.json"))
        self.assertIn("GC=F|60m", data)
        self.assertEqual(rs.load_validation(os.path.join(self.dir, "none.json")), {})

    def test_a_rejected_model_is_saved_but_never_served(self):
        self.go()
        self.assertIsNone(rs.ml.latest_accepted("GC=F", "60m", os.path.join(self.dir, "m")))
        self.assertTrue(os.listdir(os.path.join(self.dir, "m")))

    def test_the_holdout_is_looked_at_once_per_segment(self):
        bars = noise(1500, seed=6)
        self.go(bars)
        self.go(bars)
        with open(os.path.join(self.dir, "h.json")) as f:
            log = json.load(f)
        for k, v in log.items():
            self.assertEqual(len(v["attempts"]), 1, k)

    def test_too_little_data(self):
        with self.assertRaises(SystemExit):
            rs.run("GC=F", "60m", bars_override=noise(200), events=[], out_path="")

    def test_out_path_empty_means_nothing_is_written(self):
        rs.run("GC=F", "60m", quick=True, bars_override=noise(1000), events=[], out_path="", holdout_store=os.path.join(self.dir, "h.json"),
               model_dir=os.path.join(self.dir, "m"), do_ml=False)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "v.json")))

    def test_events_flow_into_the_news_filter_check(self):
        bars = noise(1500, seed=3)
        ev = [dict(ts=bars[i][0], importance="high", type="NFP", name="x", source="t", reliability="manual", utc="", actual=None,
                   forecast=None, previous=None, notes="", observed_at="") for i in range(100, 1400, 40)]
        rec = rs.run("GC=F", "60m", quick=True, bars_override=bars, events=ev, out_path="", holdout_store=os.path.join(self.dir, "h.json"),
                     model_dir=os.path.join(self.dir, "m"), do_ml=False)
        for s in rec["strategies"].values():
            self.assertIn("verdict", s["news_filter"])
            self.assertFalse(s["news_filter"]["recommended"] and s["news_filter"]["inside"]["n"] < 30)
        self.assertEqual(rec["events"]["high"], len(ev))


class Data(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = md.Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_stored_bid_ask_data_gives_measured_costs(self):
        bars = [(1_700_006_400 + i * 300, 2001.0, 1999.0, 2000.5, 2000.0) for i in range(60)]
        self.store.save("dukascopy", "XAUUSD", "5m", bars, extras={"spread": {b[0]: 0.4 for b in bars}})
        got, info, spread = rs.get_bars("XAUUSD", "5m", "dukascopy", store=self.store, now=bars[-1][0] + 10 ** 6)
        self.assertEqual(len(got), 60)
        self.assertTrue(info["bid_ask"])
        c, src = rs.default_costs("XAUUSD", spread)
        self.assertAlmostEqual(c["spread"], 0.4)
        self.assertAlmostEqual(c["slippage"], 0.1)
        self.assertIn("measured", src)

    def test_the_forming_bar_is_dropped(self):
        bars = [(1_700_006_400 + i * 300, 2001.0, 1999.0, 2000.5, 2000.0) for i in range(60)]
        self.store.save("dukascopy", "XAUUSD", "5m", bars)
        got, _, _ = rs.get_bars("XAUUSD", "5m", "dukascopy", store=self.store, now=bars[-1][0] + 100)
        self.assertEqual(len(got), 59)

    def test_nothing_stored_is_an_error_not_a_guess(self):
        with self.assertRaises(SystemExit):
            rs.get_bars("XAUUSD", "5m", "dukascopy", store=self.store)

    def test_placeholder_costs_are_labelled_and_overridable(self):
        c, src = rs.default_costs("GC=F")
        self.assertIn("PLACEHOLDER", src)
        self.assertEqual((c["spread"], c["slippage"]), (0.30, 0.10))
        c2, _ = rs.default_costs("GC=F", None, dict(spread=0.5, slippage=None, commission_per_unit=0.02))
        self.assertEqual((c2["spread"], c2["slippage"], c2["commission_per_unit"]), (0.5, 0.10, 0.02))
        c3, _ = rs.default_costs("BTC-USD")
        self.assertGreater(c3["spread"], 1)

    def test_walk_forward_sizes_scale_with_the_data(self):
        a, b = rs.wf_config(1000), rs.wf_config(3000)
        self.assertLess(a["train_min"], b["train_min"])
        self.assertLessEqual(a["test_len"], b["test_len"])

    def test_clean_removes_nan_and_infinity(self):
        out = rs._clean({"a": float("nan"), "b": [float("inf"), 1.0], "c": (1, 2)})
        self.assertEqual(out, {"a": None, "b": [None, 1.0], "c": [1, 2]})

    def test_slim_thins_long_curves_and_drops_the_grid_without_touching_the_original(self):
        eq = [dict(t=i, r=float(i)) for i in range(1000)]
        rec = dict(strategies={"range": dict(equity=eq, grid_oos=[1, 2, 3], status="x")})
        out = rs.slim(rec, max_points=100)
        s = out["strategies"]["range"]
        self.assertEqual(len(s["equity"]), 100)
        self.assertEqual((s["equity"][0]["t"], s["equity"][-1]["t"]), (0, 999))        # both ends kept
        self.assertNotIn("grid_oos", s)
        self.assertEqual(len(rec["strategies"]["range"]["equity"]), 1000)
        self.assertIn("grid_oos", rec["strategies"]["range"])
        short = rs.slim(dict(strategies={"range": dict(equity=eq[:50])}))
        self.assertEqual(len(short["strategies"]["range"]["equity"]), 50)

    def test_slim_output_of_a_real_run_is_small_and_valid_json(self):
        d = tempfile.mkdtemp()
        try:
            rec = rs.run("GC=F", "60m", quick=True, bars_override=noise(1200, seed=2), events=[], out_path="",
                         holdout_store=os.path.join(d, "h.json"), model_dir=os.path.join(d, "m"), do_ml=False)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        text = json.dumps(rs.slim(rec), allow_nan=False)
        self.assertLess(len(text), 150000)
        self.assertEqual(json.loads(text)["symbol"], "GC=F")

    def test_equity_curve(self):
        tr = [dict(r=1.0, exit_t=2), dict(r=-0.5, exit_t=1), dict(r=2.0, exit_t=3)]
        self.assertEqual([p["r"] for p in rs.equity_curve(tr)], [-0.5, 0.5, 2.5])


if __name__ == "__main__":
    unittest.main()
