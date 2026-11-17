"""Phase 14: Gold Profitability Intelligence. Standard library only. Read only: nothing here places or changes an order.

This module runs NO new experiment. It reads the results the earlier phases saved and arranges them into eight sections (A to H), so that every
number on the dashboard can be traced to a pre-registered test or to the recorded live scorecard. Each section carries a source label:

  historical backtest   a replay of the rules on past bars (6 months, delayed Yahoo bars, flat costs)
  live record           plans recorded when the console showed them, graded later (a forward record, still small)
  forward simulation    not available yet: no simulated forward run has been produced

Nothing here is a forecast, a probability or advice. Where a quantity is not known the dashboard says so.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "trader")
HB, LR, FS = "historical backtest", "live record", "forward simulation"


def _load(name, default=None):
    try:
        with open(os.path.join(T, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def verdict(band, mean):
    """Plain verdict from a corrected bootstrap band on the held-out test period."""
    if mean is None or not band:
        return "UNKNOWN", "too few test trades to say"
    if band[1] < 0:
        return "LOSING", "the whole corrected band is below zero"
    if band[0] > 0:
        return "PROFITABLE", "the whole corrected band is above zero"
    return "INCONCLUSIVE", "the band spans zero: cannot tell"


def build(e1, e2, e3, replay, live, log, strict):
    """All arguments are saved documents (any may be None). Returns the dashboard document."""
    miss = [n for n, v in (("Regime, trend and breakout experiments", e1), ("Stops, grading and strict mode experiments", e2), ("Session, news and machine-learning filter experiments", e3),
                           ("Backtest every recommendation", replay)) if not v]
    doc = dict(missing=miss, labels=dict(historical=HB, live=LR, forward=FS))
    if miss:
        doc["note"] = "Run these first: " + "; ".join(miss) + "."
    e1h, e2h, e3h = (e1 or {}).get("hypotheses", []), (e2 or {}).get("hypotheses", []), (e3 or {}).get("hypotheses", [])
    base_te = next((h["test"] for h in e3h if h["id"] == "BASE10"), None)
    band, mean = (base_te or {}).get("band_corrected"), (base_te or {}).get("mean_r")
    v, why = verdict(band, mean)
    att = (replay or {}).get("attribution") or {}
    sa, sd = att.get("stats_all") or {}, att.get("stats_deduplicated") or {}
    gold = (((replay or {}).get("summary") or {}).get("by") or {}).get("symbol", {}).get("GC=F")
    lv = sc_summary(live)
    adopted = [h for h in e1h + e2h + e3h if h.get("decision") == "ADOPTED" and h.get("id") not in ("H7",)]

    # ---- A strategy health
    doc["A"] = dict(title="Strategy health", source=HB, verdict=v, verdict_reason=why,
                    headline=dict(test_mean_r=mean, test_band=band, test_trades=(base_te or {}).get("trades"), profit_factor=(base_te or {}).get("profit_factor"),
                                  max_drawdown_r=(base_te or {}).get("max_drawdown_r"), replay_mean_r=sa.get("mean_r"), replay_completed=sa.get("completed"),
                                  replay_dedup_mean_r=sd.get("mean_r"), replay_dedup_completed=sd.get("completed"), win_rate=sa.get("win_rate"), win_ci=sa.get("win_ci")),
                    gold=_agg_view(gold), live=lv, improvements_adopted=[h["id"] + ": " + h["name"] for h in adopted],
                    hypotheses_tested=(e3 or {}).get("hypotheses_cumulative"),
                    strict_mode=bool((strict or {}).get("strict")))

    # ---- B loss diagnostics
    causes = [dict(label=c["label"], supported=c["supported"], unknown=c["unknown"], share=(c["supported"] / (att.get("losses") or {}).get("count", 1) if (att.get("losses") or {}).get("count") else None))
              for c in ((att.get("losses") or {}).get("causes") or [])]
    doc["B"] = dict(title="Loss diagnostics", source=HB, losses=(att.get("losses") or {}).get("count"), causes=sorted(causes, key=lambda c: -c["supported"]), fill_gap=att.get("fill_gap"),
                    audit=att.get("audit"), counts=att.get("counts"),
                    note="A cause is listed only where recorded evidence supports it. Causes overlap (one loss can have several), so shares do not add to 100%. Supported by evidence is not the same as proven cause.")

    # ---- C signal quality
    doc["C"] = dict(title="Signal quality", source=HB, strategies=(e2 or {}).get("strategies", []), score_vs_grade=(e2 or {}).get("score_vs_grade"), grading_policy=(e2 or {}).get("grading_policy"),
                    ml=dict(test_auc=((e3 or {}).get("ml") or {}).get("test_auc"), brier_skill=((e3 or {}).get("ml") or {}).get("brier_skill"), probability_status=((e3 or {}).get("ml") or {}).get("probability_status", "INSUFFICIENT DATA")),
                    note="Evidence grades come from the held-out test period. Probabilities are shown only if a calibrated model passed acceptance; none has.")

    # ---- D opportunity ranking: by test evidence, with the sample and the band. Never a trade instruction.
    rank = []
    for s in (e2 or {}).get("strategies", []):
        te, tr = s["test"], s["train"]
        vv, _ = verdict(te.get("band_corrected"), te.get("mean_r"))
        rank.append(dict(name=s["name"], test_mean_r=te.get("mean_r"), test_trades=te.get("trades"), band=te.get("band_corrected"), train_mean_r=tr.get("mean_r"), evidence_grade=s["grade"], verdict=vv,
                         stable=(te.get("mean_r") is not None and tr.get("mean_r") is not None and (te["mean_r"] > 0) == (tr["mean_r"] > 0))))
    rank.sort(key=lambda r: -(r["test_mean_r"] if r["test_mean_r"] is not None else -9))
    doc["D"] = dict(title="Opportunity ranking", source=HB, ranking=rank, any_qualifies=any(r["evidence_grade"] == "A" for r in rank),
                    note=("Ranked by held-out test average. No strategy has grade A, so this is a list of what looked least bad, not a list of what to trade. A positive test average whose sign "
                          "flipped from the earlier period is marked unstable." if not any(r["evidence_grade"] == "A" for r in rank) else "Strategies graded A are the only ones strict mode allows."))

    # ---- E execution quality
    cnt = att.get("counts") or {}
    tot = cnt.get("recommendations") or 0
    doc["E"] = dict(title="Execution quality", source=HB, counts=cnt,
                    rates=dict(triggered=(cnt.get("triggered", 0) / tot if tot else None), not_triggered=(cnt.get("not_triggered", 0) / tot if tot else None), invalidated=(cnt.get("invalidated", 0) / tot if tot else None)),
                    fill_gap=att.get("fill_gap"), mae=(e2 or {}).get("mae"), engine=dict(agreement=((e3 or {}).get("engine") or {}).get("agreement"), compared=((e3 or {}).get("engine") or {}).get("compared")),
                    audit=att.get("audit"), executed_on_a_broker=cnt.get("executed_on_a_broker", 0),
                    unknown=["spread and slippage (no bid or ask in this data; costs are flat)", "broker fill and rejection behaviour (nothing was executed on a broker)", "real tick-level order of stop and target inside a bar (assumed stop first)"])

    # ---- F profitability comparison across every variant ever tested
    rows = []
    for ph_doc, tag in ((e1h, "Phases 3 to 5"), (e2h, "Phases 6 to 9"), (e3h, "Phases 10 to 12")):
        for h in ph_doc:
            te = h.get("test") or {}
            rows.append(dict(id=h["id"], phase=h["phase"], group=tag, name=h["name"], decision=h["decision"], test_trades=te.get("trades"), test_mean_r=te.get("mean_r"),
                             band=te.get("band_corrected") or te.get("band"), train_mean_r=(h.get("train") or {}).get("mean_r"), max_drawdown_r=te.get("max_drawdown_r")))
    rows = [r for r in rows if r["test_mean_r"] is not None]
    doc["F"] = dict(title="Profitability comparison", source=HB, rows=sorted(rows, key=lambda r: -r["test_mean_r"]), positive=len([r for r in rows if r["test_mean_r"] > 0]), total=len(rows),
                    note="Sorted by held-out test average per trade, best first. A variant near the top of a list of many is expected by chance; only the decision column counts.")

    # ---- G capital protection
    gate = (e3 or {}).get("gate")
    dd = (e2 or {}).get("dedup")
    doc["G"] = dict(title="Capital protection", source=HB, strict_mode=bool((strict or {}).get("strict")), strict_vs_baseline=(e2 or {}).get("strict_vs_baseline"), gate=gate, dedup=dd,
                    controls=["Strict mode (setting, off by default): allows only strategies graded A, so right now it would allow nothing.",
                              "Risk limits as replayed: 0.25% per trade, at most 3 open, 1% aggregate, one per market and side, 1.5% daily lockout.",
                              "No martingale and no sizing up after losses anywhere in this system.",
                              "Read only: the console never places an order; the MT5 project is separate and demo only."],
                    note="Limits reduce exposure and drawdown; they do not change the average result per trade, so they cannot turn a negative average into a positive one.")

    # ---- H optimisation history
    entries = []
    for e in (log or []):
        for h in e.get("hypotheses", []):
            entries.append(dict(run=e.get("created"), kind=e.get("kind"), id=h["id"], name=h["name"], decision=h["decision"], test_mean_r=h.get("test_mean_r"), test_trades=h.get("test_trades")))
    doc["H"] = dict(title="Optimisation history", source=HB, runs=len(log or []), entries=entries[-120:], total_logged=len(entries),
                    adopted=len([x for x in entries if x["decision"] == "ADOPTED"]), rejected=len([x for x in entries if x["decision"] == "REJECTED"]),
                    insufficient=len([x for x in entries if x["decision"] == "INSUFFICIENT SAMPLE"]),
                    note="Every idea tried is listed, including the rejected ones. The multiple-testing correction counts all of them, so the more that is tried the harder the bar gets.")
    doc["forward_simulation"] = "NOT AVAILABLE: no forward simulation has been run. The live record is the only forward evidence, and it is small."
    doc["caution"] = ("Historical backtest figures cover about six months of delayed bars on two markets with flat costs and overlapping ideas, so bands are optimistic. "
                      "Nothing here is a forecast or advice. No order is ever placed from this console.")
    return doc


def _agg_view(a):
    if not a:
        return None
    return dict(completed=a.get("resolved"), win_rate=a.get("win_rate"), win_ci=a.get("win_ci"), mean_r=a.get("mean_r"), total_r=a.get("total_r"))


def sc_summary(live):
    """Live record: completed plans graded under the current engine. Small by nature."""
    try:
        import scorecard as sc
        recs = [r for r in (live or []) if isinstance(r, dict) and r.get("result")]
        a = sc.summary(recs)["all"]
        return dict(recorded=a["total"], resolved=a["resolved"], wins=a["counts"]["WIN"], losses=a["counts"]["LOSS"], open=a["counts"]["OPEN"], mean_r=a["mean_r"], win_ci=a["win_ci"],
                    enough=a["enough"], label=LR)
    except Exception:
        return dict(recorded=0, resolved=0, label=LR)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Gold Profitability Intelligence: arranges the saved results; runs no new experiment.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    doc = build(_load("experiment_result.json"), _load("experiment2_result.json"), _load("experiment3_result.json"), _load("recommendation_backtest.json"), _load("scorecard.json"),
                _load("experiment_log.json"), _load("strict_mode.json"))
    if a.json:
        print(json.dumps(doc))
    else:
        h = doc["A"]
        print("verdict:", h["verdict"], "-", h["verdict_reason"], "| test mean", h["headline"]["test_mean_r"])
        print("missing:", doc["missing"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
