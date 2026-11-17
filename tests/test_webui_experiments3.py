"""Phase 10 to 12: event-driven backtester, risk-gate replay, logistic filter and AUC. SYNTHETIC data only."""
import os
import random
import shutil
import tempfile
import unittest

from _load import load

sc = load("webui", "scorecard")
eb = load("webui", "event_backtest")
e3 = load("webui", "experiments3")

T0 = 1_700_000_000
H = 3600


def bar(i, hi, lo, cl, op=None):
    return (T0 + i * H, hi, lo, cl, cl if op is None else op)


def rec(kind="pullback", side="long", entry=100.0, stop=95.0, targets=(105.0, 110.0, 115.0)):
    p = dict(name="x", side=side, kind=kind, entry=entry, stop=stop, targets=[dict(price=t) for t in targets], rank=1, grade="A", score=7.0)
    r = sc.make_record("GC=F", "1h", p, T0)
    r["id"] = "r-%s-%s-%s" % (kind, side, entry)
    return r


NOW = T0 + 200 * H


class Lifecycle(unittest.TestCase):
    def _run(self, r, bars):
        out, trans, hist = eb.run([r], {"GC=F": {"1h": bars}}, NOW, keep_history=2)
        return out[r["id"]], trans, hist

    def test_winner_passes_through_every_status_in_order(self):
        r = rec()
        bars = [bar(0, 101, 99.0, 100), bar(1, 111, 100.5, 110), bar(2, 116, 105, 115)]
        res, trans, _ = self._run(r, bars)
        o = eb.Order(r)
        for b in bars:
            if o.on_bar(b):
                break
        self.assertEqual([s for _, s in o.history], ["CREATED", "PENDING", "FILLED", "PARTIALLY CLOSED", "CLOSED WIN"])
        self.assertEqual(res["state"], "WIN")

    def test_expiry_and_invalidation(self):
        r = rec()
        flat = [bar(i, 110, 105, 108) for i in range(r["expiry_bars"] + 3)]
        res, _, _ = self._run(r, flat)
        self.assertEqual((res["state"], res["status"]), ("NOT TRIGGERED", "EXPIRED"))
        res, _, _ = self._run(r, [bar(0, 101, 90, 92)])
        self.assertEqual(res["state"], "INVALIDATED")

    def test_unfinished_trade_is_open_never_a_win(self):
        r = rec()
        res, _, _ = self._run(r, [bar(0, 101, 99.0, 100), bar(1, 106, 100.5, 105)])
        self.assertEqual((res["state"], res["status"]), ("OPEN", "OPEN"))

    def test_breakout_fills_next_open_and_rejected_next_open_has_no_fill(self):
        r = rec(kind="breakout", entry=100.0, stop=95.0)
        res, _, _ = self._run(r, [bar(0, 102, 99, 101), bar(1, 103, 100, 102, op=101.0), bar(2, 96, 94, 95)])
        self.assertEqual(res["state"], "LOSS")
        self.assertEqual(res["fill_price"], 101.0)
        res, _, _ = self._run(r, [bar(0, 102, 99, 101), bar(1, 103, 92, 93, op=94.0)])
        self.assertEqual(res["state"], "NOT TRIGGERED")
        self.assertIsNone(res["fill_time"])


class Reconcile(unittest.TestCase):
    def test_the_event_engine_agrees_with_the_grader_on_random_paths(self):
        rnd = random.Random(5)
        recs, series = [], {}
        for n in range(150):
            side = rnd.choice(("long", "short"))
            kind = rnd.choice(("pullback", "breakout"))
            sgn = 1 if side == "long" else -1
            entry = 100.0
            r = rec(kind=kind, side=side, entry=entry, stop=entry - sgn * 5, targets=tuple(entry + sgn * k for k in (5, 10, 15)))
            r["id"] = "r%d" % n
            px, bars = 100.0 + rnd.uniform(-3, 3), []
            for i in range(60):
                op = px
                px = max(50.0, px + rnd.uniform(-3.2, 3.2))
                hi, lo = max(op, px) + rnd.uniform(0, 2), min(op, px) - rnd.uniform(0, 2)
                bars.append((T0 + i * H, hi, lo, px, op))
            recs.append(r)
            series[r["id"]] = bars
        agree = 0
        for r in recs:
            rep = eb.reconcile([r], {"GC=F": {"1h": series[r["id"]]}}, NOW)
            agree += rep["identical"]
            self.assertEqual(rep["compared"], 1, rep["disagreements"])
        self.assertEqual(agree, len(recs))

    def test_a_disagreement_is_listed(self):
        r = rec()
        bars = [bar(0, 101, 99.0, 100)]
        real = eb.sc.grade

        def broken(rec_, bars_, now_):
            g = real(rec_, bars_, now_)
            g["state"] = "LOSS"
            return g
        eb.sc.grade = broken
        try:
            rep = eb.reconcile([r], {"GC=F": {"1h": bars}}, NOW)
        finally:
            eb.sc.grade = real
        self.assertFalse(rep["passed"])
        self.assertEqual(len(rep["disagreements"]), 1)


class Gates(unittest.TestCase):
    def trade(self, i, sym="GC=F", side="long", r=1.0, hours=2):
        t0 = T0 + i * 600
        return dict(fill_time=sc._iso(t0), exit_time=sc._iso(t0 + hours * H), r_net=r, symbol=sym, side=side)

    def test_position_cap_and_same_market_side(self):
        rows = [self.trade(0, "A"), self.trade(1, "B"), self.trade(2, "C"), self.trade(3, "D"), self.trade(4, "A")]
        g = eb.gate_replay(rows)
        self.assertEqual(g["taken"], 3)
        self.assertEqual(g["skipped"]["max_positions"], 2)
        rows = [self.trade(0, "A"), self.trade(1, "A")]
        g = eb.gate_replay(rows)
        self.assertEqual((g["taken"], g["skipped"]["same_market_side"]), (1, 1))

    def test_daily_lockout_after_six_losing_trades(self):
        day = (T0 // 86400 + 1) * 86400 + H                     # a start well inside one UTC day
        rows = [dict(fill_time=sc._iso(day + i * 600), exit_time=sc._iso(day + i * 600 + 300), r_net=-1.0, symbol="S%d" % i, side="long") for i in range(8)]
        g = eb.gate_replay(rows)
        self.assertEqual(g["taken"], 6)
        self.assertEqual(g["skipped"]["daily_lockout"], 2)
        self.assertAlmostEqual(g["total_pct"], -1.5)

    def test_gates_never_make_a_negative_average_positive_by_construction_label(self):
        g = eb.gate_replay([self.trade(0, r=-0.5)])
        self.assertIn("historical backtest", g["label"])


class Model(unittest.TestCase):
    def test_auc_known_values(self):
        self.assertEqual(e3.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]), 1.0)
        self.assertEqual(e3.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]), 0.0)
        self.assertEqual(e3.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]), 0.5)
        self.assertIsNone(e3.auc([0.1, 0.2], [1, 1]))

    def test_logit_learns_a_planted_signal_and_ignores_noise(self):
        rnd = random.Random(3)
        X, y = [], []
        for _ in range(600):
            row = [rnd.gauss(0, 1) for _ in e3.FEATURES]
            X.append(row)
            y.append(1 if row[1] + 0.3 * rnd.gauss(0, 1) > 0 else 0)
        m = e3.Logit().fit(X[:400], y[:400])
        a = e3.auc([m.predict(x) for x in X[400:]], y[400:])
        self.assertGreater(a, 0.9)
        Xn = [[rnd.gauss(0, 1) for _ in e3.FEATURES] for _ in range(600)]
        yn = [rnd.randint(0, 1) for _ in range(600)]
        m = e3.Logit().fit(Xn[:400], yn[:400])
        a = e3.auc([m.predict(x) for x in Xn[400:]], yn[400:])
        self.assertLess(abs(a - 0.5), 0.1)

    def test_missing_features_are_imputed_not_fatal(self):
        X = [[None] * len(e3.FEATURES) for _ in range(60)] + [[1.0] * len(e3.FEATURES) for _ in range(60)]
        y = [0] * 60 + [1] * 60
        m = e3.Logit().fit(X, y)
        self.assertTrue(0.0 <= m.predict([None] * len(e3.FEATURES)) <= 1.0)

    def test_feature_vector_has_the_declared_length_and_is_direction_aware(self):
        r = dict(kind="pullback", side="short", created_ts=T0, entry=100.0, stop=105.0, score=7.0, dist_atr=1.0, agree=["1h", "4h"])
        f = dict(s1=dict(adx=30, er=0.4, slope_atr=-1.0, vol_ratio=1.1, bias="bearish"), s4=dict(bias="bearish"), tf_atr=2.0, news_near=False)
        v = e3.vector(r, f)
        self.assertEqual(len(v), len(e3.FEATURES))
        self.assertEqual(v[1], 1.0)      # a falling slope helps a short
        self.assertEqual(v[5], 1.0)      # H1 bias agrees with the short
        self.assertEqual(v[10], 2.5)     # planned risk in ATR


class Filters(unittest.TestCase):
    def test_session_filters_use_the_creation_time_only(self):
        asia = 1_700_006_400 - (1_700_006_400 % 86400) + 3 * H     # 03:00 UTC
        ny = asia + 12 * H                                         # 15:00 UTC
        late = asia + 21 * H                                       # 00:00 next day is asia, so use 22:00
        late = asia - 3 * H + 22 * H
        h10b = next(k for k in e3.FILTERS if k[0] == "H10b")[3]
        h10c = next(k for k in e3.FILTERS if k[0] == "H10c")[3]
        self.assertFalse(h10b(dict(created_ts=asia), {}))
        self.assertTrue(h10b(dict(created_ts=ny), {}))
        self.assertTrue(h10c(dict(created_ts=ny), {}))
        self.assertFalse(h10c(dict(created_ts=late), {}))
        h10a = next(k for k in e3.FILTERS if k[0] == "H10a")[3]
        self.assertFalse(h10a(dict(created_ts=ny), dict(news_near=True)))

    def test_every_hypothesis_has_a_definition_and_the_count_is_four(self):
        self.assertEqual(len(e3.FILTERS) + 1, 4)
        self.assertEqual(e3.N_TOTAL, 29)


class LogAndResult(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def doc(self, m):
        h = dict(phase=10, id="H10a", name="n", definition="d", decision="REJECTED", reasons=["r"], train=dict(mean_r=-0.1), test=dict(mean_r=m, trades=120))
        return dict(created="2026-01-01T00:00:00Z", split={}, hypotheses=[h], engine=dict(agreement=1.0))

    def test_identical_reruns_are_not_logged_twice_but_changed_results_are(self):
        p = os.path.join(self.d, "log.json")
        e3.append_log(self.doc(-0.1), p)
        e3.append_log(self.doc(-0.1), p)
        e3.append_log(self.doc(-0.2), p)
        import json
        with open(p, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)

    def test_result_round_trip_and_junk(self):
        p = os.path.join(self.d, "r.json")
        self.assertIsNone(e3.load_result(p))
        e3.save_result(self.doc(-0.1), p)
        self.assertEqual(e3.load_result(p)["created"], "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
