"""Phases 3 to 5: regime classifier v2 with ablation, trend and reversal filters, and breakout entry modes. Standard library only. Read only.

Every rule, threshold, split and decision criterion here was fixed in webui/docs/PHASE_3_5_PREREGISTRATION.md BEFORE the experiments were run.
Nothing is tuned: each hypothesis is evaluated on the TEST split once and its result is written to the experiment log, rejected ones included.

Unit of analysis: one unique opportunity (the first recommendation of each cluster of repeated ideas). Results are in R after costs.
"""
import bisect
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loss_attribution as la  # noqa: E402
import regimes  # noqa: E402
import scorecard as sc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPPS = os.path.join(ROOT, "trader", "replay_opportunities.json")
LOG = os.path.join(ROOT, "trader", "experiment_log.json")
RESULT = os.path.join(ROOT, "trader", "experiment_result.json")
SPLIT_FRAC, MIN_TEST, N_HYP, BOOT, PERM = 0.6, 100, 12, 4000, 500
ALPHA = 0.05 / N_HYP
GROUPS = ("h4_bias", "h1_bias", "slope", "adx_er", "vol_ratio", "news")
FINAL_FILLED = ("WIN", "LOSS", "TIMED OUT")
RETEST_BARS, RETEST_ATR, SWEEP_BARS, NEWS_SEC = 6, 0.15, 12, 3600


# ---------------------------------------------------------------- entry modes (the same management rules as scorecard.grade) ---------

def manage(rec, seq, i0, fill, limit_bar=False):
    """Run an already filled trade from bar i0. Mirrors scorecard.grade exactly after the fill; scorecard's tests pin the two together."""
    step, sgn = sc.STEP[rec["tf"]], (1 if rec["side"] == "long" else -1)
    stop, tg, risk = rec["stop"], rec["targets"], abs(rec["entry"] - rec["stop"])
    out = dict(state="WAITING", legs=[], best_target=0, exit_time=None, note="", engine=sc.ENGINE)
    k, rem, real, stop_cur, closed, bars_in, last_close = 0, 1.0, 0.0, stop, False, 0, None
    for idx in range(i0, len(seq)):
        t, hi, lo, cl, op = seq[idx]
        last_close = cl
        bars_in += 1
        if (lo <= stop_cur) if sgn == 1 else (hi >= stop_cur):
            px = min(stop_cur, op) if sgn == 1 else max(stop_cur, op)
            real += rem * (px - fill) * sgn / risk
            out["legs"].append(dict(price=px, frac=rem, why="stop" if k == 0 else "stop at entry"))
            rem, closed = 0.0, True
            out.update(exit_time=sc._iso(t + step), note="stopped out" if k == 0 else "stopped at the entry after target 1 was reached")
            break
        if limit_bar:
            limit_bar = False
            continue
        while k < 3 and rem > 1e-9 and ((hi >= tg[k]) if sgn == 1 else (lo <= tg[k])):
            real += sc.SPLIT[k] * (tg[k] - fill) * sgn / risk
            out["legs"].append(dict(price=tg[k], frac=sc.SPLIT[k], why="target %d" % (k + 1)))
            rem -= sc.SPLIT[k]
            k += 1
            if k == 1:
                stop_cur = fill
        if k >= 1 and rem > 1e-9 and ((lo <= stop_cur) if sgn == 1 else (hi >= stop_cur)):
            out["legs"].append(dict(price=fill, frac=rem, why="stop at entry"))
            rem, closed = 0.0, True
            out.update(exit_time=sc._iso(t + step), note="stopped at the entry after target %d" % k)
            break
        if k == 3 or rem <= 1e-9:
            closed = True
            out.update(exit_time=sc._iso(t + step), note="all three targets reached")
            break
        if bars_in >= rec["hold_bars"]:
            real += rem * (cl - fill) * sgn / risk
            out["legs"].append(dict(price=cl, frac=rem, why="time exit"))
            rem, closed = 0.0, True
            out.update(state="WIN" if k >= 1 else "TIMED OUT", exit_time=sc._iso(t + step), note="time exit after %d bars" % rec["hold_bars"])
            break
    cost_r = rec["cost"] / risk
    if out["state"] == "WAITING":
        if closed:
            out["state"] = "WIN" if k >= 1 else "LOSS"
        else:
            real += rem * ((last_close - fill) * sgn / risk if last_close is not None else 0.0)
            out["state"] = "OPEN"
            out["legs"].append(dict(price=last_close, frac=rem, why="mark (not closed)"))
            out["note"] = "triggered; not closed (marked at the last close)"
    out.update(fill_price=fill, fill_time=sc._iso(seq[i0][0]), best_target=k, r_gross=real, r_net=real - cost_r, risk=risk, risk_fill=abs(fill - stop))
    out["final"] = out["state"] in ("LOSS", "TIMED OUT") or (out["state"] == "WIN" and closed)
    return out


def _dead(state, note):
    return dict(state=state, note=note, final=True, fill_price=None, r_net=None, r_gross=None, legs=[], engine=sc.ENGINE)


def grade_mode(rec, bars, now, mode, atr=None):
    """Grade one plan under an entry mode: 'limit' (pullback), 'close' (breakout, the current rule), 'stop' (breakout stop order), 'retest'."""
    step, sgn = sc.STEP[rec["tf"]], (1 if rec["side"] == "long" else -1)
    entry, stop, tg, tick = rec["entry"], rec["stop"], rec["targets"], rec["tick"]
    seq = [b for b in bars if b[0] >= rec["created_ts"] and b[0] + step <= now]

    def beyond_stop(c):
        return c < stop if sgn == 1 else c > stop

    def beyond_entry(c):
        return c > entry if sgn == 1 else c < entry

    waited = 0
    if mode == "limit":
        for i, (t, hi, lo, cl, op) in enumerate(seq):
            waited += 1
            if waited > rec["expiry_bars"]:
                return _dead("NOT TRIGGERED", "never traded through the entry")
            if (lo <= entry - tick) if sgn == 1 else (hi >= entry + tick):
                fill = min(entry, op) if sgn == 1 else max(entry, op)
                if (fill - stop) * sgn <= 0:
                    return _dead("INVALIDATED", "opened beyond the stop")
                return manage(rec, seq, i, fill, limit_bar=True)
            if beyond_stop(cl):
                return _dead("INVALIDATED", "closed beyond the stop first")
        return dict(state="WAITING", final=False, note="", fill_price=None, r_net=None, legs=[], engine=sc.ENGINE)
    if mode in ("close", "retest"):
        trig = None
        for i, (t, hi, lo, cl, op) in enumerate(seq):
            if trig is None:
                waited += 1
                if waited > rec["expiry_bars"]:
                    return _dead("NOT TRIGGERED", "no closing candle beyond the level")
                if beyond_entry(cl):
                    trig = i
                    if mode == "close":
                        if i + 1 >= len(seq):
                            break
                        fill = seq[i + 1][4]
                        if (fill - stop) * sgn <= 0 or (tg[0] - fill) * sgn <= 0:
                            return _dead("NOT TRIGGERED", "no clean entry at the next open")
                        return manage(rec, seq, i + 1, fill)
                elif beyond_stop(cl):
                    return _dead("INVALIDATED", "closed beyond the stop before the breakout")
                continue
            # retest mode: look for a bar that comes back to the level and closes on the breakout side
            if i - trig > RETEST_BARS:
                return _dead("NOT TRIGGERED", "no retest within %d bars" % RETEST_BARS)
            if beyond_stop(cl):
                return _dead("INVALIDATED", "closed beyond the stop before a retest")
            if (hi >= tg[0]) if sgn == 1 else (lo <= tg[0]):
                return _dead("NOT TRIGGERED", "the first target was reached before any retest (missed)")
            tol = RETEST_ATR * (atr or 0.0)
            touched = (lo <= entry + tol) if sgn == 1 else (hi >= entry - tol)
            if touched and beyond_entry(cl):
                if i + 1 >= len(seq):
                    break
                fill = seq[i + 1][4]
                if (fill - stop) * sgn <= 0 or (tg[0] - fill) * sgn <= 0:
                    return _dead("NOT TRIGGERED", "no clean entry at the next open")
                return manage(rec, seq, i + 1, fill)
        return dict(state="WAITING", final=False, note="", fill_price=None, r_net=None, legs=[], engine=sc.ENGINE)
    if mode == "stop":
        for i, (t, hi, lo, cl, op) in enumerate(seq):
            waited += 1
            if waited > rec["expiry_bars"]:
                return _dead("NOT TRIGGERED", "price never traded beyond the level")
            if (hi >= entry) if sgn == 1 else (lo <= entry):
                fill = (max(op, entry) + tick) if sgn == 1 else (min(op, entry) - tick)       # worse of the open and the trigger, plus one tick
                if (fill - stop) * sgn <= 0 or (tg[0] - fill) * sgn <= 0:
                    return _dead("NOT TRIGGERED", "no clean stop-order fill")
                return manage(rec, seq, i, fill, limit_bar=True)                                 # no target credit on the bar that triggers the order
            if beyond_stop(cl):
                return _dead("INVALIDATED", "closed beyond the stop first")
        return dict(state="WAITING", final=False, note="", fill_price=None, r_net=None, legs=[], engine=sc.ENGINE)
    raise ValueError("unknown mode " + mode)


# ---------------------------------------------------------------- features (all causal: last CLOSED bar at the recommendation time) ---

def features(rec, ctxs, events):
    sym, ts = rec["symbol"], rec["created_ts"]
    c1, c4, ct = ctxs.get((sym, "1h")), ctxs.get((sym, "4h")), ctxs.get((sym, rec["tf"]))
    s1, s4 = (c1.snap(ts) if c1 else None), (c4.snap(ts) if c4 else None)
    i1 = c1.last_closed(ts) if c1 else -1
    f = dict(s1=s1, s4=s4, i1=i1)
    if ct:
        i = ct.last_closed(ts)
        if 0 <= i < len(ct.bars):
            b = ct.bars[i]
            f.update(candle_up=b[3] > b[4], candle_range=b[1] - b[2], candle_body=abs(b[3] - b[4]), tf_atr=(ct.snap(ts) or {}).get("atr"))
    f["sweeps"] = [c1.A.snaps[j].get("sweep") for j in range(max(0, i1 - SWEEP_BARS + 1), i1 + 1)] if (c1 and c1.A is not None and i1 >= 0) else []
    f["news_near"] = any(e.get("importance") == "high" and abs(e["ts"] - ts) <= NEWS_SEC for e in (events or []))
    return f


# ---------------------------------------------------------------- regime classifier v2 ------------------------------------------------

def regime_v2(f, mask=()):
    """One of nine classes from completed candles only. Groups in `mask` are neutralised (their condition is skipped) for the ablation."""
    s1, s4 = f.get("s1"), f.get("s4")
    if s1 is None:
        return "uncertain"
    if "news" not in mask and (f.get("news_near") or s1.get("post_news")):
        return "news_instability"
    adx, er, sl, vr = s1.get("adx"), s1.get("er"), s1.get("slope_atr"), s1.get("vol_ratio")
    if adx is None or er is None or sl is None or vr is None:
        return "uncertain"
    bias1, bias4 = s1.get("bias"), (s4 or {}).get("bias")
    adx_ok = lambda lo_adx, lo_er: ("adx_er" in mask) or (adx >= lo_adx and er >= lo_er)   # noqa: E731
    strong = adx_ok(25, 0.30)
    if "vol_ratio" not in mask:
        if vr >= 1.3 and not strong:
            return "vol_expansion"
        if vr <= 0.7 and adx < 20 and "adx_er" not in mask:
            return "vol_compression"
    up = "bullish"
    if strong:
        slope_agrees = ("slope" in mask) or (("h1_bias" in mask) or ((sl > 0) == (bias1 == up) and bias1 in ("bullish", "bearish")))
        h4_agrees = ("h4_bias" in mask) or bias4 == bias1
        if slope_agrees and h4_agrees:
            d = (sl > 0) if "slope" not in mask else (bias1 == up)
            return "strong_bull" if d else "strong_bear"
        return "uncertain"
    if adx_ok(18, 0.20):
        if "h1_bias" in mask or bias1 in ("bullish", "bearish"):
            d = (bias1 == up) if "h1_bias" not in mask else (sl > 0)
            return "weak_bull" if d else "weak_bear"
        return "uncertain"
    if (adx < 18 or "adx_er" in mask) and (bias1 == "range" or "h1_bias" in mask):
        return "sideways"
    return "uncertain"


# ---------------------------------------------------------------- statistics --------------------------------------------------------------

def metrics(trades, recommended, boot_seed=7, alpha=None):
    """trades: list of (time_key, r_net) for filled, final trades. recommended: number of opportunities the variant would have recommended."""
    rs = [r for _, r in trades]
    n = len(rs)
    out = dict(trades=n, recommended=recommended, fill_rate=(n / recommended if recommended else None), mean_r=(sum(rs) / n if n else None),
               per_opportunity_r=(sum(rs) / recommended if recommended else None), band=None, band_corrected=None, profit_factor=None, max_drawdown_r=0.0)
    if n:
        pos, neg = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
        out["profit_factor"] = pos / neg if neg > 0 else None
        eq = peak = dd = 0.0
        for _, r in sorted(trades, key=lambda x: x[0]):
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        out["max_drawdown_r"] = dd
    if n >= 20:
        rnd = random.Random(boot_seed)
        means = sorted(sum(rnd.choices(rs, k=n)) / n for _ in range(BOOT))
        pick = lambda q: means[min(len(means) - 1, max(0, int(q * len(means))))]   # noqa: E731
        out["band"] = [pick(0.025), pick(0.975)]
        a_ = ALPHA if alpha is None else alpha
        out["band_corrected"] = [pick(a_), pick(1 - a_)]
    return out


def decide(train, test, base_train, base_test, is_baseline=False):
    """The pre-registered decision rule."""
    if is_baseline:
        return "BASELINE", ["reference for the comparison"]
    n = test["trades"]
    if n < MIN_TEST:
        return "INSUFFICIENT SAMPLE", ["only %d filled test opportunities; at least %d are needed" % (n, MIN_TEST)]
    why = []
    if not (test["mean_r"] is not None and test["mean_r"] > 0 and test["band_corrected"] and test["band_corrected"][0] > 0):
        why.append("test mean net R %s is not above zero with the multiple-testing corrected band" % ("n/a" if test["mean_r"] is None else "%+.3f" % test["mean_r"]))
    if base_test["mean_r"] is not None and test["mean_r"] is not None and not test["mean_r"] > base_test["mean_r"]:
        why.append("it does not beat the baseline on the test split (%+.3f vs %+.3f)" % (test["mean_r"], base_test["mean_r"]))
    if train["mean_r"] is not None and base_train["mean_r"] is not None and train["mean_r"] < base_train["mean_r"]:
        why.append("on the train split it is worse than the baseline, so the direction is not consistent")
    return ("ADOPTED", ["passed every pre-registered criterion"]) if not why else ("REJECTED", why)


def eta_squared(labels, values):
    """Share of the variance in `values` explained by the grouping `labels`."""
    n = len(values)
    if n < 3:
        return None
    mean = sum(values) / n
    sst = sum((v - mean) ** 2 for v in values)
    if sst <= 0:
        return None
    groups = {}
    for lab, v in zip(labels, values):
        groups.setdefault(lab, []).append(v)
    ssb = sum(len(g) * (sum(g) / len(g) - mean) ** 2 for g in groups.values())
    return ssb / sst


def permutation_null(labels, values, seed=11):
    rnd = random.Random(seed)
    lab = list(labels)
    vals = []
    for _ in range(PERM):
        rnd.shuffle(lab)
        e = eta_squared(lab, values)
        if e is not None:
            vals.append(e)
    vals.sort()
    return vals[int(0.95 * (len(vals) - 1))] if vals else None


def confirming_bar(rec, bars, now, ctxs):
    """The first closed bar whose close is beyond the breakout level (within the plan's expiry): its distance in ATR and body share of its range."""
    step, sgn = sc.STEP[rec["tf"]], (1 if rec["side"] == "long" else -1)
    seq = [b for b in bars if b[0] >= rec["created_ts"] and b[0] + step <= now][:rec["expiry_bars"]]
    ctx = ctxs.get((rec["symbol"], rec["tf"]))
    for t, hi, lo, cl, op in seq:
        if (cl - rec["entry"]) * sgn > 0:
            i = ctx.index_at(t) if ctx else None
            atr = ctx.A.snaps[i].get("atr") if (ctx is not None and ctx.A is not None and i is not None) else None
            if not atr or hi <= lo:
                return None
            return dict(dist_atr=(cl - rec["entry"]) * sgn / atr, body_frac=abs(cl - op) / (hi - lo))
        if (cl < rec["stop"]) if sgn == 1 else (cl > rec["stop"]):
            return None
    return None

# ---------------------------------------------------------------- the experiments --------------------------------------------------------

def trade_of(rec, res):
    """(sortable time, net R) when the opportunity filled and finished, else None."""
    return (res["fill_time"], res["r_net"]) if res and res.get("final") and res.get("state") in FINAL_FILLED and res.get("r_net") is not None else None


def evaluate(name, reps, results, cut_ts, passes=None):
    """Metrics on TRAIN and TEST for the opportunities a variant takes. results[id] is the graded result under the variant's entry mode."""
    out = {}
    for split in ("train", "test"):
        sel = [r for r in reps if (r["created_ts"] < cut_ts) == (split == "train") and (passes is None or passes(r))]
        trades, open_n = [], 0
        for r in sel:
            res = results.get(r["id"])
            if res and res.get("state") in ("OPEN",):
                open_n += 1
                continue
            t = trade_of(r, res)
            if t:
                trades.append(t)
        out[split] = metrics(trades, len(sel) - open_n)
        out[split]["open_excluded"] = open_n
    return out


def run(reps, ctxs, bars_by_symbol, events, now, log_path=None):
    reps = sorted(reps, key=lambda r: r["created_ts"])
    cut_ts = reps[int(SPLIT_FRAC * len(reps))]["created_ts"] if reps else 0
    F = {r["id"]: features(r, ctxs, events) for r in reps}
    hyps = []
    pull = [r for r in reps if r["kind"] == "pullback"]
    brk = [r for r in reps if r["kind"] == "breakout"]
    base_res = {r["id"]: r.get("result") for r in reps}

    def add(phase, hid, name, definition, ev, base, is_base=False, extra=None):
        dec, why = decide(ev["train"], ev["test"], base["train"], base["test"], is_base)
        hyps.append(dict(phase=phase, id=hid, name=name, definition=definition, train=ev["train"], test=ev["test"], decision=dec, reasons=why, **(extra or {})))

    # ---- Phase 4
    net_ok = lambda r: ((r.get("blend_r") or 0) - r["cost"] / abs(r["entry"] - r["stop"])) >= 1.0   # noqa: E731

    def with_trend(r):
        f = F[r["id"]]
        s1, s4 = f["s1"], f["s4"]
        if not s1 or not s4 or "candle_up" not in f:
            return False
        long = r["side"] == "long"
        want = "bullish" if long else "bearish"
        trend_ok = s1.get("trend") in (("strong_bull", "weak_bull") if long else ("strong_bear", "weak_bear"))
        candle = f["candle_up"] if long else not f["candle_up"]
        level = s1.get("swing_high") if long else s1.get("swing_low")
        atr = s1.get("atr") or 0.0
        clear = True
        if level is not None:
            gap = (level - r["entry"]) if long else (r["entry"] - level)
            clear = not (0 < gap < 1.0 * atr)
        return s1.get("bias") == want and s4.get("bias") == want and trend_ok and candle and clear and net_ok(r)

    def counter(r):
        s1 = F[r["id"]]["s1"]
        return bool(s1) and s1.get("bias") in ("bullish", "bearish") and s1.get("bias") != ("bullish" if r["side"] == "long" else "bearish")

    # counter-trend means the plan's own label (the bias read from the higher timeframes when it was recommended); see the clarification in the pre-registration
    def reversal_c(r):
        f = F[r["id"]]
        s1 = f["s1"]
        if not s1 or r.get("trend") != "counter":
            return False
        need = "bull" if r["side"] == "long" else "bear"
        recent_choch = s1.get("last_choch") is not None and f["i1"] - s1["last_choch"] <= SWEEP_BARS
        return need in f["sweeps"] and recent_choch and s1.get("bias") == ("bullish" if r["side"] == "long" else "bearish") and net_ok(r)

    base4 = evaluate("A", pull, base_res, cut_ts)
    add(4, "A", "A. Current strategy, no filter", "All pullback plans.", base4, base4, is_base=True)
    for hid, name, df, fn in (("B", "B. Higher-timeframe trend filter", "H4 and H1 bias agree with the side, H1 trend agrees, last candle confirms, not beneath nearby resistance, net reward at least 1R.", with_trend),
                              ("C", "C. Confirmed reversal filter (counter-trend plans only)", "Sweep in the plan direction and change of character within 12 H1 bars, H1 bias now agrees, net reward at least 1R.", reversal_c),
                              ("D", "D. Combined selective strategy", "B for with-trend plans or C for counter-trend plans.", lambda r: with_trend(r) or reversal_c(r))):
        add(4, hid, name, df, evaluate(hid, pull, base_res, cut_ts, fn), base4)

    # ---- Phase 5
    def graded(mode):
        out = {}
        for r in brk:
            b = (bars_by_symbol.get(r["symbol"]) or {}).get(r["tf"])
            atr = F[r["id"]].get("tf_atr")
            out[r["id"]] = grade_mode(r, b, now, mode, atr) if b else None
        return out

    close_res = graded("close")
    CONF = {}
    for r in brk:                           # the confirming bar of the baseline rule: first closed bar beyond the level within the plan's expiry
        b = (bars_by_symbol.get(r["symbol"]) or {}).get(r["tf"])
        c = confirming_bar(r, b, now, ctxs) if b else None
        if c:
            CONF[r["id"]] = c
    base5 = evaluate("A", brk, close_res, cut_ts)
    add(5, "A5", "A. Breakout confirmed by a closing candle (current)", "Wait for a close beyond the level, enter at the next open.", base5, base5, is_base=True)
    add(5, "stop", "Breakout stop order", "Enter when price trades beyond the level, at the worse of the open and the level plus a tick.", evaluate("stop", brk, graded("stop"), cut_ts), base5)
    add(5, "retest", "Breakout followed by a confirmed retest", "After the closing candle wait up to 6 bars for a return to within 0.15 ATR that closes on the breakout side.", evaluate("retest", brk, graded("retest"), cut_ts), base5)

    def trig_bar(r):
        return F[r["id"]]

    flt = (("vol", "Filter: volatility expansion", "H1 vol_ratio at least 1.2.", lambda r: bool(F[r["id"]]["s1"]) and (F[r["id"]]["s1"].get("vol_ratio") or 0) >= 1.2),
           ("align", "Filter: trend alignment", "Plan labelled with-trend.", lambda r: r.get("trend") == "with"),
           ("dist", "Filter: breakout distance", "Confirming close at least 0.2 ATR beyond the level.", lambda r: CONF.get(r["id"]) is not None and CONF[r["id"]]["dist_atr"] >= 0.2),
           ("candle", "Filter: candle quality", "Confirming candle body at least half of its range.", lambda r: CONF.get(r["id"]) is not None and CONF[r["id"]]["body_frac"] >= 0.5),
           ("sweep", "Filter: no recent adverse sweep", "No sweep against the trade in the last 6 H1 bars.", lambda r: ("bear" if r["side"] == "long" else "bull") not in F[r["id"]]["sweeps"][-6:]),
           ("news", "Filter: no news window", "No high-impact release within 60 minutes either side.", lambda r: not F[r["id"]]["news_near"]))
    for hid, name, df, fn in flt:
        add(5, hid, name, df, evaluate(hid, brk, close_res, cut_ts, fn), base5)

    # ---- Phase 3: regime classifier and ablation (all graded opportunities that filled, test split)
    filled = [(r, trade_of(r, base_res.get(r["id"]))) for r in reps]
    filled = [(r, t) for r, t in filled if t]
    test_items = [(r, t[1]) for r, t in filled if r["created_ts"] >= cut_ts]
    train_items = [(r, t[1]) for r, t in filled if r["created_ts"] < cut_ts]

    def eta(items, mask=()):
        return eta_squared([regime_v2(F[r["id"]], mask) for r, _ in items], [v for _, v in items])

    full_test, full_train = eta(test_items), eta(train_items)
    null95 = permutation_null([regime_v2(F[r["id"]]) for r, _ in test_items], [v for _, v in test_items])
    has_signal = full_test is not None and null95 is not None and full_test > null95
    abl = []
    for g in GROUPS:
        e = eta(test_items, (g,))
        abl.append(dict(group=g, test_eta2=e, change=(None if e is None or full_test is None else e - full_test),
                        adds_value=bool(has_signal and e is not None and full_test is not None and e < full_test)))
    by_regime = {}
    for r, v in test_items:
        by_regime.setdefault(regime_v2(F[r["id"]]), []).append(v)
    regime = dict(classes=["strong_bull", "strong_bear", "weak_bull", "weak_bear", "sideways", "vol_compression", "vol_expansion", "news_instability", "uncertain"],
                  test_eta2=full_test, train_eta2=full_train, permutation_95th=null95, has_signal=has_signal, ablation=abl,
                  by_regime={k: dict(trades=len(v), mean_r=sum(v) / len(v)) for k, v in sorted(by_regime.items())},
                  verdict=("the regime labels explain more of the net-R variance than random labels (test)" if has_signal else
                           "NO DETECTABLE SIGNAL: on the test split the regime labels explain no more of the net-R variance than random labels do, so no feature group can be said to add value"))
    doc = dict(created=sc._iso(now), split=dict(fraction=SPLIT_FRAC, cut=sc._iso(cut_ts), train_opportunities=len([r for r in reps if r["created_ts"] < cut_ts]),
                                              test_opportunities=len([r for r in reps if r["created_ts"] >= cut_ts])),
               hypotheses_total=N_HYP, bonferroni_alpha=ALPHA, hypotheses=hyps, regime=regime,
               breakout_policy=("DISABLED for strict mode: no breakout variant passed the pre-registered criteria" if not any(h["decision"] == "ADOPTED" for h in hyps if h["phase"] == 5)
                                else "a breakout variant was adopted; see the hypotheses"),
               pullback_policy=("no pullback filter passed the pre-registered criteria; the baseline stays as it is" if not any(h["decision"] == "ADOPTED" for h in hyps if h["phase"] == 4 and h["id"] != "A")
                                else "a pullback filter was adopted; see the hypotheses"),
               not_tested=["spread expansion (no bid or ask in this data)", "distance to opposing liquidity beyond the swing levels", "M15 momentum before the 30 day 15 minute history"],
               caution="Opportunities are clustered by hand-made rules and still overlap in time, so the bands are optimistic. Six months, two markets, delayed last-trade bars, flat costs. Nothing here is tuned on the test split. Past results are not a promise. Not financial advice.")
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
    entry = dict(created=doc["created"], split=doc["split"], hypotheses=[dict(phase=h["phase"], id=h["id"], name=h["name"], definition=h["definition"], decision=h["decision"],
                                                                         reasons=h["reasons"], train_mean_r=h["train"]["mean_r"], test_mean_r=h["test"]["mean_r"], test_trades=h["test"]["trades"]) for h in doc["hypotheses"]],
                    regime_has_signal=doc["regime"]["has_signal"])
    key = lambda e: [(h["id"], h["decision"], h["test_mean_r"], h["test_trades"]) for h in e["hypotheses"]]   # noqa: E731
    if log and key(log[-1]) == key(entry):                  # the same data gives the same answer: a repeat is not a new evaluation
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


def load_opportunities(path=None):
    try:
        with open(path or OPPS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def main(argv=None):
    import argparse
    import gold_intraday as gi
    import macro
    import recommendation_backtest as rb
    ap = argparse.ArgumentParser(description="Phases 3 to 5 pre-registered experiments.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rerun", action="store_true", help="recompute with the latest data instead of showing the saved result")
    a = ap.parse_args(argv)
    now0 = time.time()
    if not a.rerun:
        saved = load_result()
        if saved:
            print(json.dumps(saved) if a.json else "saved result from %s" % saved["created"])
            return 0
    reps = load_opportunities()
    if not reps:
        rb.run()
        reps = load_opportunities()
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
    doc = run(reps, ctxs, bars_by, events, end)
    doc["errors"], doc["seconds"] = errors, round(time.time() - now0, 1)
    save_result(doc)
    if a.json:
        print(json.dumps(doc))
    else:
        for h in doc["hypotheses"]:
            print("%-6s %-55s %-20s test n=%d mean=%s" % (h["id"], h["name"][:55], h["decision"], h["test"]["trades"], h["test"]["mean_r"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
