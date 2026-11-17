"""Phase 14 dashboard builder. SYNTHETIC saved results."""
import unittest

from _load import load

pf = load("webui", "profitability")


def hyp(i, phase, dec, mean, band=None, trades=150):
    return dict(id=i, phase=phase, name="n" + i, definition="d", decision=dec, reasons=["r"], train=dict(mean_r=-0.1, trades=100),
                test=dict(mean_r=mean, trades=trades, band_corrected=band, max_drawdown_r=5.0, profit_factor=0.8))


E3 = dict(hypotheses=[hyp("BASE10", 10, "BASELINE", -0.16, [-0.3, -0.01], 650), hyp("H10b", 10, "REJECTED", -0.13, [-0.3, 0.07])], hypotheses_cumulative=29,
          ml=dict(test_auc=0.57, brier_skill=0.01, probability_status="INSUFFICIENT DATA"), engine=dict(agreement=1.0, compared=10), gate=dict(candidates=5, taken=2))
E2 = dict(hypotheses=[hyp("S1", 6, "REJECTED", -0.1, [-0.2, 0.0]), hyp("H7", 7, "ADOPTED", 0.1)],
          strategies=[dict(name="s-a", kind="pullback", side="long", grade="NO TRADE", grade_reason="neg", train=dict(mean_r=-0.1), test=dict(mean_r=-0.4, trades=200, band_corrected=[-0.6, -0.1])),
                      dict(name="s-b", kind="pullback", side="short", grade="B", grade_reason="meh", train=dict(mean_r=-0.1), test=dict(mean_r=0.1, trades=150, band_corrected=[-0.1, 0.3]))],
          score_vs_grade=dict(by_grade={}), mae=dict(), dedup=None, strict_vs_baseline=None, grading_policy={})
E1 = dict(hypotheses=[hyp("A", 4, "BASELINE", -0.15)])
REPLAY = dict(attribution=dict(counts=dict(recommendations=10, triggered=4, not_triggered=3, invalidated=3, executed_on_a_broker=0), losses=dict(count=4, causes=[dict(label="x", supported=3, unknown=0)]),
                               stats_all=dict(mean_r=-0.16, completed=8), stats_deduplicated=dict(mean_r=-0.13, completed=4)), summary=dict(by=dict(symbol={})))
LOG = [dict(created="2026-01-01T00:00:00Z", kind="phase10-12", hypotheses=[dict(id="H10b", name="n", decision="REJECTED", test_mean_r=-0.1, test_trades=100)])]


class Verdict(unittest.TestCase):
    def test_verdicts_follow_the_corrected_band(self):
        self.assertEqual(pf.verdict([-0.3, -0.01], -0.16)[0], "LOSING")
        self.assertEqual(pf.verdict([0.01, 0.3], 0.1)[0], "PROFITABLE")
        self.assertEqual(pf.verdict([-0.1, 0.2], 0.0)[0], "INCONCLUSIVE")
        self.assertEqual(pf.verdict(None, None)[0], "UNKNOWN")


class Build(unittest.TestCase):
    def doc(self, **kw):
        a = dict(e1=E1, e2=E2, e3=E3, replay=REPLAY, live=[], log=LOG, strict=None)
        a.update(kw)
        return pf.build(a["e1"], a["e2"], a["e3"], a["replay"], a["live"], a["log"], a["strict"])

    def test_all_eight_sections_and_labels_exist(self):
        d = self.doc()
        for k in "ABCDEFGH":
            self.assertIn(k, d)
            self.assertTrue(d[k]["source"])
        self.assertEqual(d["A"]["verdict"], "LOSING")
        self.assertIn("NOT AVAILABLE", d["forward_simulation"])

    def test_nothing_is_adopted_so_no_improvement_is_claimed_and_h7_is_not_one(self):
        d = self.doc()
        self.assertEqual(d["A"]["improvements_adopted"], [])
        self.assertEqual(d["H"]["adopted"], 0)

    def test_ranking_sorted_by_test_average_and_never_claims_a_qualifier(self):
        d = self.doc()
        self.assertEqual([r["name"] for r in d["D"]["ranking"]], ["s-b", "s-a"])
        self.assertFalse(d["D"]["any_qualifies"])
        self.assertIn("not a list of what to trade", d["D"]["note"])

    def test_missing_inputs_degrade_instead_of_crashing(self):
        d = self.doc(e1=None, e2=None, e3=None, replay=None, log=None)
        self.assertEqual(len(d["missing"]), 4)
        self.assertEqual(d["A"]["verdict"], "UNKNOWN")
        self.assertEqual(d["F"]["rows"], [])

    def test_strict_mode_flag_is_carried(self):
        self.assertTrue(self.doc(strict=dict(strict=True))["G"]["strict_mode"])

    def test_comparison_lists_every_variant_best_first(self):
        d = self.doc()
        means = [r["test_mean_r"] for r in d["F"]["rows"]]
        self.assertEqual(means, sorted(means, reverse=True))
        self.assertEqual(d["F"]["total"], 5)


if __name__ == "__main__":
    unittest.main()
