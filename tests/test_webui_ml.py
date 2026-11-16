"""ML research layer: numerics, calibration, causal features, chronological splits and the acceptance rules.
Every dataset here is SYNTHETIC."""
import math
import os
import random
import shutil
import tempfile
import unittest

from _load import load

ml = load("webui", "ml")


def bar(t, o, h, l, c):
    return (t, h, l, c, o)


def walk(n, seed=3, vol=1.0, step=3600, t0=1_700_006_400):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, vol)
        out.append(bar(t0 + i * step, o, max(o, c) + abs(rnd.gauss(0, vol / 3)), min(o, c) - abs(rnd.gauss(0, vol / 3)), c))
        p = c
    return out


class Numerics(unittest.TestCase):
    def test_sigmoid_logit_roundtrip(self):
        for p in (0.01, 0.3, 0.5, 0.9, 0.999):
            self.assertAlmostEqual(ml.sigmoid(ml.logit(p)), p, places=6)
        self.assertAlmostEqual(ml.sigmoid(1000), 1.0)
        self.assertAlmostEqual(ml.sigmoid(-1000), 0.0)

    def test_solver(self):
        x = ml._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
        self.assertAlmostEqual(x[0], 1.0)
        self.assertAlmostEqual(x[1], 3.0)

    def test_scaler(self):
        X = [[1.0, 10.0], [3.0, 10.0], [5.0, 10.0]]
        s = ml.Scaler(X)
        Z = s.apply(X)
        self.assertAlmostEqual(sum(r[0] for r in Z), 0.0)
        self.assertTrue(all(r[1] == 0.0 for r in Z))                         # a constant column is left harmless

    def make(self, n, w, b, seed=1):
        rnd = random.Random(seed)
        X = [[rnd.gauss(0, 1) for _ in w] for _ in range(n)]
        y = [1 if rnd.random() < ml.sigmoid(b + sum(wi * xi for wi, xi in zip(w, r))) else 0 for r in X]
        return X, y

    def test_logistic_recovers_known_weights(self):
        X, y = self.make(4000, [1.5, -2.0], 0.3)
        w = ml.fit_logistic(X, y, l2=0.01)
        self.assertAlmostEqual(w[0], 0.3, delta=0.2)
        self.assertAlmostEqual(w[1], 1.5, delta=0.3)
        self.assertAlmostEqual(w[2], -2.0, delta=0.3)

    def test_l2_shrinks_the_weights(self):
        X, y = self.make(500, [1.5, -2.0], 0.0)
        w0 = ml.fit_logistic(X, y, l2=0.01)
        w1 = ml.fit_logistic(X, y, l2=500.0)
        self.assertLess(abs(w1[1]), abs(w0[1]))
        self.assertLess(abs(w1[2]), abs(w0[2]))

    def test_gradient_boosting_learns_an_interaction_a_line_cannot(self):
        rnd = random.Random(4)
        X = [[rnd.uniform(-1, 1), rnd.uniform(-1, 1)] for _ in range(1200)]
        y = [1 if a * b > 0 else 0 for a, b in X]
        Xt = [[rnd.uniform(-1, 1), rnd.uniform(-1, 1)] for _ in range(600)]
        yt = [1 if a * b > 0 else 0 for a, b in Xt]
        g = ml.GradientBoosting(n_estimators=25, depth=2, min_leaf=20).fit(X, y)
        acc_g = sum((p > 0.5) == bool(t) for p, t in zip(g.predict(Xt), yt)) / len(yt)
        w = ml.fit_logistic(X, y)
        acc_l = sum((p > 0.5) == bool(t) for p, t in zip(ml.predict_logistic(w, Xt), yt)) / len(yt)
        self.assertGreater(acc_g, 0.85)
        self.assertLess(acc_l, 0.65)

    def test_gradient_boosting_is_deterministic_and_serialisable(self):
        rnd = random.Random(2)
        X = [[rnd.gauss(0, 1) for _ in range(4)] for _ in range(300)]
        y = [1 if x[0] > 0 else 0 for x in X]
        a = ml.GradientBoosting(n_estimators=6).fit(X, y)
        b = ml.GradientBoosting(n_estimators=6).fit(X, y)
        self.assertEqual(a.raw(X), b.raw(X))
        import json
        c = ml.GradientBoosting.from_dict(json.loads(json.dumps(a.to_dict())))
        self.assertEqual(a.raw(X), c.raw(X))
        self.assertIn("f0", a.importance(["f0", "f1", "f2", "f3"]))


class Metrics(unittest.TestCase):
    def test_auc(self):
        self.assertAlmostEqual(ml.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]), 1.0)
        self.assertAlmostEqual(ml.auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]), 0.0)
        self.assertAlmostEqual(ml.auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]), 0.5)
        self.assertIsNone(ml.auc([1, 1, 1], [0.1, 0.2, 0.3]))
        self.assertAlmostEqual(ml.auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]), 0.75)       # the textbook example

    def test_log_loss_and_brier(self):
        self.assertAlmostEqual(ml.log_loss([1, 0], [0.5, 0.5]), math.log(2))
        self.assertAlmostEqual(ml.log_loss([1], [0.9]), -math.log(0.9))
        self.assertAlmostEqual(ml.brier([1, 0], [0.8, 0.3]), (0.04 + 0.09) / 2)
        self.assertTrue(math.isfinite(ml.log_loss([1], [0.0])))             # clipped, never infinite

    def test_calibration_of_a_calibrated_forecaster(self):
        rnd = random.Random(8)
        p = [rnd.random() for _ in range(20000)]
        y = [1 if rnd.random() < q else 0 for q in p]
        self.assertLess(ml.ece(y, p), 0.02)
        bins = ml.calibration_bins(y, p)
        self.assertEqual(sum(b["n"] for b in bins), len(p))
        for b in bins:
            self.assertLessEqual(b["ci"][0], b["freq"])
            self.assertGreaterEqual(b["ci"][1], b["freq"])

    def test_platt_repairs_an_overconfident_model(self):
        rnd = random.Random(6)
        true = [rnd.uniform(0.2, 0.8) for _ in range(6000)]
        y = [1 if rnd.random() < q else 0 for q in true]
        raw = [3.0 * ml.logit(q) for q in true]                              # three times too confident
        va, te = slice(0, 3000), slice(3000, 6000)
        before = ml.ece(y[te], [ml.sigmoid(r) for r in raw[te]])
        pl = ml.fit_platt(raw[va], y[va])
        after = ml.ece(y[te], ml.apply_platt(pl, raw[te]))
        self.assertGreater(before, 0.1)
        self.assertLess(after, 0.04)
        self.assertAlmostEqual(pl["a"], 1 / 3.0, delta=0.06)

    def test_auc_ci_brackets_the_estimate(self):
        rnd = random.Random(1)
        y = [rnd.randint(0, 1) for _ in range(400)]
        p = [0.3 * yi + rnd.random() for yi in y]
        lo, hi = ml.auc_ci(y, p, 100, 3)
        self.assertLess(lo, ml.auc(y, p))
        self.assertGreater(hi, ml.auc(y, p))


class Labels(unittest.TestCase):
    def bars(self, rows):
        return [bar(1_700_000_000 + i * 60, *r) for i, r in enumerate(rows)]       # (o, h, l, c)

    def test_target_before_stop(self):
        b = self.bars([(100, 100, 100, 100), (100, 100.5, 99.8, 100.2), (100.2, 101.7, 100.0, 101.5), (101.5, 101.5, 101.5, 101.5)])
        r = ml.barrier(b, 0, 1, atr=1.0, stop_atr=1.0, target_r=1.5, horizon=3, cost_per_unit=0.0)
        self.assertEqual((r["y"], r["how"]), (1, "target"))
        self.assertAlmostEqual(r["r_gross"], 1.5)

    def test_stop_before_target(self):
        b = self.bars([(100, 100, 100, 100), (100, 100.5, 98.9, 99.2), (99.2, 102, 99, 101), (101, 101, 101, 101)])
        r = ml.barrier(b, 0, 1, 1.0, 1.0, 1.5, 3, 0.0)
        self.assertEqual((r["y"], r["how"]), (0, "stop"))
        self.assertAlmostEqual(r["r_gross"], -1.0)

    def test_same_bar_counts_as_the_stop(self):
        b = self.bars([(100, 100, 100, 100), (100, 102, 98.5, 100), (100, 100, 100, 100)])
        self.assertEqual(ml.barrier(b, 0, 1, 1.0, 1.0, 1.5, 2, 0.0)["how"], "stop")

    def test_short_mirrors(self):
        b = self.bars([(100, 100, 100, 100), (100, 100.4, 98.4, 98.6), (98.6, 98.6, 98.6, 98.6)])
        r = ml.barrier(b, 0, -1, 1.0, 1.0, 1.5, 2, 0.0)
        self.assertEqual((r["y"], r["how"]), (1, "target"))

    def test_time_exit_marks_to_the_last_close(self):
        b = self.bars([(100, 100, 100, 100), (100, 100.6, 99.5, 100.5), (100.5, 100.8, 100.2, 100.6), (100.6, 100.7, 100.4, 100.5)])
        r = ml.barrier(b, 0, 1, 1.0, 1.0, 1.5, 3, 0.0)
        self.assertEqual((r["y"], r["how"]), (0, "time"))
        self.assertAlmostEqual(r["r_gross"], 0.5)

    def test_costs_come_off_in_r(self):
        b = self.bars([(100, 100, 100, 100), (100, 101.7, 99.9, 101.6), (101.6, 101.6, 101.6, 101.6)])
        r = ml.barrier(b, 0, 1, 1.0, 1.0, 1.5, 2, 0.2)
        self.assertAlmostEqual(r["r_net"], 1.5 - 0.2)
        self.assertAlmostEqual(r["cost_r"], 0.2)


class Features(unittest.TestCase):
    def test_feature_names_match_the_vector(self):
        rows = ml.build_dataset(walk(300), horizon=8, stride=3)
        self.assertTrue(rows)
        for r in rows[:50]:
            self.assertEqual(len(r["x"]), len(ml.FEATURES))
            self.assertTrue(all(math.isfinite(v) for v in r["x"]))
        self.assertEqual(len(ml.FEATURES), len(set(ml.FEATURES)))

    def test_features_never_use_the_future(self):
        bars = walk(600, seed=5)
        full = ml.build_dataset(bars, horizon=10, stride=2)
        part = ml.build_dataset(bars[:400], horizon=10, stride=2)
        key = lambda r: (r["i"], r["side"])
        full_map = {key(r): r for r in full}
        checked = 0
        for r in part:
            f = full_map[key(r)]
            self.assertEqual(r["x"], f["x"], "features at bar %d" % r["i"])
            self.assertEqual((r["y"], r["r_net"]), (f["y"], f["r_net"]))
            checked += 1
        self.assertGreater(checked, 100)

    def test_both_sides_are_present_and_flip_directional_features(self):
        rows = ml.build_dataset(walk(200), horizon=8, stride=4)
        longs = {r["i"]: r for r in rows if r["side"] == 1}
        shorts = {r["i"]: r for r in rows if r["side"] == -1}
        self.assertEqual(set(longs), set(shorts))
        i = next(iter(longs))
        ret1 = ml.FEATURES.index("ret1")
        self.assertAlmostEqual(longs[i]["x"][ret1], -shorts[i]["x"][ret1])
        self.assertEqual(longs[i]["x"][0], 1.0)
        self.assertEqual(shorts[i]["x"][0], -1.0)

    def test_rows_stop_before_the_labels_run_out_of_data(self):
        bars = walk(200)
        rows = ml.build_dataset(bars, horizon=12, stride=1)
        self.assertLess(max(r["i"] for r in rows) + 12 + 1, len(bars))

    def test_events_are_scheduled_times_so_they_are_allowed(self):
        bars = walk(300)
        ev = [bars[150][0], bars[250][0]]
        rows = ml.build_dataset(bars, horizon=8, stride=3, event_times=ev)
        k = ml.FEATURES.index("event_next")
        near = [r for r in rows if r["i"] == 147 and r["side"] == 1]
        far = [r for r in rows if r["i"] == 90 and r["side"] == 1]
        if near and far:
            self.assertLess(near[0]["x"][k], far[0]["x"][k])


class Splits(unittest.TestCase):
    def rows(self, n=1000):
        return [dict(i=i, t=i, side=1, x=[0.0] * len(ml.FEATURES), y=i % 2, r_net=0.0) for i in range(n)]

    def test_blocks_are_ordered_and_embargoed(self):
        h = 20
        tr, va, te = ml.split_chronological(self.rows(), h)
        self.assertTrue(tr and va and te)
        self.assertLess(max(r["i"] for r in tr) + h, min(r["i"] for r in va))
        self.assertLess(max(r["i"] for r in va) + h, min(r["i"] for r in te))
        self.assertEqual(len({r["i"] for r in tr} & {r["i"] for r in va}), 0)
        self.assertEqual(len({r["i"] for r in va} & {r["i"] for r in te}), 0)

    def test_empty(self):
        self.assertEqual(ml.split_chronological([], 10), ([], [], []))

    def test_threshold_is_chosen_on_validation_only(self):
        p = [i / 100.0 for i in range(100)]
        r = [1.0 if q > 0.8 else -0.5 for q in p]
        thr = ml.pick_threshold(p, r, min_n=5)
        self.assertGreaterEqual(thr, 0.7)
        self.assertIsNone(ml.pick_threshold([0.5] * 10, [1.0] * 10, min_n=20))


def synth_rows(n, signal, seed=3, cost=0.1):
    rnd = random.Random(seed)
    rows = []
    d = len(ml.FEATURES)
    for i in range(n):
        x = [rnd.gauss(0, 1) for _ in range(d)]
        z = signal * x[1] + rnd.gauss(0, 0.4)                                # feature 1 carries the signal
        y = 1 if z > 0.4 else 0
        rows.append(dict(i=i, t=i, side=1, x=x, y=y, r_gross=1.5 if y else -1.0, r_net=(1.5 if y else -1.0) - cost,
                         bars=5, mae_r=0.5, how="x", cost_r=cost))
    return rows


class Acceptance(unittest.TestCase):
    GBM = dict(n_estimators=8, depth=2, min_leaf=20)

    def test_noise_is_rejected(self):
        rows = synth_rows(2400, signal=0.0, seed=5)
        ev = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        self.assertEqual(ev["decision"]["status"], "REJECTED")
        self.assertTrue(ev["decision"]["reasons"])

    def test_a_real_signal_can_be_accepted_and_beats_the_baselines(self):
        rows = synth_rows(2400, signal=2.0, seed=7)
        ev = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        self.assertTrue(ev["decision"]["status"].startswith("ACCEPTED"), ev["decision"])
        m = ev["models"][ev["decision"]["model"]]
        self.assertGreater(m["selected"]["expectancy"], ev["baselines"]["take_all"]["expectancy"])
        self.assertGreater(m["auc_ci"][0], 0.5)
        self.assertLess(m["ece"], 0.08)
        self.assertEqual(ev["baselines"]["no_trade"]["expectancy"], 0.0)

    def test_a_signal_that_costs_eat_is_rejected(self):
        rows = synth_rows(2400, signal=2.0, seed=7, cost=3.0)               # every trade loses 3R of cost
        ev = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        self.assertEqual(ev["decision"]["status"], "REJECTED")
        self.assertTrue(any("expectancy" in r or "no better" in r for r in ev["decision"]["reasons"]))

    def test_too_little_data_is_rejected_up_front(self):
        ev = ml.evaluate_rows(synth_rows(200, 2.0), 10, gbm=self.GBM)
        self.assertEqual(ev["decision"]["status"], "REJECTED")
        self.assertIn("too few rows", ev["decision"]["reasons"][0])

    def test_test_block_is_not_used_for_calibration_or_threshold(self):
        rows = synth_rows(2400, 2.0, seed=9)
        a = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        tr, va, te = ml.split_chronological(rows, 10)
        flipped = [dict(r, y=1 - r["y"], r_net=-r["r_net"]) if r in te else r for r in rows]    # ruin the test block only
        b = ml.evaluate_rows(flipped, 10, gbm=self.GBM)
        for name in a["models"]:
            self.assertEqual(a["models"][name]["platt"], b["models"][name]["platt"])
            self.assertEqual(a["models"][name]["threshold"], b["models"][name]["threshold"])
        self.assertNotEqual(a["decision"]["status"], b["decision"]["status"])

    def test_reproducible(self):
        rows = synth_rows(1600, 1.0, seed=2)
        a = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        b = ml.evaluate_rows(rows, 10, gbm=self.GBM)
        self.assertEqual(a["models"]["logistic"]["log_loss"], b["models"]["logistic"]["log_loss"])
        self.assertEqual(a["decision"], b["decision"])


class Registry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def trained(self, signal, seed=7):
        rows = synth_rows(2400, signal, seed=seed)
        ev = ml.evaluate_rows(rows, 10, gbm=Acceptance.GBM)
        return rows, ev

    def test_accepted_model_predicts_with_uncertainty(self):
        rows, ev = self.trained(2.0)
        body = ml.package(ev, rows, "GC=F", "1h", dict(horizon=10), 7)
        ml.save_model(body, self.dir)
        m = ml.latest_accepted("GC=F", "1h", self.dir)
        self.assertIsNotNone(m)
        strong = [0.0] * len(ml.FEATURES)
        strong[1] = 3.0
        weak = [0.0] * len(ml.FEATURES)
        weak[1] = -3.0
        ps, pw = ml.predict(m, strong), ml.predict(m, weak)
        self.assertGreater(ps["p"], pw["p"])
        self.assertTrue(0 < ps["p"] < 1)
        self.assertEqual(ps["model_id"], body["id"])
        self.assertIn("interval", ps)

    def test_rejected_models_are_never_served(self):
        rows, ev = self.trained(0.0, seed=5)
        body = ml.package(ev, rows, "GC=F", "1h", dict(horizon=10), 7)
        ml.save_model(body, self.dir)
        self.assertIsNone(ml.latest_accepted("GC=F", "1h", self.dir))
        with self.assertRaises(ValueError):
            ml.predict(body, [0.0] * len(ml.FEATURES))

    def test_other_markets_do_not_leak(self):
        rows, ev = self.trained(2.0)
        ml.save_model(ml.package(ev, rows, "GC=F", "1h", {}, 7), self.dir)
        self.assertIsNone(ml.latest_accepted("BTC-USD", "1h", self.dir))
        self.assertIsNone(ml.latest_accepted("GC=F", "15m", self.dir))

    def test_version_id_is_deterministic_and_input_sensitive(self):
        rows, ev = self.trained(2.0)
        a = ml.package(ev, rows, "GC=F", "1h", dict(horizon=10), 7)["id"]
        b = ml.package(ev, rows, "GC=F", "1h", dict(horizon=10), 7)["id"]
        c = ml.package(ev, rows, "GC=F", "1h", dict(horizon=10), 8)["id"]
        d = ml.package(ev, rows, "GC=F", "1h", dict(horizon=12), 7)["id"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_empty_directory(self):
        self.assertIsNone(ml.latest_accepted("GC=F", "1h", os.path.join(self.dir, "none")))


if __name__ == "__main__":
    unittest.main()
