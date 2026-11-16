"""Event-driven backtester, statistics and validation gate. Standard library only.

Signals are decided at the CLOSE of a bar and executed from the NEXT bar. Execution is deliberately conservative:
  * market orders fill at the next open plus half the spread plus slippage against you;
  * a limit order needs price to trade THROUGH its level by penetration ticks, not just touch it;
  * a stop-entry fills at the worse of the open and the level, plus slippage;
  * if a stop and a target are both reachable inside one bar, the STOP is assumed to be hit first;
  * a stop that gaps exits at the open, not at the stop price;
  * last-trade bars stand in for bid/ask: longs are stopped on the low and sold at the high, shorts are stopped
    when the high plus the spread reaches the stop and covered when the low plus the spread reaches the target.
Nothing here assumes a strategy has an edge; verdict() decides whether the evidence is good enough.
"""
import hashlib
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402
import regimes  # noqa: E402
import strategies  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER = os.path.join(ROOT, "trader")

EXEC_DEFAULTS = dict(split=(0.5, 0.3, 0.2), be_after_t1=True, max_hold=48, penetration_ticks=1.0, min_risk_atr=0.3,
                     daily_loss_limit_r=None, one_position=True)
GATE_DEFAULTS = dict(min_oos_trades=100, min_holdout_trades=30, dsr_min=0.95, stress_multiple=2.0, bootstrap=1000)
WF_DEFAULTS = dict(train_min=600, test_len=250, embargo=None, holdout_frac=0.2, min_train_trades=10, warmup=60, seed=7)


def costs(spread=0.0, slippage=0.0, commission_per_unit=0.0):
    for k, v in (("spread", spread), ("slippage", slippage), ("commission_per_unit", commission_per_unit)):
        if v < 0:
            raise ValueError("%s cannot be negative" % k)
    return dict(spread=float(spread), slippage=float(slippage), commission_per_unit=float(commission_per_unit))


def scaled(c, m):
    return costs(c["spread"] * m, c["slippage"] * m, c["commission_per_unit"] * m)


def round_trip(c):
    return c["spread"] + 2 * c["slippage"] + c["commission_per_unit"]


# ------------------------------------------------------------------------------------ execution

def _simulate(bars, sig, c, cfg):
    """Fill one signal and manage it to the end. Returns (trade, None) or (None, reason)."""
    sg = 1 if sig["side"] == "long" else -1
    n = len(bars)
    half, slip = c["spread"] / 2.0, c["slippage"]
    tick = assets.tick_for(sig["price"])
    pen = cfg["penetration_ticks"] * tick
    stop, i0 = sig["stop"], sig["decided_i"]
    price = sig["price"]
    fill_i = fill = None
    last = min(n, i0 + 1 + max(1, sig["expires"]))
    for j in range(i0 + 1, last):
        o, h, l, cl = bars[j][4], bars[j][1], bars[j][2], bars[j][3]
        if sig["kind"] == "market":
            fill_i, fill = j, o + sg * (half + slip)
            break
        beyond_stop = cl <= stop if sg == 1 else cl >= stop
        if sig["kind"] == "limit":
            if beyond_stop:
                return None, "cancelled: closed beyond the stop before filling"
            reach = (l + half <= price - pen) if sg == 1 else (h - half >= price + pen)
            if reach:
                fill_i, fill = j, price
                break
        elif sig["kind"] == "stop":
            trig = (h + half >= price) if sg == 1 else (l - half <= price)
            if trig:
                fill_i = j
                fill = (max(o + half, price) + slip) if sg == 1 else (min(o - half, price) - slip)
                break
    if fill_i is None:
        return None, "not filled within %d bar(s)" % max(1, sig["expires"])
    risk = (fill - stop) * sg
    atr = sig.get("atr") or 0.0
    if risk <= max(cfg["min_risk_atr"] * atr, tick):
        return None, "stop too close to the fill (%.2f)" % risk
    targets = [t for t in sig["targets"] if (t - fill) * sg > 0.25 * risk]
    if not targets:
        return None, "no target left beyond the fill"
    w = list(cfg["split"][:len(targets)])
    tot = sum(w)
    w = [x / tot for x in w]
    stop_cur, remaining, exits = stop, 1.0, []
    mae = mfe = 0.0
    t_hit = 0
    exit_i = n - 1
    how_last = "eod"
    for m in range(fill_i, n):
        o, h, l, cl = bars[m][4], bars[m][1], bars[m][2], bars[m][3]
        open_ok = not (m == fill_i and sig["kind"] != "market")
        mae = max(mae, ((fill - l) if sg == 1 else (h + half - fill)) / risk)
        mfe = max(mfe, ((h - fill) if sg == 1 else (fill - (l + half))) / risk)
        hit = (l <= stop_cur) if sg == 1 else (h + half >= stop_cur)
        if hit:
            if sg == 1:
                px = (o - slip) if (open_ok and o <= stop_cur) else stop_cur - slip
            else:
                px = (o + half + slip) if (open_ok and o + half >= stop_cur) else stop_cur + slip
            exits.append((m, px, remaining, "breakeven" if (t_hit and stop_cur == fill) else "stop"))
            remaining, exit_i, how_last = 0.0, m, "stop"
            break
        while t_hit < len(targets) and remaining > 1e-9:
            tg = targets[t_hit]
            reached = (h >= tg) if sg == 1 else (l + half <= tg)
            if not reached:
                break
            exits.append((m, tg, w[t_hit], "target %d" % (t_hit + 1)))
            remaining -= w[t_hit]
            t_hit += 1
            if t_hit == 1 and cfg["be_after_t1"]:
                stop_cur = fill
        if remaining <= 1e-9:
            exit_i, how_last, remaining = m, "target", 0.0
            break
        if m - fill_i >= cfg["max_hold"]:
            exits.append((m, cl - sg * (half + slip), remaining, "time"))
            remaining, exit_i, how_last = 0.0, m, "time"
            break
    if remaining > 1e-9:
        exits.append((n - 1, bars[-1][3] - sg * (half + slip), remaining, "end of data"))
    r = sum(wt * (px - fill) * sg / risk for _, px, wt, _ in exits) - c["commission_per_unit"] / risk
    return dict(id=sig["id"], strategy=sig["strategy"], side=sig["side"], decided_i=i0, decided_t=sig["decided_t"],
                entry_i=fill_i, entry_t=bars[fill_i][0], entry=fill, stop0=stop, risk=risk, targets=targets, kind=sig["kind"],
                exits=[dict(i=a, price=b, weight=wt, how=hw) for a, b, wt, hw in exits], exit_i=exit_i, exit_t=bars[exit_i][0],
                r=r, cost_r=round_trip(c) / risk, mae_r=mae, mfe_r=mfe, bars_held=exit_i - fill_i, how=how_last,
                regime=sig["regime"], session=sig["session"], planned_rr=abs(targets[0] - fill) / risk,
                note=sig.get("note", ""), meta=sig.get("meta", {})), None


def execute(bars, signals, c, cfg=None):
    """Run signals through the execution model in order. Returns (trades, rejected)."""
    cfg = dict(EXEC_DEFAULTS, **(cfg or {}))
    trades, rejected = [], []
    busy = -1
    day_r = {}
    for sig in sorted(signals, key=lambda s: s["decided_i"]):
        if cfg["one_position"] and sig["decided_i"] <= busy:
            rejected.append(dict(i=sig["decided_i"], strategy=sig["strategy"], reason="a position was already open", id=sig["id"]))
            continue
        lim = cfg["daily_loss_limit_r"]
        if lim is not None and day_r.get(sig["decided_t"] // 86400, 0.0) <= -lim:
            rejected.append(dict(i=sig["decided_i"], strategy=sig["strategy"], reason="daily loss limit reached", id=sig["id"]))
            continue
        tr, why = _simulate(bars, sig, c, cfg)
        if tr is None:
            rejected.append(dict(i=sig["decided_i"], strategy=sig["strategy"], reason=why, id=sig["id"]))
            continue
        busy = tr["exit_i"]
        day_r[tr["exit_t"] // 86400] = day_r.get(tr["exit_t"] // 86400, 0.0) + tr["r"]
        trades.append(tr)
    return trades, rejected


def generate(bars, strategy_list, interval_sec=None, htf_sec=None, event_times=None, cost_per_unit=0.0, cfg=None):
    """Step the market analysis once and feed every strategy instance. Returns {id(strategy): signals}."""
    A = regimes.Analysis(bars, cfg, event_times=event_times, interval_sec=interval_sec)
    H = regimes.HTF(bars, htf_sec, cfg, event_times) if htf_sec else None
    out = {id(s): [] for s in strategy_list}
    for s in strategy_list:
        s.reset()
        s.rejected = []
        s.cost_per_unit = cost_per_unit
    for i in range(len(bars)):
        A.step(i)
        hs = H.at(i) if H else None
        for s in strategy_list:
            out[id(s)].extend(s.on_bar(A, i, hs))
    return out, A


def run_strategy(bars, name, params=None, c=None, exec_cfg=None, interval_sec=None, htf_sec=None, event_times=None):
    """One strategy, one parameter set, end to end."""
    c = c or costs()
    s = strategies.make(name, **(params or {}))
    sigs, A = generate(bars, [s], interval_sec, htf_sec, event_times, round_trip(c))
    trades, rej = execute(bars, sigs[id(s)], c, exec_cfg)
    return dict(signals=sigs[id(s)], trades=trades, rejected=[dict(r, strategy=name) for r in s.rejected] + rej, analysis=A)


# ------------------------------------------------------------------------------------ statistics

def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mean_sd(x):
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    return m, sd


def summary(trades, risk_pct=1.0):
    rs = [t["r"] for t in trades]
    n = len(rs)
    if n == 0:
        return dict(n=0)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    m, sd = _mean_sd(rs)
    se = sd / math.sqrt(n) if n > 1 else None
    cum = peak = dd = 0.0
    eq, eq_peak, dd_pct = 1.0, 1.0, 0.0
    streak = best = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        eq *= max(0.0, 1 + risk_pct / 100.0 * r)
        eq_peak = max(eq_peak, eq)
        dd_pct = max(dd_pct, (eq_peak - eq) / eq_peak * 100)
        streak = streak + 1 if r <= 0 else 0
        best = max(best, streak)
    ts = [t["entry_t"] for t in trades]
    span_weeks = max((max(ts) - min(ts)) / 604800.0, 1e-9) if n > 1 else None
    per_year = (n / span_weeks * 52.0) if span_weeks else None
    down = math.sqrt(sum(min(r, 0) ** 2 for r in rs) / n)
    lo, hi = wilson(len(wins), n)
    gl = -sum(losses)
    return dict(n=n, win_rate=len(wins) / n, win_ci=[lo, hi], avg_win=(sum(wins) / len(wins)) if wins else None,
                avg_loss=(sum(losses) / len(losses)) if losses else None, expectancy=m, sd=sd, se=se,
                ci95=[m - 1.96 * se, m + 1.96 * se] if se else [None, None],
                profit_factor=(sum(wins) / gl) if gl > 0 else None, max_dd_r=dd, max_dd_pct=dd_pct, max_consec_losses=best,
                sharpe=(m / sd * math.sqrt(per_year)) if (n >= 30 and sd > 0 and per_year) else None,
                sortino=(m / down * math.sqrt(per_year)) if (n >= 30 and down > 0 and per_year) else None,
                trades_per_week=(n / span_weeks) if span_weeks else None,
                avg_mae_r=sum(t["mae_r"] for t in trades) / n, avg_mfe_r=sum(t["mfe_r"] for t in trades) / n,
                avg_bars=sum(t["bars_held"] for t in trades) / n, avg_cost_r=sum(t["cost_r"] for t in trades) / n,
                total_r=cum)


def segment(trades, key_fn):
    groups = {}
    for t in trades:
        groups.setdefault(key_fn(t), []).append(t)
    out = {}
    for k, v in sorted(groups.items(), key=lambda kv: str(kv[0])):
        s = summary(v)
        out[str(k)] = dict(n=s["n"], expectancy=s["expectancy"], win_rate=s["win_rate"], profit_factor=s["profit_factor"],
                           ci95=s["ci95"])
    return out


def block_bootstrap(rs, n_boot=1000, seed=7, block=None):
    """Moving-block bootstrap of the mean R. Blocks keep short-run dependence between neighbouring trades."""
    n = len(rs)
    if n < 5:
        return None
    b = block or max(1, int(round(n ** (1.0 / 3.0))))
    rnd = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = []
        while len(s) < n:
            st = rnd.randrange(0, n - b + 1)
            s.extend(rs[st:st + b])
        means.append(sum(s[:n]) / n)
    means.sort()
    return dict(block=b, n_boot=n_boot, lo=means[int(0.025 * n_boot)], hi=means[int(0.975 * n_boot) - 1],
                p_le_zero=sum(1 for m in means if m <= 0) / n_boot, seed=seed)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p):
    """Inverse normal CDF (Acklam's rational approximation, relative error about 1e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - lo:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(rs, n_trials, sr_var=None):
    """Probability that the observed per-trade Sharpe ratio beats what the best of n_trials unskilled trials would
    show by luck (Bailey and Lopez de Prado). Uses the per-trade Sharpe, skewness and kurtosis of the R series.
    Returns None when there are too few trades."""
    n = len(rs)
    if n < 10 or n_trials < 1:
        return None
    m, sd = _mean_sd(rs)
    if sd <= 0:
        return None
    sr = m / sd
    sk = sum(((x - m) / sd) ** 3 for x in rs) / n
    ku = sum(((x - m) / sd) ** 4 for x in rs) / n
    var = sr_var if sr_var is not None else 1.0 / (n - 1)          # variance of a Sharpe estimate under the null
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * _phi_inv(1 - 1.0 / max(n_trials, 2)) + g * _phi_inv(1 - 1.0 / (max(n_trials, 2) * math.e))) if n_trials > 1 else 0.0
    denom = math.sqrt(max(1e-12, 1 - sk * sr + (ku - 1) / 4.0 * sr * sr))
    return dict(sr=sr, sr0=sr0, dsr=_phi((sr - sr0) * math.sqrt(n - 1) / denom), n_trials=n_trials, skew=sk, kurtosis=ku)


def holm(pvals):
    """Holm-Bonferroni adjusted p-values, in the input order."""
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    adj = [0.0] * len(pvals)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, (len(pvals) - rank) * pvals[i])
        adj[i] = min(1.0, run)
    return adj


# ------------------------------------------------------------------------------------ validation

def dataset_hash(bars):
    body = json.dumps([[b[0], b[1], b[2], b[3], b[4]] for b in bars], separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def code_version():
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for f in ("indicators.py", "regimes.py", "strategies.py", "backtester.py"):
        with open(os.path.join(here, f), "rb") as fh:
            h.update(fh.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _score(trades, min_n):
    """Selection score: expectancy minus one standard error, so a lucky small sample ranks lower."""
    if len(trades) < min_n:
        return None
    rs = [t["r"] for t in trades]
    m, sd = _mean_sd(rs)
    return m - sd / math.sqrt(len(rs))


def _holdout_path():
    return os.path.join(TRADER, "holdout_log.json")


def holdout_register(key, params, store_path=None):
    """Record that a holdout is being looked at. Returns the attempt number for this holdout segment. A segment
    that has been looked at with different parameters is contaminated and the verdict says so."""
    path = store_path or _holdout_path()
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = {}
    rec = log.setdefault(key, dict(attempts=[], first=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    p = json.dumps(params, sort_keys=True)
    if p not in rec["attempts"]:
        rec["attempts"].append(p)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)
    os.replace(tmp, path)
    return len(rec["attempts"])


def walk_forward(bars, name, c, exec_cfg=None, wf=None, interval_sec=None, htf_sec=None, event_times=None, n_trials_extra=0,
                 holdout_store=None, symbol="", interval=""):
    """Chronological walk-forward with purging, then ONE look at an untouched final holdout.

    Every parameter set in the strategy's grid is run once over the pre-holdout bars (signals are causal, so this is
    the same as running each fold separately). Fold f trains on everything before its test window minus an embargo
    (trades that exit inside the embargo are purged, so no label overlaps the test window), picks the parameters with
    the best expectancy-minus-one-SE, and is judged only on trades decided inside its own test window."""
    wf = dict(WF_DEFAULTS, **(wf or {}))
    exec_cfg = dict(EXEC_DEFAULTS, **(exec_cfg or {}))
    n = len(bars)
    embargo = wf["embargo"] if wf["embargo"] is not None else exec_cfg["max_hold"]
    split = int(n * (1 - wf["holdout_frac"]))
    grid = strategies.param_grid(name)
    insts = [strategies.make(name, **g) for g in grid]
    sigs, A = generate(bars, insts, interval_sec, htf_sec, event_times, round_trip(c))
    per = []
    for s, g in zip(insts, grid):
        tr, _ = execute(bars, sigs[id(s)], c, exec_cfg)
        per.append(dict(params=g, trades=tr))
    folds, oos, chosen = [], [], []
    start = wf["train_min"]
    while start < split:
        end = min(start + wf["test_len"], split)
        best, best_score = None, None
        for k, d in enumerate(per):
            train = [t for t in d["trades"] if t["decided_i"] >= wf["warmup"] and t["exit_i"] < start - embargo]
            sc = _score(train, wf["min_train_trades"])
            if sc is not None and (best_score is None or sc > best_score):
                best, best_score = k, sc
        test_all = {k: [t for t in d["trades"] if start <= t["decided_i"] < end] for k, d in enumerate(per)}
        if best is None:
            folds.append(dict(start=start, end=end, chosen=None, n_test=0, note="no parameter set had enough training trades"))
        else:
            chosen.append(best)
            oos.extend(test_all[best])
            tsum = summary(test_all[best])
            folds.append(dict(start=start, end=end, chosen=per[best]["params"], train_score=best_score, n_test=tsum["n"],
                              test_expectancy=tsum.get("expectancy"),
                              grid_median_test_expectancy=_median([summary(v)["expectancy"] for v in test_all.values() if v])))
        start = end
    # parameter stability: how often neighbours of the chosen set also have positive training expectancy
    stab = []
    for k in set(chosen):
        nb = [j for j, d in enumerate(per) if j != k and sum(1 for key in grid[k] if grid[k][key] != d["params"][key]) == 1]
        pos = sum(1 for j in nb if (summary(per[j]["trades"]).get("expectancy") or 0) > 0)
        stab.append(dict(params=per[k]["params"], times_chosen=chosen.count(k), neighbours=len(nb), neighbours_positive=pos))
    # holdout: the parameters that did best over the whole pre-holdout span, looked at once
    pre = [(k, _score([t for t in d["trades"] if t["decided_i"] >= wf["warmup"] and t["exit_i"] < split - embargo], wf["min_train_trades"]))
           for k, d in enumerate(per)]
    pre = [(k, s) for k, s in pre if s is not None]
    holdout = None
    if pre:
        fk = max(pre, key=lambda x: x[1])[0]
        key = "%s|%s|%s|%d" % (name, symbol, interval, bars[split][0])
        attempts = holdout_register(key, per[fk]["params"], holdout_store)
        ho = [t for t in per[fk]["trades"] if t["decided_i"] >= split]
        holdout = dict(params=per[fk]["params"], attempts=attempts, start_t=bars[split][0], summary=summary(ho))
    n_trials = len(grid) + n_trials_extra
    rs = [t["r"] for t in oos]
    halves = [summary(oos[:len(oos) // 2]).get("expectancy"), summary(oos[len(oos) // 2:]).get("expectancy")] if len(oos) >= 20 else [None, None]
    return dict(strategy=name, folds=folds, oos_trades=oos, oos=summary(oos), holdout=holdout, stability=stab, halves=halves,
                bootstrap=block_bootstrap(rs, GATE_DEFAULTS["bootstrap"], wf["seed"]), dsr=deflated_sharpe(rs, n_trials),
                n_trials=n_trials, grid_size=len(grid), embargo=embargo, split=split, wf=wf, analysis=A,
                all_param_summaries=[dict(params=d["params"], n=len(d["trades"]), expectancy=summary(d["trades"]).get("expectancy")) for d in per])


def _median(v):
    v = sorted(x for x in v if x is not None)
    return v[len(v) // 2] if v else None


def stress(bars, name, params, base_costs, multiples=(0.0, 1.0, 2.0, 3.0), exec_cfg=None, interval_sec=None, htf_sec=None,
           event_times=None, decided_range=None):
    """Re-execute the SAME signals at scaled costs (spread and slippage together). Signals are generated once at base cost."""
    s = strategies.make(name, **(params or {}))
    sigs, _ = generate(bars, [s], interval_sec, htf_sec, event_times, round_trip(base_costs))
    out = []
    for m in multiples:
        tr, _ = execute(bars, sigs[id(s)], scaled(base_costs, m), exec_cfg)
        if decided_range:
            tr = [t for t in tr if decided_range[0] <= t["decided_i"] < decided_range[1]]
        sm = summary(tr)
        out.append(dict(multiple=m, n=sm["n"], expectancy=sm.get("expectancy"), profit_factor=sm.get("profit_factor"),
                        ci95=sm.get("ci95")))
    return out


def verdict(result, stress_rows, gate=None):
    """VALIDATED (research), NOT VALIDATED or INSUFFICIENT SAMPLE, with every reason. This is a statement about past
    out-of-sample data only, never a promise of profit."""
    g = dict(GATE_DEFAULTS, **(gate or {}))
    reasons, fails = [], []
    o = result["oos"]
    n = o.get("n", 0)
    if n < g["min_oos_trades"]:
        return dict(status="INSUFFICIENT SAMPLE", reasons=["only %d out-of-sample trades; at least %d are needed" % (n, g["min_oos_trades"])], checks={})
    checks = {}
    bs = result["bootstrap"]
    checks["bootstrap_ci_lower_above_zero"] = bool(bs and bs["lo"] > 0)
    checks["expectancy_positive"] = (o["expectancy"] or 0) > 0
    st = [r for r in stress_rows if r["multiple"] == g["stress_multiple"]]
    checks["positive_at_%gx_costs" % g["stress_multiple"]] = bool(st and (st[0]["expectancy"] or 0) > 0)
    d = result["dsr"]
    checks["deflated_sharpe_ok"] = bool(d and d["dsr"] >= g["dsr_min"])
    checks["both_halves_positive"] = all((h or 0) > 0 for h in result["halves"])
    ho = result["holdout"]
    ho_n = ho["summary"].get("n", 0) if ho else 0
    checks["holdout_big_enough"] = ho_n >= g["min_holdout_trades"]
    checks["holdout_positive"] = bool(ho and ho_n and (ho["summary"].get("expectancy") or 0) > 0)
    checks["holdout_looked_at_once"] = bool(ho and ho["attempts"] == 1)
    names = {
        "bootstrap_ci_lower_above_zero": "the bootstrap 95% band for expectancy includes zero or is negative",
        "expectancy_positive": "net expectancy is not positive",
        "positive_at_%gx_costs" % g["stress_multiple"]: "expectancy is not positive when costs are %gx" % g["stress_multiple"],
        "deflated_sharpe_ok": "the deflated Sharpe ratio is below %.2f once the number of trials is counted" % g["dsr_min"],
        "both_halves_positive": "the two halves of the out-of-sample record disagree in sign",
        "holdout_big_enough": "the holdout has fewer than %d trades" % g["min_holdout_trades"],
        "holdout_positive": "the untouched holdout is not positive",
        "holdout_looked_at_once": "the holdout has been examined with more than one parameter set",
    }
    for k, ok in checks.items():
        (reasons if ok else fails).append(names[k] if not ok else "passed: " + k.replace("_", " "))
    return dict(status="VALIDATED (research, past data only)" if not fails else "NOT VALIDATED", reasons=fails or reasons, checks=checks)


def run_record(bars, name, symbol, interval, c, exec_cfg, wf, gate, seed, extra=None):
    """Everything needed to reproduce a result."""
    import platform
    return dict(created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), strategy=name, symbol=symbol, interval=interval,
                seed=seed, costs=c, exec=dict(exec_cfg, split=list(exec_cfg["split"])) if exec_cfg else None, walk_forward=wf, gate=gate,
                dataset=dict(rows=len(bars), first=bars[0][0], last=bars[-1][0], sha256=dataset_hash(bars)),
                code_version=code_version(), python=platform.python_version(), **(extra or {}))
