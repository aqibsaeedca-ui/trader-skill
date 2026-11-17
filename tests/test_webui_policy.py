"""Strict mode setting, evidence grade lookup and the scorecard rows that carry them. SYNTHETIC results."""
import os
import shutil
import tempfile
import unittest

from _load import load

sp = load("webui", "strategy_policy")
sc = load("webui", "scorecard")

RESULT = dict(strategies=[
    dict(kind="pullback", side="long", grade="NO TRADE", grade_reason="statistically negative"),
    dict(kind="pullback", side="short", grade="A", grade_reason="validated"),
    dict(kind="breakout", side="long", grade="C", grade_reason="unvalidated"),
    dict(kind="breakout", side="short", grade="B", grade_reason="plausible")])


class Setting(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "strict.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_default_is_off_and_round_trips(self):
        self.assertEqual(sp.load(self.p), dict(strict=False))
        sp.save(True, self.p)
        self.assertTrue(sp.load(self.p)["strict"])
        sp.save(False, self.p)
        self.assertFalse(sp.load(self.p)["strict"])

    def test_only_a_real_boolean_is_accepted_and_junk_files_mean_off(self):
        for bad in ("yes", 1, None, "true"):
            with self.assertRaises(ValueError):
                sp.save(bad, self.p)
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertFalse(sp.load(self.p)["strict"])


class Check(unittest.TestCase):
    def test_baseline_mode_allows_everything_but_still_reports_the_grade(self):
        for kind, side in (("pullback", "long"), ("breakout", "short")):
            c = sp.check(kind, side, strict=False, result=RESULT)
            self.assertTrue(c["strict_allowed"])
        self.assertEqual(sp.check("pullback", "long", strict=False, result=RESULT)["evidence_grade"], "NO TRADE")

    def test_strict_mode_allows_only_grade_a(self):
        allowed = {(k, s): sp.check(k, s, strict=True, result=RESULT)["strict_allowed"] for k in ("pullback", "breakout") for s in ("long", "short")}
        self.assertEqual([k for k, v in allowed.items() if v], [("pullback", "short")])
        self.assertIn("Strict mode is on", sp.check("pullback", "long", strict=True, result=RESULT)["note"])

    def test_without_a_saved_result_every_strategy_is_unvalidated_and_strict_blocks_all(self):
        c = sp.check("pullback", "long", strict=True, result=dict(strategies=[]))
        self.assertEqual((c["evidence_grade"], c["strict_allowed"]), ("C", False))
        self.assertEqual(c["probability"], "INSUFFICIENT DATA")


class ScorecardRows(unittest.TestCase):
    def test_rows_carry_the_evidence_grade_and_permission(self):
        r = sc.make_record("GC=F", "1h", dict(name="x", side="long", kind="pullback", entry=100.0, stop=95.0, targets=[dict(price=105.0), dict(price=110.0), dict(price=115.0)], rank=1, grade="A", score=7.0), 1_700_000_000)
        rows = sc.view([r])
        self.assertIn(rows[0]["evidence_grade"], ("A", "B", "C", "NO TRADE"))
        self.assertIn("strict_allowed", rows[0])
        doc = sc.document([r], 1_700_000_100)
        self.assertIn("strict_mode", doc)


if __name__ == "__main__":
    unittest.main()
