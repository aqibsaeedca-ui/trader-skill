"""Backtest of every recommendation: causality of the replay, dedupe and grading plumbing. Bars are SYNTHETIC."""
import os
import random
import shutil
import tempfile
import unittest

from _load import load

rb = load("webui", "recommendation_backtest")
sc = load("webui", "scorecard")

END = 1_760_000_000 // 86400 * 86400            # a UTC midnight, so every grid lines up


def series(n, step, price=100.0, seed=1):
    rnd = random.Random(seed)
    out, p = [], price
    for i in range(n):
        t = END - (n - i) * step
        o = p
        p = max(5.0, p + rnd.uniform(-1, 1))
        out.append((t, max(o, p) + 0.3, min(o, p) - 0.3, p, o))
    return out


D, W, H1, M15 = series(150, 86400), series(90, 604800), series(500, 3600), series(300, 900)


def plan(entry=100.0, stop=95.0, rank=1):
    return dict(name="LONG on a pullback", side="long", kind="pullback", entry=entry, stop=stop, targets=[dict(price=105.0), dict(price=110.0), dict(price=115.0)], rank=rank, grade="A", score=7.0)


class Replay(unittest.TestCase):
    def test_decision_points_are_ordered_and_use_15m_only_where_it_exists(self):
        pts = rb.decision_points(M15, H1, every=4)
        times = [t for t, _ in pts]
        self.assertEqual(times, sorted(times))
        first15 = M15[rb.MIN_BARS][0] + 900
        self.assertTrue(all(w for t, w in pts if t >= first15))
        self.assertTrue(all((not w) for t, w in pts if t < first15))

    def test_the_newest_bar_which_may_still_be_forming_is_never_a_decision_point_or_graded(self):
        pts = rb.decision_points(M15, H1, every=1)
        self.assertLessEqual(pts[-1][0], M15[-1][0])
        recs, _ = rb.run_symbol("GC=F", D, W, H1, M15, every=4, analyse_bars=lambda *a: dict(setup={"15m": dict(plans=[plan()])}))
        self.assertTrue(all(r["result"]["checked_to"] is None or r["result"]["checked_to"] <= sc._iso(M15[-1][0]) for r in recs))

    def test_every_analysis_sees_only_bars_closed_at_the_decision_time(self):
        seen = []

        def spy(symbol, d, w, h1, m15):
            seen.append((d[-1][0] + 86400, w[-1][0] + 604800, h1[-1][0] + 3600, (m15[-1][0] + 900) if m15 else None))
            return dict(setup={})
        rb.run_symbol("GC=F", D, W, H1, M15, every=4, analyse_bars=spy)
        self.assertGreater(len(seen), 50)
        pts = [t for t, _ in rb.decision_points(M15, H1, 4)]
        for T, got in zip(pts, seen):
            for closes in got:
                if closes is not None:
                    self.assertLessEqual(closes, T)

    def test_the_same_plan_is_recorded_once_and_then_graded(self):
        def fake(symbol, d, w, h1, m15):
            return dict(setup={"1h": dict(plans=[plan()])})
        recs, cov = rb.run_symbol("GC=F", D, W, H1, M15, every=4, analyse_bars=fake)
        self.assertGreaterEqual(len(recs), 1)
        self.assertLess(len(recs), cov["decision_points"])            # dedupe: not one record per decision point
        self.assertTrue(all(r["result"]["state"] for r in recs))
        self.assertTrue(all(r["source"] == "replay" for r in recs))
        self.assertEqual(cov["skipped"], 0)

    def test_a_failing_analysis_is_skipped_not_fatal(self):
        def boom(*a):
            raise SystemExit("not enough bars")
        recs, cov = rb.run_symbol("GC=F", D, W, H1, M15, every=4, analyse_bars=boom)
        self.assertEqual((recs, cov["decision_points"]), ([], 0))
        self.assertGreater(cov["skipped"], 0)

    def test_run_saves_a_document_the_scorecard_can_show(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "rb.json")

            def fake(symbol, dd, w, h1, m15):
                return dict(setup={"1h": dict(plans=[plan()])})
            doc = rb.run(symbols=("GC=F",), every=8, fetch=lambda s: (D, W, H1, M15), path=p, analyse_bars=fake)
            self.assertEqual(doc["kind"], "replay")
            self.assertIn("overlap", doc["caution"])
            self.assertTrue(rb.load_doc(p))
            self.assertEqual(rb.load_doc(os.path.join(d, "missing.json")), None)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_data_failure_is_reported_in_the_document(self):
        def bad(s):
            raise SystemExit("offline")
        d = tempfile.mkdtemp()
        try:
            doc = rb.run(symbols=("GC=F",), fetch=bad, path=os.path.join(d, "x.json"))
            self.assertEqual(len(doc["errors"]), 1)
            self.assertFalse(os.path.exists(os.path.join(d, "x.json")))      # nothing saved when nothing ran
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Intraday(unittest.TestCase):
    def test_analysis_works_without_15_minute_history_and_with_it(self):
        gi = load("webui", "gold_intraday")
        a = gi.analyse_bars("GC=F", D, W, H1, [])
        self.assertNotIn("15m", a["setup"])
        self.assertIn("1h", a["setup"])
        b = gi.analyse_bars("GC=F", D, W, H1, M15)
        self.assertIn("15m", b["setup"])
        with self.assertRaises(SystemExit):
            gi.analyse_bars("GC=F", D, W, H1, M15[:10])


if __name__ == "__main__":
    unittest.main()
