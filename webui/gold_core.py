#!/usr/bin/env python3
"""Gold core: the two strategy families kept in the simplified Gold engine, plus their pre-registered backtest. Standard library only.
Read only: nothing here places or changes an order.

  A  H4/H1 trend continuation   A1 Donchian 20 breakout, 2R exit;  A2 same entry, 10-bar channel exit;  A3 structural pullback to the H1 EMA20
  B  Trend-aligned structural rejection   B1 2R exit;  B2 exit at the nearest opposing confirmed structure
  C  VWAP/EMA continuation: research only (no volume in this data), never generates a signal

Every rule, window and decision criterion is fixed in webui/docs/GOLD_REBUILD_PREREGISTRATION.md, committed before this was run. Only closed bars are
used; a signal decided on the close of H1 bar i fills at the open of bar i+1. The old engine is the benchmark, compared on identical dates and costs.
"""
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402
import experiments as ex  # noqa: E402
import gold_sessions as gs  # noqa: E402
import scorecard as sc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT = os.path.join(ROOT, "trader", "gold_rebuild_result.json")
LOG = os.path.join(ROOT, "trader", "experiment_log.json")
N_TOTAL = 29 + 5
ALPHA = 0.05 / N_TOTAL
H4 = 14400
DONCHIAN, EXIT_CHANNEL, HOLD, COOLDOWN, ATR_N, EMA_N, SMA4, SLOPE4, PIV_K, PIV_CONFIRM_GAP = 20, 10, 48, 6, 14, 20, 50, 6, 3, 6
DEV_FRAC, FOLDS, MIN_FILLS, MIN_HOLDOUT = 0.8, 3, 100, 20
STRATEGIES = (
    ("A1", "A", "Trend continuation: Donchian 20 breakout, 2R exit", "With an H4 uptrend, an H1 close above the previous 20-bar high; stop 2 ATR below the close; target 2R (mirror for shorts)."),
    ("A2", "A", "Trend continuation: Donchian 20 breakout, channel exit", "Same entry and stop; exit on an H1 close beyond the previous 10-bar channel, no fixed target."),
    ("A3", "A", "Trend continuation: structural pullback to the H1 EMA20", "With an H4 uptrend, price touched the H1 EMA20 within 3 bars and the signal bar closes back above it bullish; stop below the pullback low; target 2R."),
    ("B1", "B", "Structural rejection at an H4 pivot zone, 2R exit", "H4 pivot zone confirmed 6+ bars earlier, H4 trend aligned, completed H1 rejection candle, opposing structure at least 1.5R away; target 2R."),
    ("B2", "B", "Structural rejection at an H4 pivot zone, structural exit", "Same setup; target at the nearest opposing confirmed pivot zone edge, taken only between 1.5R and 4R."),
)


# ------------------------------------------------------------------ indicators and structure (all causal) ------------------------------------------

def wilder_atr(h, l, c, n=ATR_N):
    out = [None] * len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    if len(c) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    e = sum(vals[:n]) / n
    out[n - 1] = e
    k = 2.0 / (n + 1)
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def h4_blocks(h1):
    """H4 bars in fixed UTC blocks, only complete ones (four H1 bars). Returns tuples (t, high, low, close, open)."""
    out, cur = [], None
    for t, hi, lo, cl, op in h1:
        key = t // H4
        if cur is None or cur[0] != key:
            if cur and cur[2] == 4:
                out.append(tuple(cur[1]))
            cur = [key, [key * H4, hi, lo, cl, op], 1]
        else:
            b = cur[1]
            b[1], b[2], b[3] = max(b[1], hi), min(b[2], lo), cl
            cur[2] += 1
    if cur and cur[2] == 4:
        out.append(tuple(cur[1]))
    return out


class Context:
    """Everything the signals need, precomputed once per series, indexed so nothing later than the decision time can be read."""

    def __init__(self, h1):
        self.h1 = h1
        self.t = [b[0] for b in h1]
        self.h, self.l, self.c, self.o = [b[1] for b in h1], [b[2] for b in h1], [b[3] for b in h1], [b[4] for b in h1]
        self.atr = wilder_atr(self.h, self.l, self.c)
        self.ema = ema(self.c, EMA_N)
        self.b4 = h4_blocks(h1)
        self.t4 = [b[0] for b in self.b4]
        self.end4 = [t + H4 for t in self.t4]
        c4 = [b[3] for b in self.b4]
        self.sma4 = [None] * len(c4)
        for j in range(SMA4 - 1, len(c4)):
            self.sma4[j] = sum(c4[j - SMA4 + 1:j + 1]) / SMA4
        self.trend4 = [0] * len(c4)
        for j in range(SMA4 - 1 + SLOPE4, len(c4)):
            if c4[j] > self.sma4[j] and self.sma4[j] > self.sma4[j - SLOPE4]:
                self.trend4[j] = 1
            elif c4[j] < self.sma4[j] and self.sma4[j] < self.sma4[j - SLOPE4]:
                self.trend4[j] = -1
        self.pivot_hi, self.pivot_lo = {}, {}                      # H4 index -> price, for confirmed pivots (confirmed once bar j+K has closed)
        for j in range(PIV_K, len(self.b4) - PIV_K):
            hi, lo = self.b4[j][1], self.b4[j][2]
            if all(hi > self.b4[j - m][1] and hi > self.b4[j + m][1] for m in range(1, PIV_K + 1)):
                self.pivot_hi[j] = hi
            if all(lo < self.b4[j - m][2] and lo < self.b4[j + m][2] for m in range(1, PIV_K + 1)):
                self.pivot_lo[j] = lo

    def last_closed_h4(self, i):
        """Index of the latest H4 bar that is closed when H1 bar i has closed."""
        return bisect.bisect_right(self.end4, self.t[i] + 3600) - 1


def _trend(ctx, i):
    j = ctx.last_closed_h4(i)
    return (ctx.trend4[j] if j >= 0 else 0), j


# ------------------------------------------------------------------ signals ----------------------------------------------------------------------

def signal_at(ctx, i, sid):
    """The signal of strategy `sid` decided on the close of H1 bar i, or None. Pure function of closed data up to bar i."""
    atr = ctx.atr[i]
    if atr is None or i < DONCHIAN + 1:
        return None
    tr, J = _trend(ctx, i)
    if tr == 0 or J < 0:
        return None
    side = "long" if tr == 1 else "short"
    sg = tr
    c, o, h, l = ctx.c, ctx.o, ctx.h, ctx.l
    if sid in ("A1", "A2"):
        if sg == 1 and c[i] > max(h[i - DONCHIAN:i]):
            return dict(i=i, side=side, stop=c[i] - 2.0 * atr, exit="2R" if sid == "A1" else "channel", atr=atr, strategy=sid)
        if sg == -1 and c[i] < min(l[i - DONCHIAN:i]):
            return dict(i=i, side=side, stop=c[i] + 2.0 * atr, exit="2R" if sid == "A1" else "channel", atr=atr, strategy=sid)
        return None
    if sid == "A3":
        e = ctx.ema
        if e[i] is None or i < 3:
            return None
        if sg == 1:
            if any(l[k] <= e[k] for k in range(i - 2, i + 1)) and c[i] > e[i] and c[i] > o[i]:
                return dict(i=i, side=side, stop=min(l[i - 2:i + 1]) - 0.25 * atr, exit="2R", atr=atr, strategy=sid)
        else:
            if any(h[k] >= e[k] for k in range(i - 2, i + 1)) and c[i] < e[i] and c[i] < o[i]:
                return dict(i=i, side=side, stop=max(h[i - 2:i + 1]) + 0.25 * atr, exit="2R", atr=atr, strategy=sid)
        return None
    if sid in ("B1", "B2"):
        rng = h[i] - l[i]
        if rng <= 0:
            return None
        pad = 0.25 * atr
        # zones confirmed at least PIV_CONFIRM_GAP H4 bars before the latest closed H4 bar: pivot index j, confirmed at j + PIV_K
        if sg == 1:
            cands = [j for j in ctx.pivot_lo if j + PIV_K <= J - PIV_CONFIRM_GAP and ctx.pivot_lo[j] - pad <= l[i] <= ctx.pivot_lo[j] + pad]
            if not cands:
                return None
            j = max(cands)
            top = ctx.pivot_lo[j] + pad
            wick = (min(o[i], c[i]) - l[i]) / rng
            if not (c[i] > top and wick >= 0.5 and (c[i] - l[i]) / rng >= 0.6):
                return None
            stop = min(l[i], ctx.pivot_lo[j] - pad) - 0.5 * atr
            opp = [ctx.pivot_hi[m] - pad for m in ctx.pivot_hi if m + PIV_K <= J - PIV_CONFIRM_GAP and ctx.pivot_hi[m] - pad > c[i]]
            edge = min(opp) if opp else None
        else:
            cands = [j for j in ctx.pivot_hi if j + PIV_K <= J - PIV_CONFIRM_GAP and ctx.pivot_hi[j] - pad <= h[i] <= ctx.pivot_hi[j] + pad]
            if not cands:
                return None
            j = max(cands)
            bot = ctx.pivot_hi[j] - pad
            wick = (h[i] - max(o[i], c[i])) / rng
            if not (c[i] < bot and wick >= 0.5 and (h[i] - c[i]) / rng >= 0.6):
                return None
            stop = max(h[i], ctx.pivot_hi[j] + pad) + 0.5 * atr
            opp = [ctx.pivot_lo[m] + pad for m in ctx.pivot_lo if m + PIV_K <= J - PIV_CONFIRM_GAP and ctx.pivot_lo[m] + pad < c[i]]
            edge = max(opp) if opp else None
        return dict(i=i, side=side, stop=stop, exit="2R" if sid == "B1" else "structure", opp_edge=edge, atr=atr, strategy=sid)
    return None


# ------------------------------------------------------------------ simulation ------------------------------------------------------------------

def simulate(ctx, sig, cost, hold=HOLD):
    """Fill at the next H1 open, then manage bar by bar. Returns None when the setup is skipped at the fill (and why in .skip), a dict with final=False when the trade is
    still open at the end of the data, otherwise the completed trade. Same-bar stop and target is resolved as stop first; a gap through the stop exits at the open."""
    i = sig["i"]
    k0 = i + 1
    n = len(ctx.t)
    if k0 >= n:
        return dict(final=False, skip="pending: the next bar has not opened yet")
    long = sig["side"] == "long"
    sg = 1 if long else -1
    fill, stop, atr = ctx.o[k0], sig["stop"], sig["atr"]
    risk = (fill - stop) * sg
    if risk <= 0:
        return dict(skip="the next bar opened beyond the stop")
    kind = sig["exit"]
    if sig["strategy"] in ("B1", "B2"):
        if risk < 1.0 * atr or risk > 4.0 * atr:
            return dict(skip="planned risk outside 1 to 4 ATR")
        edge = sig.get("opp_edge")
        if edge is not None and (edge - fill) * sg < 1.5 * risk:
            return dict(skip="opposing structure within 1.5R")
    target = None
    if kind == "2R":
        target = fill + 2.0 * risk * sg
    elif kind == "structure":
        edge = sig.get("opp_edge")
        if edge is None or not (1.5 * risk <= (edge - fill) * sg <= 4.0 * risk):
            return dict(skip="no structural target between 1.5R and 4R")
        target = edge
    t, h, l, c, o = ctx.t, ctx.h, ctx.l, ctx.c, ctx.o
    exit_px = exit_k = why = None
    last = min(n, k0 + hold)
    for k in range(k0, last):
        if (l[k] <= stop) if long else (h[k] >= stop):
            exit_px, exit_k, why = (min(stop, o[k]) if long else max(stop, o[k])), k, "stop"
            break
        if target is not None and ((h[k] >= target) if long else (l[k] <= target)):
            exit_px, exit_k, why = target, k, "target"
            break
        if kind == "channel" and k - EXIT_CHANNEL >= 0:
            lo10, hi10 = min(l[k - EXIT_CHANNEL:k]), max(h[k - EXIT_CHANNEL:k])
            if (c[k] < lo10) if long else (c[k] > hi10):
                if k + 1 < n:
                    exit_px, exit_k, why = o[k + 1], k + 1, "channel"
                    break
                return dict(final=False, skip="channel exit pending")
    if exit_px is None:
        if last - k0 >= hold:
            exit_px, exit_k, why = c[last - 1], last - 1, "time"
        else:
            return dict(final=False, skip="still open at the end of the data")
    gross = (exit_px - fill) * sg / risk
    return dict(final=True, sig_i=i, side=sig["side"], strategy=sig["strategy"], entry_ts=t[k0], exit_ts=t[exit_k] + 3600, exit_k=exit_k, fill=fill, stop=stop, risk=risk, gross_r=gross,
                cost_r=cost / risk, net_r=gross - cost / risk, why=why, session=gs.session_of(t[k0]))


def trades_for(ctx, sid, cost, i_lo=0, i_hi=None):
    """All completed trades of one strategy with signal bars in [i_lo, i_hi), one at a time with a cooldown after a stop-out. Also counts skips and open trades."""
    i_hi = len(ctx.t) if i_hi is None else i_hi
    out, skipped, open_, busy, signals = [], {}, 0, 0, 0
    for i in range(max(i_lo, DONCHIAN + 2), i_hi):
        if i < busy:
            continue
        sig = signal_at(ctx, i, sid)
        if sig is None:
            continue
        signals += 1
        r = simulate(ctx, sig, cost)
        if r.get("final") is False:
            open_ += 1
            continue
        if "skip" in r and "net_r" not in r:
            skipped[r["skip"]] = skipped.get(r["skip"], 0) + 1
            continue
        out.append(r)
        busy = r["exit_k"] + (COOLDOWN if r["why"] == "stop" else 0)
    return out, dict(signals=signals, skipped=skipped, open=open_)


# ------------------------------------------------------------------ evaluation --------------------------------------------------------------------

def _metrics(trades, sens=1.0):
    rows = [(t["entry_ts"], t["gross_r"] - sens * t["cost_r"]) for t in trades]
    return ex.metrics(rows, len(trades), alpha=ALPHA)


def _by_session(trades):
    by = {}
    for t in trades:
        by.setdefault(t["session"], []).append(t["net_r"])
    return {k: dict(trades=len(v), mean_r=sum(v) / len(v), wins=sum(1 for x in v if x > 0)) for k, v in sorted(by.items())}


def _stats(trades):
    n = len(trades)
    if not n:
        return dict(trades=0)
    w = [t["net_r"] for t in trades if t["net_r"] > 0]
    ls = [t["net_r"] for t in trades if t["net_r"] <= 0]
    m = _metrics(trades)
    m.update(win_rate=len(w) / n, avg_win_r=(sum(w) / len(w) if w else None), avg_loss_r=(sum(ls) / len(ls) if ls else None),
             cost_sensitivity={"0.5x": _metrics(trades, 0.5)["mean_r"], "1x": m["mean_r"], "2x": _metrics(trades, 2.0)["mean_r"]}, by_session=_by_session(trades),
             exits={k: sum(1 for t in trades if t["why"] == k) for k in sorted({t["why"] for t in trades})})
    return m


def decide(dev, folds, old_dev, holdout=None):
    why = []
    n = dev["trades"]
    if n < MIN_FILLS:
        return "INSUFFICIENT SAMPLE", ["only %d completed trades in the development window; at least %d are needed" % (n, MIN_FILLS)]
    if not (dev["mean_r"] is not None and dev["mean_r"] > 0 and dev["band_corrected"] and dev["band_corrected"][0] > 0):
        why.append("development mean net R %+.3f is not above zero with the multiple-testing corrected band" % dev["mean_r"])
    pos = sum(1 for f in folds if f.get("trades") and f["mean_r"] is not None and f["mean_r"] > 0)
    if pos < 2:
        why.append("positive in only %d of %d chronological folds" % (pos, len(folds)))
    if old_dev.get("mean_r") is not None and dev["mean_r"] is not None and not dev["mean_r"] > old_dev["mean_r"]:
        why.append("does not beat the old engine on the same window (%+.3f vs %+.3f)" % (dev["mean_r"], old_dev["mean_r"]))
    return ("ADOPTED", ["passed every pre-registered development criterion"]) if not why else ("REJECTED", why)


def old_engine(reps, ts_dev_end, ts_start):
    """The archived engine's unique GOLD opportunities (grading engine 2, same cost per unit) on the same windows."""
    rows = []
    for r in reps:
        if r.get("symbol") != "GC=F":
            continue
        res = r.get("result") or {}
        if res.get("final") and res.get("state") in ex.FINAL_FILLED and res.get("r_net") is not None and r["created_ts"] >= ts_start:
            rows.append((r["created_ts"], res["r_net"]))
    dev = [(t, v) for t, v in rows if t < ts_dev_end]
    hold = [(t, v) for t, v in rows if t >= ts_dev_end]
    n_all = len([r for r in reps if r.get("symbol") == "GC=F" and r["created_ts"] >= ts_start])
    m = lambda x: ex.metrics(x, len(x), alpha=ALPHA)   # noqa: E731
    def extra(x):
        v = [a for _, a in x]
        d = m(x)
        d.update(win_rate=(sum(1 for a in v if a > 0) / len(v) if v else None))
        return d
    return dict(development=extra(dev), holdout=extra(hold), opportunities=n_all)


def run(h1, reps, now, cost, log_path=None, path_result=None):
    ctx = Context(h1)
    n = len(ctx.t)
    dev_end = int(DEV_FRAC * n)
    ts_dev_end, ts_start = ctx.t[dev_end], ctx.t[0]
    bounds = [DONCHIAN + 2 + (dev_end - DONCHIAN - 2) * f // FOLDS for f in range(FOLDS + 1)]
    old = old_engine(reps, ts_dev_end, ts_start)
    hyps = []
    for sid, fam, name, definition in STRATEGIES:
        dev_tr, info = trades_for(ctx, sid, cost, 0, dev_end)
        dev = _stats(dev_tr)
        folds = []
        for f in range(FOLDS):
            ft = [t for t in dev_tr if bounds[f] <= t["sig_i"] < bounds[f + 1]]
            folds.append(dict(fold=f + 1, trades=len(ft), mean_r=(sum(t["net_r"] for t in ft) / len(ft) if ft else None)))
        decision, why = decide(dev, folds, old["development"])
        hold = None
        elig = False
        if decision == "ADOPTED":                                          # the holdout is opened only here, once
            ho_tr, hinfo = trades_for(ctx, sid, cost, dev_end, n)
            hold = _stats(ho_tr)
            hold["opened"] = True
            elig = hold["trades"] >= MIN_HOLDOUT and (hold.get("mean_r") or 0) > 0
            if not elig:
                why = why + ["holdout: %d trades, mean %s, so it is not eligible" % (hold["trades"], "n/a" if hold.get("mean_r") is None else "%+.3f" % hold["mean_r"])]
        hyps.append(dict(id=sid, family=fam, name=name, definition=definition, development=dev, folds=folds, signals=info, decision=decision, reasons=why, holdout=hold or dict(opened=False, note="not opened: no development adoption"),
                         eligible=elig))
    doc = dict(created=sc._iso(now), data=dict(bars_h1=n, first=sc._iso(ctx.t[0]), last=sc._iso(ctx.t[-1] + 3600), development_end=sc._iso(ts_dev_end), h4_bars=len(ctx.b4), cost_per_unit=cost),
               hypotheses_total=len(STRATEGIES), hypotheses_cumulative=N_TOTAL, bonferroni_alpha=ALPHA, hypotheses=hyps, old_engine=old, strategy_c=dict(status="research only", reason="no volume in this data, so VWAP cannot be built; it cannot generate a signal"),
               eligible=[h["id"] for h in hyps if h["eligible"]],
               not_tested=["M5 and M15 entry triggers (only 30 days of history)", "spread and broker slippage (no bid or ask; cost sensitivity at 0.5x, 1x and 2x of a flat cost instead)", "pending-order fills (these entries are market orders at the next open)",
                           "a per-session edge (sessions are descriptive; enabling one needs its own 100 fills)"],
               caution="Six months of delayed GC=F bars. Opportunities overlap, so bands are optimistic. Nothing is tuned on the holdout. A variant that is not adopted cannot generate a Gold plan. Not financial advice.")
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
    entry = dict(created=doc["created"], kind="gold-rebuild", split=dict(development_end=doc["data"]["development_end"]),
                 hypotheses=[dict(phase=15, id=h["id"], name=h["name"], definition=h["definition"], decision=h["decision"], reasons=h["reasons"], train_mean_r=None,
                                  test_mean_r=h["development"].get("mean_r"), test_trades=h["development"].get("trades")) for h in doc["hypotheses"]])
    key = lambda e: [(h["id"], h["decision"], h["test_mean_r"], h["test_trades"]) for h in e["hypotheses"]]   # noqa: E731
    prev = [e for e in log if e.get("kind") == "gold-rebuild"]
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


# ------------------------------------------------------------------ live: at most one independent opportunity ------------------------------------------

def live_candidate(h1, now=None, result=None):
    """Signals on the latest CLOSED H1 bar, merged into at most ONE opportunity. A variant that is not eligible is shown as research only. Returns (candidate or None, info)."""
    now = time.time() if now is None else now
    closed = [b for b in h1 if b[0] + 3600 <= now]
    if len(closed) < 400:
        return None, dict(note="not enough closed H1 bars", research=[])
    ctx = Context(closed)
    i = len(closed) - 1
    result = load_result() if result is None else result
    elig = set((result or {}).get("eligible", []))
    found = []
    for sid, fam, name, _ in STRATEGIES:
        s = signal_at(ctx, i, sid)
        if s:
            found.append(dict(s, name=name, family=fam, eligible=sid in elig, ref_close=ctx.c[i], atr=ctx.atr[i]))
    found.sort(key=lambda s: (not s["eligible"], s["strategy"]))
    return (found[0] if found else None), dict(research=found, eligible=sorted(elig), bar=sc._iso(closed[-1][0] + 3600),
                                               note="At most one opportunity is returned. A signal from a variant that is not eligible is research only.")


def main(argv=None):
    import argparse
    import gold_intraday as gi
    ap = argparse.ArgumentParser(description="Gold rebuild: strategies A and B, pre-registered backtest against the archived engine.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rerun", action="store_true")
    a = ap.parse_args(argv)
    if not a.rerun:
        saved = load_result()
        if saved:
            print(json.dumps(saved) if a.json else "saved result from %s" % saved["created"])
            return 0
    t0 = time.time()
    h1 = gi.get_bars("GC=F", "60m", "6mo", 300)
    now = h1[-1][0] + 3600
    h1 = [b for b in h1 if b[0] + 3600 <= now]
    reps = ex.load_opportunities()
    cost = float(assets.profile("GC=F")["default_cost"])
    doc = run(h1, reps, now, cost)
    doc["seconds"] = round(time.time() - t0, 1)
    save_result(doc)
    if a.json:
        print(json.dumps(doc))
    else:
        for h in doc["hypotheses"]:
            d = h["development"]
            print("%-3s %-62s %-20s n=%s mean=%s" % (h["id"], h["name"][:62], h["decision"], d.get("trades"), None if d.get("mean_r") is None else round(d["mean_r"], 3)))
        print("old engine (Gold, same window): n=%s mean=%s" % (doc["old_engine"]["development"]["trades"], doc["old_engine"]["development"]["mean_r"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
