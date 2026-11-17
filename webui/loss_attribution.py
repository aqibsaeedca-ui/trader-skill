"""Loss attribution for the recommendation record. Standard library only. Read only: no orders, no accounts.

Takes graded recommendation records (scorecard.py live record, or the historical replay) and answers: what happened to each one, and WHY did the
losers lose, using only evidence that was recorded. A cause is reported only when a predeclared, objective rule finds evidence for it in the
recorded plan, the bars or the economic calendar. If the data to judge a cause does not exist, the cause is reported as UNKNOWN, never guessed.
These are diagnostics, not fitted thresholds: the numbers below were fixed before looking at any result.

What this is NOT: none of these records was ever sent to a broker. A recommendation is not a trade. "Filled" means a conservative simulated fill.

Cause rules (a loss may carry several; they are flags, not an exclusive split)
  countertrend      the plan's own label was counter-trend
  false_breakout    breakout lost and a bar closed back through the breakout level within 3 bars of the fill
  premature_entry   pullback limit stopped within 1 bar of the fill (price never stalled at the level)
  poor_location     the zone behind the plan had fewer than 2 touches (plan marked weak)
  no_confirmation   pullback plans are limit orders placed before any confirming candle (true by design for every pullback)
  tight_stop        price reached target 1 within the holding window AFTER the stop-out
  wide_stop         stop distance above 3 ATR
  high_spread       UNKNOWN always: the data (Yahoo last-trade bars) has no bid or ask
  slippage          exit worse than the stop by more than 15% of the risk (a gap through the stop)
  volatility_spike  the exit bar's range exceeded 2.5 ATR
  news              a high-impact scheduled release fell between 1 hour before the fill and 15 minutes after the exit
  low_liquidity     fill or exit fell between 21:00 and 24:00 UTC (off-peak, near the futures daily break)
  late_entry        breakout filled more than 25% of the planned risk beyond the planned entry (next-open fill after a gap)
  duplicate_idea    the same idea was recommended more than once (same market, side and kind, overlapping zone and time)
  wrong_regime      UNKNOWN: no regime classification was stored with the plan, and judging one needs the validated classifier
  model_error       NOT APPLICABLE: grades come from a hand-weighted score, not from a probability model
  simulation_error  the simulator's own arithmetic failed an independent check (see audit)
"""
import bisect
import calendar
import hashlib
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regimes  # noqa: E402
import scorecard as sc  # noqa: E402

CATEGORIES = (
    ("countertrend", "Countertrend entry"), ("false_breakout", "False breakout"), ("premature_entry", "Premature entry"),
    ("poor_location", "Poor support/resistance location"), ("no_confirmation", "Insufficient confirmation"), ("tight_stop", "Excessively tight stop"),
    ("wide_stop", "Excessively wide stop"), ("high_spread", "High spread"), ("slippage", "Slippage (gap through the stop)"), ("volatility_spike", "Volatility spike"),
    ("news", "News event"), ("low_liquidity", "Low-liquidity session"), ("late_entry", "Late entry"), ("duplicate_idea", "Duplicated trade idea"),
    ("wrong_regime", "Incorrect regime classification"), ("model_error", "Statistical model error"), ("simulation_error", "Backtesting simulation error"))
ALWAYS_UNKNOWN = {"high_spread": "the data has no bid or ask, so spread cannot be measured",
                  "wrong_regime": "no regime was stored with the plan; judging it needs a validated regime classifier",
                  "model_error": "not applicable: grades come from a hand-weighted score, not a probability model"}
FALSE_BREAKOUT_BARS = 3
FINAL_STATES = ("WIN", "LOSS", "TIMED OUT")


def _ts(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) if iso else None


class Context:
    """Bars of one (market, chart) with a causal regime analysis, so every record can be looked up at its own decision time."""

    def __init__(self, bars, step, event_times=()):
        self.bars, self.step = bars, step
        self.times = [b[0] for b in bars]
        self.A = regimes.analyse(bars, event_times=list(event_times), interval_sec=step) if len(bars) >= 60 else None

    def last_closed(self, ts):
        return bisect.bisect_right(self.times, ts - self.step) - 1

    def snap(self, ts):
        i = self.last_closed(ts)
        return self.A.snaps[i] if self.A is not None and 0 <= i < len(self.A.snaps) else None

    def index_at(self, ts):
        i = bisect.bisect_left(self.times, ts)
        return i if i < len(self.times) and self.times[i] == ts else None


def build_contexts(bars_by_symbol, events):
    high = [e["ts"] for e in events if e.get("importance") == "high"]
    return {(s, tf): Context(b, sc.STEP[tf], high) for s, per in bars_by_symbol.items() for tf, b in per.items() if b}


def audit(rec, ctx):
    """Independent checks of the simulator's arithmetic and fill plausibility. Returns a list of problems (empty when the record is sound)."""
    g = rec.get("result") or {}
    bad = []
    if g.get("fill_price") is None:
        return bad
    sgn = 1 if rec["side"] == "long" else -1
    legs, risk = g.get("legs") or [], g.get("risk")
    if not legs or not risk:
        return ["no exit legs were recorded"]
    if abs(sum(l["frac"] for l in legs) - 1.0) > 1e-6:
        bad.append("exit fractions do not add up to 1")
    again = sum(l["frac"] * (l["price"] - g["fill_price"]) * sgn / risk for l in legs)
    if abs(again - g["r_gross"]) > 1e-6:
        bad.append("R recomputed from the exit legs (%.4f) differs from the reported %.4f" % (again, g["r_gross"]))
    if abs(risk - abs(rec["entry"] - rec["stop"])) > 1e-6:
        bad.append("the R unit is not the planned entry-to-stop distance")
    if (rec["entry"] - rec["stop"]) * sgn <= 0 or any((t - rec["entry"]) * sgn <= 0 for t in rec["targets"]):
        bad.append("stop or target on the wrong side of the entry")
    if ctx is not None:
        i = ctx.index_at(_ts(g["fill_time"]))
        if i is None:
            bad.append("the fill bar is not in the data")
        else:
            _, hi, lo, cl, op = ctx.bars[i]
            if rec["kind"] == "pullback" and not (lo - 1e-9 <= g["fill_price"] <= hi + 1e-9):
                bad.append("limit fill price lies outside the fill bar's range")
            if rec["kind"] == "breakout" and abs(g["fill_price"] - op) > 1e-9:
                bad.append("breakout fill is not the bar's open")
    return bad


def clusters(recs, ctxs):
    """Group recommendations that are the same idea: same market, side and kind, zones within half an hourly ATR, and overlapping validity windows."""
    out, active = {}, {}
    for r in sorted(recs, key=lambda x: x["created_ts"]):
        grp = (r["symbol"], r["side"], r["kind"])
        c1 = ctxs.get((r["symbol"], "1h"))
        sn = c1.snap(r["created_ts"]) if c1 else None
        tol = 0.5 * sn["atr"] if sn else 0.002 * r["entry"]
        window = sc.STEP[r["tf"]] * r["expiry_bars"]
        live = [c for c in active.setdefault(grp, []) if c["until"] >= r["created_ts"]]
        active[grp] = live
        hit = next((c for c in live if abs(c["entry"] - r["entry"]) <= tol), None)
        if hit is None:
            hit = dict(id="op-" + hashlib.sha1(("%s|%s|%s|%d|%.2f" % (grp + (int(r["created_ts"]), r["entry"]))).encode()).hexdigest()[:8], entry=r["entry"], until=0, n=0)
            live.append(hit)
        hit["until"] = max(hit["until"], r["created_ts"] + window)
        hit["n"] += 1
        out[r["id"]] = hit["id"]
    size = {}
    for cid in out.values():
        size[cid] = size.get(cid, 0) + 1
    return out, size


def overlap_counts(recs, ctxs):
    """For each record: how many OTHER recommendations on the same market and side were valid at the same time with an entry within 1 hourly ATR."""
    by = {}
    for r in recs:
        by.setdefault((r["symbol"], r["side"]), []).append(r)
    res = {}
    for key, rows in by.items():
        rows.sort(key=lambda x: x["created_ts"])
        times = [x["created_ts"] for x in rows]
        c1 = ctxs.get((key[0], "1h"))
        for r in rows:
            win = sc.STEP[r["tf"]] * r["expiry_bars"]
            lo = bisect.bisect_left(times, r["created_ts"] - 21 * 86400)
            hi = bisect.bisect_right(times, r["created_ts"] + win)
            sn = c1.snap(r["created_ts"]) if c1 else None
            tol = sn["atr"] if sn else 0.004 * r["entry"]
            n = 0
            for o in rows[lo:hi]:
                if o is r:
                    continue
                if o["created_ts"] <= r["created_ts"] + win and r["created_ts"] <= o["created_ts"] + sc.STEP[o["tf"]] * o["expiry_bars"] and abs(o["entry"] - r["entry"]) <= tol:
                    n += 1
            res[r["id"]] = n
    return res


def flags_for(rec, ctx, events, cluster_size):
    """(supported {cause: evidence}, unknown {cause: why}) for one LOSING record."""
    g = rec["result"]
    sgn = 1 if rec["side"] == "long" else -1
    step = sc.STEP[rec["tf"]]
    risk = g["risk"]
    ok, unk = {}, dict(ALWAYS_UNKNOWN)
    fill_ts, exit_ts = _ts(g["fill_time"]), _ts(g["exit_time"])
    fi = ctx.index_at(fill_ts) if ctx else None
    xi = ctx.index_at(exit_ts - step) if (ctx and exit_ts) else None
    sn = ctx.snap(rec["created_ts"]) if ctx else None
    atr = sn["atr"] if sn else None
    # trend label stored with the plan
    if rec.get("trend") is None:
        unk["countertrend"] = "the plan's trend label was not stored with this older record"
    elif rec["trend"] == "counter":
        ok["countertrend"] = "the plan itself was labelled counter-trend (against the bias read from the higher timeframes)"
    if rec["kind"] == "breakout" and fi is not None:
        level = rec["entry"]
        for j in range(fi, min(len(ctx.bars), fi + FALSE_BREAKOUT_BARS + 1)):
            cl = ctx.bars[j][3]
            if (cl < level) if sgn == 1 else (cl > level):
                ok["false_breakout"] = "a bar closed back %s the breakout level %.2f within %d bars of the fill" % ("below" if sgn == 1 else "above", level, j - fi + 1)
                break
    if rec["kind"] == "pullback" and fi is not None and xi is not None and xi - fi <= 1:
        ok["premature_entry"] = "the stop was hit %d bar(s) after the limit fill, so price never stalled at the level" % (xi - fi + 1) if xi > fi else "the stop was hit on the fill bar itself, so price never stalled at the level"
    if rec.get("weak") is None:
        unk["poor_location"] = "the zone strength was not stored with this older record"
    elif rec["weak"]:
        ok["poor_location"] = "the zone behind the plan had fewer than 2 touches (plan marked weak)"
    if rec["kind"] == "pullback":
        ok["no_confirmation"] = "pullback plans are limit orders placed before any confirming candle"
    if xi is not None and ctx:
        after = ctx.bars[xi + 1: xi + 1 + rec["hold_bars"]]
        t1 = rec["targets"][0]
        for n, b in enumerate(after, 1):
            if (b[1] >= t1) if sgn == 1 else (b[2] <= t1):
                ok["tight_stop"] = "price reached target 1 (%.2f) %d bar(s) after the stop-out%s" % (t1, n, ("; stop was %.1f ATR" % (risk / atr)) if atr else "")
                break
    else:
        unk["tight_stop"] = "the bars after the exit are not available"
    if atr:
        if risk / atr > 3.0:
            ok["wide_stop"] = "the stop was %.1f ATR away" % (risk / atr)
        if xi is not None:
            xb = ctx.bars[xi]
            if xb[1] - xb[2] > 2.5 * atr:
                ok["volatility_spike"] = "the exit bar's range was %.1f ATR" % ((xb[1] - xb[2]) / atr)
    else:
        unk["wide_stop"] = unk["volatility_spike"] = "no ATR available at the recommendation time"
    if g["r_gross"] < -1.15:
        ok["slippage"] = "the exit was %.2fR, worse than the planned -1R, because price gapped through the stop" % g["r_gross"]
    if events is None:
        unk["news"] = "no economic calendar available"
    else:
        hit = next((e for e in events if e.get("importance") == "high" and fill_ts - 3600 <= e["ts"] <= (exit_ts or fill_ts) + 900), None)
        if hit:
            ok["news"] = "%s at %s fell during or just around the trade" % (hit["name"], time.strftime("%H:%M UTC", time.gmtime(hit["ts"])))
    for label, ts in (("fill", fill_ts), ("exit", (exit_ts - step) if exit_ts else None)):
        if ts is not None and 21 <= time.gmtime(ts).tm_hour <= 23:
            ok["low_liquidity"] = "the %s fell at %s UTC, off-peak and near the futures daily break" % (label, time.strftime("%H:%M", time.gmtime(ts)))
            break
    if rec["kind"] == "breakout":
        planned = abs(rec["entry"] - rec["stop"])
        gap = (g["fill_price"] - rec["entry"]) * sgn
        if gap > 0.25 * planned:
            ok["late_entry"] = "filled %.2f beyond the planned entry (%.0f%% of the planned risk): the next bar opened past the trigger" % (gap, 100 * gap / planned)
    if cluster_size > 1:
        ok["duplicate_idea"] = "the same idea was recommended %d times (counted as one opportunity in the de-duplicated results)" % cluster_size
    return ok, unk


def _stats(rows):
    done = [r for r in rows if r["state"] in FINAL_STATES]
    w = sum(1 for r in done if r["state"] == "WIN")
    l = sum(1 for r in done if r["state"] == "LOSS")
    rs = [r["r_net"] for r in done if r["r_net"] is not None]
    n = w + l
    pos, neg = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(done, key=lambda x: x["exit_time"] or ""):
        eq += r["r_net"] or 0.0
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dict(completed=len(done), wins=w, losses=l, win_rate=(w / n if n else None), win_ci=sc.wilson(w, n), mean_r=(sum(rs) / len(rs) if rs else None), total_r=sum(rs),
                profit_factor=(pos / neg if neg > 0 else None), max_drawdown_r=dd, avg_win=(pos / w if w else None), avg_loss=(-neg / l if l else None))


def build(recs, bars_by_symbol, events, label, max_rows=40):
    """The full report for one population of graded records."""
    ctxs = build_contexts(bars_by_symbol, events or [])
    cl, size = clusters(recs, ctxs)
    ov = overlap_counts([r for r in recs if (r.get("result") or {}).get("fill_price") is not None], ctxs)
    rows, audits, cause_ok, cause_unk = [], {}, {k: 0 for k, _ in CATEGORIES}, {k: 0 for k, _ in CATEGORIES}
    losses = 0
    for r in sorted(recs, key=lambda x: x["created_ts"]):
        g = r.get("result") or {}
        st = g.get("state", "WAITING")
        ctx = ctxs.get((r["symbol"], r["tf"]))
        row = dict(id=r["id"], opportunity=cl.get(r["id"]), cluster_size=size.get(cl.get(r["id"]), 1), created=r["created"], symbol=r["symbol"], tf=r["tf"], strategy=r["name"],
                   kind=r["kind"], grade=r.get("grade"), entry=r["entry"], stop=r["stop"], targets=r["targets"], state=st, final=bool(g.get("final")),
                   fill_price=g.get("fill_price"), fill_time=g.get("fill_time"), exit_time=g.get("exit_time"), r_gross=g.get("r_gross"), r_net=g.get("r_net"),
                   cost_per_unit=r["cost"], cost_r=(r["cost"] / g["risk"] if g.get("risk") else None), entry_reason=("; ".join(r.get("why") or []) or r["kind"]),
                   exit_reason=g.get("note", ""), trend_label=r.get("trend"), overlapping_active=ov.get(r["id"], 0))
        if g.get("fill_price") is not None:
            sn = ctx.snap(r["created_ts"]) if ctx else None
            row.update(regime=sn["label"] if sn else None, volatility=sn["vol"] if sn else None, session=regimes.session_of(_ts(g["fill_time"])),
                       fill_gap=(g["fill_price"] - r["entry"]) * (1 if r["side"] == "long" else -1),
                       exit_price=(sum(l["price"] * l["frac"] for l in g["legs"] if not l["why"].startswith("mark")) / max(1e-9, sum(l["frac"] for l in g["legs"] if not l["why"].startswith("mark"))) if any(not l["why"].startswith("mark") for l in g["legs"]) else None))
            bad = audit(r, ctx)
            audits[r["id"]] = bad
            row["audit"] = bad
        if st == "LOSS" and g.get("fill_price") is not None:
            losses += 1
            ok, unk = flags_for(r, ctx, events, size.get(cl.get(r["id"]), 1))
            if audits.get(r["id"]):
                ok["simulation_error"] = "; ".join(audits[r["id"]])
            else:
                pass
            for k, _ in CATEGORIES:
                if k in ok:
                    cause_ok[k] += 1
                elif k in unk:
                    cause_unk[k] += 1
            row.update(causes=ok, unknown_causes=sorted(unk), no_supported_cause=not [k for k in ok if k not in ("no_confirmation",)])
        rows.append(row)
    filled = [x for x in rows if x["fill_price"] is not None]
    first_per_cluster = {}
    for x in sorted(filled, key=lambda x: x["fill_time"]):
        first_per_cluster.setdefault(x["opportunity"], x)
    states = {}
    for x in rows:
        states[x["state"]] = states.get(x["state"], 0) + 1
    gaps = [x["fill_gap"] for x in filled if x["kind"] == "breakout"]
    bad_audit = [k for k, v in audits.items() if v]
    by = {}
    for dim in ("regime", "session", "tf", "strategy", "grade", "volatility", "trend_label", "kind"):
        groups = {}
        for x in filled:
            groups.setdefault(str(x.get(dim)), []).append(x)
        by[dim] = {k: _stats(v) for k, v in sorted(groups.items())}
    return dict(
        label=label,
        counts=dict(recommendations=len(rows), unique_opportunities=len(size), triggered=len(filled), completed=sum(1 for x in rows if x["state"] in FINAL_STATES), open=states.get("OPEN", 0),
                    waiting=states.get("WAITING", 0), invalidated=states.get("INVALIDATED", 0), not_triggered=states.get("NOT TRIGGERED", 0), executed_on_a_broker=0),
        stats_all=_stats(filled), stats_deduplicated=_stats(list(first_per_cluster.values())),
        losses=dict(count=losses, causes=[dict(key=k, label=n, supported=cause_ok[k], unknown=cause_unk[k]) for k, n in CATEGORIES],
                    none_supported=sum(1 for x in rows if x.get("no_supported_cause"))),
        fill_gap=dict(breakout_fills=len(gaps), mean=(sum(gaps) / len(gaps) if gaps else None), worst=(max(gaps) if gaps else None), best=(min(gaps) if gaps else None),
                      note=("A breakout plan is graded as: wait for a bar to CLOSE beyond the entry, then fill at the NEXT bar's open. That open can be above (long) or below "
                            "(short) the planned entry, so a breakout can fill a few dollars away from the plan. This is a modelling rule, not a defect, but the fill is "
                            "'next open after the confirming close', not 'the planned entry price'. Pullback (limit) plans fill exactly at the entry price.")),
        audit=dict(checked=len(audits), failed=len(bad_audit), failed_ids=bad_audit[:10],
                   checks=["exit legs sum to 100% of the position", "R recomputed from the exit legs equals the reported R", "the R unit equals the planned entry-to-stop distance",
                           "stop and targets are on the correct sides of the entry", "a limit fill price lies inside its bar's range", "a breakout fill equals its bar's open"]),
        by=by, rows=[x for x in rows if x["state"] == "LOSS"][-max_rows:][::-1] + [x for x in rows if x["state"] != "LOSS"][-15:][::-1])


def live_population(symbols=("GC=F", "BTC-USD"), now=None, path=None, analyse=None):
    """Grade the live scorecard record with fresh bars and attribute its losses. Returns the population report."""
    import gold_intraday as gi
    import macro
    now = time.time() if now is None else now
    analyse = analyse or gi.analyse
    recs = sc.load(path)
    bars, errors = {}, []
    for s in symbols:
        try:
            res = analyse(s)
            bars[s] = sc.bars_from_res(res)
            sc.record_from_res_into(recs, res, s, now)
        except (Exception, SystemExit) as e:
            errors.append("%s: %s" % (s, str(e)[:160]))
    sc.grade_all(recs, bars, now)
    sc.save(recs, path)
    try:
        events = macro.load_store()
    except Exception:
        events = None
    pop = build([r for r in recs if r["symbol"] in bars], bars, events, "live record (forward, hypothetical)", max_rows=60)
    pop["errors"] = errors
    return pop


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Loss attribution for the recommendation record.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = time.time()
    live = live_population(now=now)
    try:
        import recommendation_backtest as rb
        saved = rb.load_doc()
        replay = (saved or {}).get("attribution")
    except Exception:
        replay = None
    doc = dict(checked=sc._iso(now), live=live, replay=replay,
               notes=["Recommendations are not trades: none of these was sent to a broker. A recommendation counts as filled only through a conservative simulated fill.",
                      "Recommendations that overlap in time and price are the same idea repeated; the de-duplicated statistics count each opportunity once.",
                      "A cause is listed only when recorded evidence supports it. Causes that cannot be judged from the data are reported as unknown.",
                      "An unfinished position is never counted as a win or a loss.", "Not financial advice."])
    if a.json:
        print(json.dumps(doc))
    else:
        c = live["counts"]
        print("LOSS ATTRIBUTION %s: %d recommendations, %d opportunities, %d completed, %d losses" % (doc["checked"], c["recommendations"], c["unique_opportunities"], c["completed"], live["losses"]["count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())