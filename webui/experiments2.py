"""Phases 6 to 9: stops and exits, evidence-based grading, opportunity de-duplication accounting, strict mode versus baseline.
Standard library only. Read only: nothing here places or changes an order.

Every rule, grid, split and decision criterion was fixed in webui/docs/PHASE_6_9_PREREGISTRATION.md BEFORE these experiments were run.
Each hypothesis is evaluated on the TEST split once and logged, rejected ones included. Entries are identical across variants (the baseline's fill
price and bar); only the stop or the exit rule changes. R is measured in each variant's own initial risk, entry to its own stop.
"""
import bisect
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experiments as ex  # noqa: E402
import loss_attribution as la  # noqa: E402
import scorecard as sc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALL = os.path.join(ROOT, "trader", "replay_all.json")
RESULT = os.path.join(ROOT, "trader", "experiment2_result.json")
LOG = os.path.join(ROOT, "trader", "experiment_log.json")
N_TOTAL = 12 + 13                       # every hypothesis tested on this data so far
ALPHA = 0.05 / N_TOTAL
PERM_SCORE = 5000
STOP_ATR = (1.0, 1.5, 2.0)
SWING_PAD, TRAIL_ATR, TRAIL_AFTER_R, TIME_BARS, FIXED_R, ATR_TARGET = 0.25, 1.5, 1.0, 12, 1.5, 2.0
STRATS = (("pullback", "long"), ("pullback", "short"), ("breakout", "long"), ("breakout", "short"))
NAMES = {("pullback", "long"): "LONG on a pullback", ("pullback", "short"): "SHORT on a rally into resistance",
         ("breakout", "long"): "LONG on a breakout", ("breakout", "short"): "SHORT on a breakdown"}


# ---------------------------------------------------------------- exit simulator -----------------------------------------------------------

def simulate_exit(rec, seq, i0, fill, limit_bar, stop, exit_spec, atr=None, h1=None):
    """Run a filled trade from bar i0 under a stop and an exit rule. exit_spec['kind'] is one of
    plan, plan_nobe, single, trail, time, regime. Returns a dict with final, r_net, fill_time, exit_time, mae (in the variant's own R)."""
    step, sgn = sc.STEP[rec["tf"]], (1 if rec["side"] == "long" else -1)
    entry, tg = rec["entry"], rec["targets"]
    risk = abs(entry - stop)
    kind = exit_spec["kind"]
    legs, rem, real, k, stop_cur, closed, bars_in, last_close = [], 1.0, 0.0, 0, stop, False, 0, None
    best = fill
    trail_on = False
    mae = 0.0
    why = ""
    hold = TIME_BARS if kind == "time" else rec["hold_bars"]
    pnl = lambda px: (px - fill) * sgn / risk   # noqa: E731
    for idx in range(i0, len(seq)):
        t, hi, lo, cl, op = seq[idx]
        last_close = cl
        bars_in += 1
        mae = max(mae, ((fill - lo) if sgn == 1 else (hi - fill)) / risk)
        if (lo <= stop_cur) if sgn == 1 else (hi >= stop_cur):
            px = min(stop_cur, op) if sgn == 1 else max(stop_cur, op)
            real += rem * pnl(px)
            rem, closed, why = 0.0, True, "stop"
            exit_t = t + step
            break
        if limit_bar:
            limit_bar = False
            best = max(best, hi) if sgn == 1 else min(best, lo)
            continue
        if kind in ("plan", "plan_nobe", "regime"):
            while k < 3 and rem > 1e-9 and ((hi >= tg[k]) if sgn == 1 else (lo <= tg[k])):
                real += sc.SPLIT[k] * pnl(tg[k])
                rem -= sc.SPLIT[k]
                k += 1
                if k == 1 and kind != "plan_nobe":
                    stop_cur = fill
            if k >= 1 and kind != "plan_nobe" and rem > 1e-9 and ((lo <= stop_cur) if sgn == 1 else (hi >= stop_cur)):
                rem, closed, why, exit_t = 0.0, True, "stop at entry", t + step
                break
            if k == 3 or rem <= 1e-9:
                closed, why, exit_t = True, "all targets", t + step
                break
        elif kind == "single":
            px = exit_spec["price"]
            if (hi >= px) if sgn == 1 else (lo <= px):
                real += rem * pnl(px)
                rem, closed, why, exit_t = 0.0, True, "target", t + step
                break
        elif kind == "trail":
            if trail_on:
                trail = (best - TRAIL_ATR * atr) if sgn == 1 else (best + TRAIL_ATR * atr)
                stop_cur = max(stop_cur, trail) if sgn == 1 else min(stop_cur, trail)
        if kind == "trail":
            best = max(best, hi) if sgn == 1 else min(best, lo)
            if not trail_on and (best - fill) * sgn >= TRAIL_AFTER_R * risk:
                trail_on = True
        if kind == "regime" and rem > 1e-9 and h1 is not None:
            sn = h1.snap(t + step)
            if sn and sn.get("bias") == ("bearish" if sgn == 1 else "bullish"):
                real += rem * pnl(cl)
                rem, closed, why, exit_t = 0.0, True, "regime turned", t + step
                break
        if bars_in >= hold:
            real += rem * pnl(cl)
            rem, closed, why, exit_t = 0.0, True, "time", t + step
            break
    cost_r = rec["cost"] / risk
    final = closed
    if not closed:
        real += rem * (pnl(last_close) if last_close is not None else 0.0)
        exit_t, why = None, "open"
    return dict(final=final, r_net=real - cost_r, r_gross=real, fill_time=sc._iso(seq[i0][0]), exit_time=sc._iso(exit_t) if exit_t else None, mae=mae,
                best_target=k, why=why, risk=risk)


# ---------------------------------------------------------------- variants -------------------------------------------------------------------

def stop_variants(rec, f):
    """{id: stop price or None} for S1..S4. None when it cannot be built from causal data."""
    sgn = 1 if rec["side"] == "long" else -1
    entry, atr = rec["entry"], f.get("tf_atr")
    out = {}
    for n, m in enumerate(STOP_ATR, 1):
        out["S%d" % n] = (entry - sgn * m * atr) if atr else None
    s1 = f.get("s1") or {}
    swing = s1.get("swing_low") if sgn == 1 else s1.get("swing_high")
    atr1 = s1.get("atr")
    if swing is not None and atr1 and (entry - swing) * sgn > 0:
        out["S4"] = swing - sgn * SWING_PAD * atr1
    else:
        out["S4"] = rec["stop"]
    return out


def exit_variants(rec, f):
    """{id: (stop, exit_spec)} for X1..X7 with the plan stop."""
    sgn = 1 if rec["side"] == "long" else -1
    stop, entry, atr = rec["stop"], rec["entry"], f.get("tf_atr")
    risk = abs(entry - stop)
    return {"X1": (stop, dict(kind="single", price=entry + sgn * FIXED_R * risk)),
            "X2": (stop, dict(kind="single", price=rec["targets"][0])),
            "X3": (stop, dict(kind="single", price=entry + sgn * ATR_TARGET * atr) if atr else None),
            "X4": (stop, dict(kind="plan_nobe")),
            "X5": (stop, dict(kind="trail") if atr else None),
            "X6": (stop, dict(kind="time")),
            "X7": (stop, dict(kind="regime"))}


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for q in range(i, j + 1):
                r[order[q]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sxx * syy) ** 0.5


def bucket_grade(train, test, alpha):
    """The pre-registered evidence grade for one strategy from its TRAIN and TEST metrics."""
    n, band = test["trades"], test.get("band_corrected")
    if n >= 100 and band and band[0] > 0 and train["mean_r"] is not None and train["mean_r"] > 0:
        return "A", "at least 100 test fills, corrected band above zero and a positive train mean"
    if n >= 100 and band and band[1] < 0:
        return "NO TRADE", "at least 100 test fills and the corrected band for mean net R lies entirely below zero"
    if n >= 30 and test["mean_r"] is not None and not (band and band[1] < 0):
        return "B", "at least 30 test fills, not statistically negative, below the deployment evidence threshold"
    return "C", "fewer than 30 test fills or otherwise unvalidated"


def train_only_grade(train, alpha):
    if train["trades"] >= 100 and train.get("band_corrected") and train["band_corrected"][0] > 0 and train["mean_r"] > 0:
        return "A"
    return "other"


def concurrency_peak(trades):
    """Peak number of simultaneously open 1R positions. trades: (fill_iso, exit_iso)."""
    ev = []
    for f, x in trades:
        ev.append((f, 1))
        ev.append((x or "9999", -1))
    ev.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def account(trades_rows):
    """trades_rows: dicts with fill_time, exit_time, r_net, cost_r. Mechanical accounting only."""
    rows = sorted(trades_rows, key=lambda r: r["exit_time"] or "")
    eq = peak = dd = 0.0
    for r in rows:
        eq += r["r_net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dict(trades=len(rows), total_cost_r=sum(r["cost_r"] or 0.0 for r in rows), total_net_r=sum(r["r_net"] for r in rows), max_drawdown_r=dd,
                peak_concurrent_r=concurrency_peak([(r["fill_time"], r["exit_time"]) for r in rows]),
                mean_r=(sum(r["r_net"] for r in rows) / len(rows) if rows else None))


# ---------------------------------------------------------------- the run --------------------------------------------------------------------

def run(reps, ctxs, bars_by_symbol, events, allrecs, now, log_path=None):
    reps = sorted(reps, key=lambda r: r["created_ts"])
    cut = reps[int(ex.SPLIT_FRAC * len(reps))]["created_ts"] if reps else 0
    F = {r["id"]: ex.features(r, ctxs, events) for r in reps}
    hyps = []

    def metrics_for(items):
        return ex.metrics(items[0], items[1], alpha=ALPHA)

    def base_trade(r):
        res = r.get("result") or {}
        return (res["fill_time"], res["r_net"]) if res.get("final") and res.get("state") in ex.FINAL_FILLED and res.get("r_net") is not None else None

    # ---- Phase 6 population: opportunities that filled under the baseline rule
    pop = [r for r in reps if (r.get("result") or {}).get("fill_price") is not None]

    def prepare(r):
        res = r["result"]
        ctx = ctxs.get((r["symbol"], r["tf"]))
        ts = sc._iso  # noqa: F841
        if ctx is None:
            return None
        fill_ts = la._ts(res["fill_time"])
        i = ctx.index_at(fill_ts)
        if i is None:
            return None
        seq = [b for b in bars_by_symbol[r["symbol"]][r["tf"]] if b[0] >= r["created_ts"] and b[0] + sc.STEP[r["tf"]] <= now]
        j = next((q for q, b in enumerate(seq) if b[0] == fill_ts), None)
        return None if j is None else (seq, j, res["fill_price"], r["kind"] == "pullback")

    prep = {r["id"]: prepare(r) for r in pop}

    def evaluate(vid, make):
        """make(rec, feats) -> (stop, exit_spec) or None. Returns TRAIN and TEST metrics plus skip and open counts."""
        out = {}
        for split in ("train", "test"):
            sel = [r for r in pop if (r["created_ts"] < cut) == (split == "train") and prep.get(r["id"])]
            trades, skipped, opened = [], 0, 0
            for r in sel:
                spec = make(r, F[r["id"]])
                if not spec or spec[1] is None or spec[0] is None:
                    skipped += 1
                    continue
                seq, j, fill, lb = prep[r["id"]]
                stop = spec[0]
                sgn = 1 if r["side"] == "long" else -1
                if (fill - stop) * sgn <= 0 or abs(r["entry"] - stop) <= 0:
                    skipped += 1
                    continue
                atr = F[r["id"]].get("tf_atr")
                res = simulate_exit(r, seq, j, fill, lb, stop, spec[1], atr, ctxs.get((r["symbol"], "1h")))
                if not res["final"]:
                    opened += 1
                    continue
                trades.append((res["fill_time"], res["r_net"]))
            m = ex.metrics(trades, len(sel) - skipped - opened, alpha=ALPHA)
            m["skipped"], m["open_excluded"] = skipped, opened
            out[split] = m
        return out

    base_ev = evaluate("BASE", lambda r, f: (r["stop"], dict(kind="plan")))
    hyps.append(dict(phase=6, id="BASE6", name="Baseline: plan stop, plan exit (50/30/20, breakeven after target 1)", definition="The recommendation as it is today.",
                     train=base_ev["train"], test=base_ev["test"], decision="BASELINE", reasons=["reference for the comparison"]))

    def add(phase, hid, name, definition, ev):
        dec, why = ex.decide(ev["train"], ev["test"], base_ev["train"], base_ev["test"])
        hyps.append(dict(phase=phase, id=hid, name=name, definition=definition, train=ev["train"], test=ev["test"], decision=dec, reasons=why))

    for hid, name, df in (("S1", "Stop: 1.0 ATR from the entry", "ATR of the plan's chart, plan exit and targets unchanged."), ("S2", "Stop: 1.5 ATR from the entry", "Same, 1.5 ATR."), ("S3", "Stop: 2.0 ATR from the entry", "Same, 2.0 ATR."),
                          ("S4", "Stop: beyond the H1 swing", "0.25 H1 ATR beyond the latest H1 swing low (long) or high (short) when it lies on the correct side, else the plan stop.")):
        add(6, hid, name, df, evaluate(hid, lambda r, f, hid=hid: (stop_variants(r, f).get(hid), dict(kind="plan"))))
    for hid, name, df in (("X1", "Exit: fixed 1.5R, whole position", "One target at 1.5 times the plan risk."), ("X2", "Exit: structural target 1, whole position", "One target at the plan's first target."),
                          ("X3", "Exit: 2 ATR target, whole position", "One target at 2.0 ATR from the entry."), ("X4", "Exit: partials, no breakeven move", "50/30/20 at the plan targets, stop stays at the plan stop."),
                          ("X5", "Exit: trailing stop", "After +1R the stop trails 1.5 ATR behind the best price of earlier bars; no fixed target."), ("X6", "Exit: time, 12 bars", "Close at the close of the 12th bar, plan stop, no target."),
                          ("X7", "Exit: regime-invalidated", "Plan exit, plus exit at the close of the first bar where the H1 bias turns against the trade.")):
        add(6, hid, name, df, evaluate(hid, lambda r, f, hid=hid: exit_variants(r, f).get(hid)))

    # descriptive: maximum adverse excursion in the plan's own R, by baseline outcome, and results by session
    mae = {"winners": [], "losers": []}
    for r in pop:
        p = prep.get(r["id"])
        if not p or not (r["result"].get("final") and r["result"]["state"] in ("WIN", "LOSS")):
            continue
        seq, j, fill, lb = p
        res = simulate_exit(r, seq, j, fill, lb, r["stop"], dict(kind="plan"), None, None)
        mae["winners" if r["result"]["state"] == "WIN" else "losers"].append(res["mae"])
    pct = lambda v, q: (sorted(v)[min(len(v) - 1, int(q * len(v)))] if v else None)   # noqa: E731
    mae_doc = {k: dict(n=len(v), p50=pct(v, 0.5), p75=pct(v, 0.75), p90=pct(v, 0.9)) for k, v in mae.items()}
    sess = {}
    for r in pop:
        t = base_trade(r)
        if t:
            s = F[r["id"]]["s1"]["session"] if F[r["id"]].get("s1") else "unknown"
            sess.setdefault(s, []).append(t[1])
    sess_doc = {k: dict(trades=len(v), mean_r=sum(v) / len(v)) for k, v in sorted(sess.items())}

    # ---- Phase 7: does the old score predict net R, and the evidence grade of each strategy
    test_fills = [(r, base_trade(r)) for r in reps if r["created_ts"] >= cut]
    test_fills = [(r, t[1]) for r, t in test_fills if t and r.get("score") is not None]
    rho = spearman([r["score"] for r, _ in test_fills], [v for _, v in test_fills]) if len(test_fills) > 10 else None
    p = None
    if rho is not None:
        rnd = random.Random(21)
        xs, ys = [r["score"] for r, _ in test_fills], [v for _, v in test_fills]
        ge = 0
        for _ in range(PERM_SCORE):
            rnd.shuffle(ys)
            q = spearman(xs, ys)
            if q is not None and q >= rho:
                ge += 1
        p = (ge + 1) / (PERM_SCORE + 1)
    h7_ok = rho is not None and rho > 0 and p is not None and p < ALPHA
    hyps.append(dict(phase=7, id="H7", name="The existing A/B/C score predicts net R", definition="Spearman correlation of the plan's score with net R on TEST fills, 5000-draw permutation test.",
                     train=dict(trades=0, mean_r=None), test=dict(trades=len(test_fills), mean_r=rho, rho=rho, p_value=p, per_opportunity_r=None, recommended=len(test_fills), fill_rate=None, band=None,
                                                                  profit_factor=None, max_drawdown_r=0.0, skipped=0, open_excluded=0),
                     decision="ADOPTED" if h7_ok else "REJECTED",
                     reasons=["rho %s, permutation p %s against the corrected level %.4f" % ("n/a" if rho is None else "%+.3f" % rho, "n/a" if p is None else "%.4f" % p, ALPHA)]))
    by_grade = {}
    for r, v in test_fills:
        by_grade.setdefault(str(r.get("grade")), []).append(v)
    grade_doc = {k: dict(trades=len(v), mean_r=sum(v) / len(v)) for k, v in sorted(by_grade.items())}

    strategies = []
    for kind, side in STRATS:
        sub = [r for r in reps if r["kind"] == kind and r["side"] == side]
        tr = ex.metrics([t for r in sub if r["created_ts"] < cut for t in [base_trade(r)] if t], len([r for r in sub if r["created_ts"] < cut]), alpha=ALPHA)
        te = ex.metrics([t for r in sub if r["created_ts"] >= cut for t in [base_trade(r)] if t], len([r for r in sub if r["created_ts"] >= cut]), alpha=ALPHA)
        g, why = bucket_grade(tr, te, ALPHA)
        strategies.append(dict(kind=kind, side=side, name=NAMES[(kind, side)], train=tr, test=te, grade=g, grade_reason=why, train_only_grade=train_only_grade(tr, ALPHA),
                               probability="INSUFFICIENT DATA"))

    # ---- Phase 8: duplicates accounting on TEST (mechanical, no hypothesis)
    def rows_of(items):
        return [dict(fill_time=x["fill_time"], exit_time=x["exit_time"], r_net=x["r_net"], cost_r=x["cost_r"] or 0.0) for x in items
                if x.get("final") and x.get("state") in ex.FINAL_FILLED and x.get("r_net") is not None and x.get("fill_time")]
    test_all = [x for x in allrecs if x["created_ts"] >= cut]
    rep_ids = {r["id"] for r in reps}
    dedup = {"every_recommendation": account(rows_of(test_all)),
             "first_of_each_opportunity": account(rows_of([x for x in test_all if x["id"] in rep_ids])),
             "recommendations": len(test_all), "opportunities": len({x.get("opportunity") for x in test_all})}

    # ---- Phase 9: strict versus baseline, strict strategies chosen on TRAIN only
    allowed = {(s["kind"], s["side"]) for s in strategies if s["train_only_grade"] == "A"}
    te_reps = [r for r in reps if r["created_ts"] >= cut]
    base_tr = [t for r in te_reps for t in [base_trade(r)] if t]
    strict_tr = [t for r in te_reps if (r["kind"], r["side"]) in allowed for t in [base_trade(r)] if t]
    bm, sm = ex.metrics(base_tr, len(te_reps), alpha=ALPHA), ex.metrics(strict_tr, len([r for r in te_reps if (r["kind"], r["side"]) in allowed]), alpha=ALPHA)
    ok_perf = sm["trades"] >= 100 and sm["mean_r"] is not None and sm["mean_r"] > bm["mean_r"] and sm["band_corrected"] and sm["band_corrected"][0] > 0
    ok_dd = sm["trades"] >= 100 and sm["mean_r"] is not None and sm["mean_r"] >= bm["mean_r"] and sm["max_drawdown_r"] <= 0.7 * bm["max_drawdown_r"]
    if sm["trades"] == 0:
        h9, why9 = "NOT DEMONSTRATED", ["strict mode took no trades in the test period, because no strategy met the evidence criteria on the train period. That preserves capital but is not evidence of better selection."]
    elif sm["trades"] < 100:
        h9, why9 = "NOT DEMONSTRATED", ["only %d strict trades, fewer than the 100 needed" % sm["trades"]]
    else:
        h9, why9 = ("ADOPTED", ["strict mode met the pre-registered success criteria"]) if (ok_perf or ok_dd) else ("REJECTED", ["strict mode did not beat baseline mode on the pre-registered criteria"])
    hyps.append(dict(phase=9, id="H9", name="Strict mode beats baseline mode", definition="Strict mode allows only strategies graded A on the TRAIN period; judged on TEST.", train=dict(trades=0, mean_r=None), test=sm,
                     decision=h9, reasons=why9))
    strict_vs_base = dict(baseline=bm, strict=sm, strict_strategies=sorted(NAMES[k] for k in allowed), verdict=h9)

    doc = dict(created=sc._iso(now), split=dict(fraction=ex.SPLIT_FRAC, cut=sc._iso(cut), train_opportunities=len([r for r in reps if r["created_ts"] < cut]), test_opportunities=len(te_reps)),
               hypotheses_total=13, hypotheses_cumulative=N_TOTAL, bonferroni_alpha=ALPHA, hypotheses=hyps, mae=mae_doc, sessions=sess_doc,
               score_vs_grade=dict(by_grade=grade_doc, rho=rho, p_value=p), strategies=strategies, dedup=dedup, strict_vs_baseline=strict_vs_base,
               grading_policy=dict(
                   how_the_old_grade_works=("The A, B and C grade is a hand-weighted confluence score (zone touches, number of timeframes lining up, trend alignment, best reward to risk, "
                                            "distance to the entry). It is not a probability and not an expected value."),
                   evidence_grade=("A: validated out-of-sample (test) with enough observations; B: plausible but below the evidence threshold; C: unvalidated; NO TRADE: statistically negative "
                                   "or failed risk. Probabilities show INSUFFICIENT DATA because no calibrated model passed acceptance.")),
               policy=("Strict mode allows no strategy right now" if not any(s["grade"] == "A" for s in strategies) else "Strict mode allows: " + ", ".join(s["name"] for s in strategies if s["grade"] == "A")),
               not_tested=["spread-sensitive stop sizing (no bid or ask in this data)", "session-specific stop multipliers (sessions are reported descriptively only)"],
               caution="Six months, two markets, delayed last-trade bars, flat costs. Opportunities are clustered by hand-made rules and still overlap in time, so the bands are optimistic. Nothing is tuned on the test split. Past results are not a promise. Not financial advice.")
    append_log(doc, log_path)
    return doc


def append_log(doc, path=None):
    p = path or LOG
    try:
        with open(p, encoding="utf-8") as f:
            log = json.load(f)
        log = log if isinstance(log, list) else []
    except (OSError, ValueError):
        log = []
    entry = dict(created=doc["created"], kind="phase6-9", split=doc["split"],
                 hypotheses=[dict(phase=h["phase"], id=h["id"], name=h["name"], definition=h["definition"], decision=h["decision"], reasons=h["reasons"],
                                  train_mean_r=h["train"].get("mean_r"), test_mean_r=h["test"].get("mean_r"), test_trades=h["test"].get("trades")) for h in doc["hypotheses"]],
                 strict_policy=doc["policy"])
    key = lambda e: [(h["id"], h["decision"], h["test_mean_r"], h["test_trades"]) for h in e["hypotheses"]]   # noqa: E731
    prev = [e for e in log if e.get("kind") == "phase6-9"]
    if prev and key(prev[-1]) == key(entry):
        return
    log.append(entry)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(log[-200:], f, indent=1)
    os.replace(p + ".tmp", p)


def save_result(doc, path=None):
    p = path or RESULT
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(p + ".tmp", p)


def load_result(path=None):
    try:
        with open(path or RESULT, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("hypotheses") else None
    except (OSError, ValueError):
        return None


def load_all(path=None):
    try:
        with open(path or ALL, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def main(argv=None):
    import argparse
    import gold_intraday as gi
    import macro
    import recommendation_backtest as rb
    ap = argparse.ArgumentParser(description="Phases 6 to 9 pre-registered experiments.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rerun", action="store_true", help="recompute with the latest data instead of showing the saved result")
    a = ap.parse_args(argv)
    now0 = time.time()
    if not a.rerun:
        saved = load_result()
        if saved:
            print(json.dumps(saved) if a.json else "saved result from %s" % saved["created"])
            return 0
    reps, allrecs = ex.load_opportunities(), load_all()
    if not reps or not allrecs:
        rb.run()
        reps, allrecs = ex.load_opportunities(), load_all()
    if not reps:
        print(json.dumps(dict(error="no replay opportunities; run Backtest every recommendation first")) if a.json else "no replay opportunities")
        return 1
    events = macro.load_store()
    bars_by, errors = {}, []
    for s in sorted({r["symbol"] for r in reps}):
        try:
            d, w, h1, m15 = (gi.get_bars(s, "1d", "2y", 1800), gi.get_bars(s, "1wk", "5y", 3600), gi.get_bars(s, "60m", "6mo", 300), gi.get_bars(s, "15m", "30d", 60))
            bars_by[s] = {"15m": m15, "1h": h1, "4h": gi.resample(h1, 4), "daily": d, "weekly": w}
        except (Exception, SystemExit) as e:
            errors.append("%s: %s" % (s, str(e)[:160]))
    reps = [r for r in reps if r["symbol"] in bars_by]
    ctxs = la.build_contexts(bars_by, events)
    end = max((bars_by[s]["15m"] or bars_by[s]["1h"])[-1][0] for s in bars_by)
    doc = run(reps, ctxs, bars_by, events, allrecs, end)
    doc["errors"], doc["seconds"] = errors, round(time.time() - now0, 1)
    save_result(doc)
    if a.json:
        print(json.dumps(doc))
    else:
        for h in doc["hypotheses"]:
            print("%-6s %-60s %-20s test n=%s mean=%s" % (h["id"], h["name"][:60], h["decision"], h["test"].get("trades"), h["test"].get("mean_r")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
