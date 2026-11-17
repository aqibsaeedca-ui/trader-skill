"""Strict mode and the evidence-based grade. Standard library only. Read only: it never places or changes an order.

STRICT MODE is a local setting, off by default, so the original (baseline) behaviour is unchanged until you switch it on. When it is on, a
recommendation is ALLOWED only if its strategy (plan kind and side) has evidence grade A from the Phase 6 to 9 experiments:

  A         validated out-of-sample (test) with enough observations, corrected band above zero, positive train record
  B         plausible, not statistically negative, but below the deployment evidence threshold
  C         unvalidated (too little data, or otherwise unproven)
  NO TRADE  statistically negative on the test period

The grade is a policy over recorded results, not a probability. Where no calibrated model exists the probability reads INSUFFICIENT DATA.
With no strategy at grade A, strict mode allows nothing, and that is the intended behaviour: the engine may stay silent for long periods.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "strict_mode.json")


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        return dict(strict=bool(d.get("strict")))
    except (OSError, ValueError, AttributeError):
        return dict(strict=False)


def save(strict, path=None):
    if not isinstance(strict, bool):
        raise ValueError("strict must be true or false")
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(dict(strict=strict), f)
    os.replace(p + ".tmp", p)
    return dict(strict=strict)


def grades(result=None):
    """{(kind, side): (grade, reason)} from the saved Phase 6 to 9 result. Without a result every strategy is C, unvalidated."""
    if result is None:
        try:
            import experiments2
            result = experiments2.load_result()
        except Exception:
            result = None
    out = {}
    for s in (result or {}).get("strategies", []):
        out[(s["kind"], s["side"])] = (s["grade"], s["grade_reason"])
    return out


def check(kind, side, strict=None, result=None):
    """Evidence grade and whether the recommendation is allowed under the current mode."""
    g = grades(result).get((kind, side), ("C", "no experiment result is saved yet, so the strategy is unvalidated"))
    strict = load()["strict"] if strict is None else strict
    allowed = (g[0] == "A") if strict else True
    return dict(evidence_grade=g[0], evidence_reason=g[1], strict_allowed=allowed, probability="INSUFFICIENT DATA",
                note=("" if allowed else "Strict mode is on and this strategy is not graded A, so no recommendation is allowed."))
