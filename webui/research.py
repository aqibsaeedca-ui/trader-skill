#!/usr/bin/env python3
"""Research runner: data -> strategies -> walk-forward -> validation gate -> ML -> one validation record.

    python webui/research.py run --symbol GC=F --interval 60m
    python webui/research.py run --symbol XAUUSD --interval 5m --source dukascopy      (after downloading ticks)

Results are written to trader/validation.json (latest per market and interval) with the seed, configuration, data
hashes and code version needed to reproduce them. It never places orders and never talks to a broker. A strategy or
model that fails its gate is reported as not validated; nothing is tuned until it passes.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402
import backtester as bt  # noqa: E402
import macro  # noqa: E402
import marketdata as md  # noqa: E402
import ml  # noqa: E402
import strategies as st  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATION = os.path.join(ROOT, "trader", "validation.json")
HTF = {"1m": 900, "5m": 3600, "15m": 3600, "30m": 4 * 3600, "60m": 4 * 3600, "1h": 4 * 3600, "1d": 7 * 86400}
MAX_HOLD = {"1m": 120, "5m": 72, "15m": 48, "30m": 48, "60m": 48, "1h": 48, "1d": 20}
PLACEHOLDER_COSTS = {"gold": dict(spread=0.30, slippage=0.10), "btc": dict(spread=15.0, slippage=10.0)}


def key(symbol, interval):
    return "%s|%s" % (symbol, interval)


def load_validation(path=None):
    try:
        with open(path or VALIDATION, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_validation(rec, path=None):
    p = path or VALIDATION
    data = load_validation(p)
    data[key(rec["symbol"], rec["interval"])] = rec
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def get_bars(symbol, interval, source="yahoo", store=None, now=None):
    """Bars for research: closed bars only, deduplicated, validated. Yahoo history is merged into the on-disk store so
    short windows grow over repeated runs. Returns (bars, info, spread_by_time)."""
    store = store or md.Store()
    now = time.time() if now is None else now
    step = md.INTERVAL_SEC[interval]
    spread = {}
    if source == "yahoo":
        fresh, meta = md.PROVIDERS["yahoo"].fetch(symbol, interval)
        fresh, _ = md.dedupe_bars(fresh)
        fresh, _ = md.completed(fresh, step, now)
        store.save("yahoo", symbol, interval, fresh)
        bars, _, _ = store.load("yahoo", symbol, interval)
        notes = meta["notes"]
    else:
        bars, _, _ = store.load(source, symbol, interval)
        if not bars:
            raise SystemExit("error: nothing stored for %s %s %s; download it first (marketdata.py %s ...)" % (source, symbol, interval, source))
        spread = store.load_extras(source, symbol, interval).get("spread", {})
        notes = ["Data from the on-disk store (%s)." % source]
        bars, _ = md.completed(bars, step, now)
    cal = assets.profile(symbol)["calendar"]
    rep = md.validate_bars(bars, interval, cal, now)
    info = dict(source=source, rows=len(bars), first=md.utc_iso(bars[0][0]) if bars else None, last=md.utc_iso(bars[-1][0]) if bars else None,
                sha256=bt.dataset_hash(bars), quality=rep["status"], issues=rep["issues"], completeness_pct=rep["completeness_pct"],
                notes=notes, bid_ask=bool(spread))
    return bars, info, spread


def default_costs(symbol, spread_series=None, overrides=None):
    pf = assets.profile(symbol)
    base = dict(PLACEHOLDER_COSTS[pf["id"]])
    src = "PLACEHOLDER assumption: replace with your broker's spread and slippage"
    if spread_series:
        sp = sorted(spread_series.values())
        base = dict(spread=sp[len(sp) // 2], slippage=0.25 * sp[len(sp) // 2])
        src = "measured: median spread from stored bid/ask data, slippage assumed at a quarter of it"
    base.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return bt.costs(**base), src


def wf_config(n):
    return dict(train_min=max(250, int(0.30 * n)), test_len=max(100, int(0.12 * n)), holdout_frac=0.2, min_train_trades=8, warmup=60)


def equity_curve(trades):
    cum, out = 0.0, []
    for t in sorted(trades, key=lambda x: x["exit_t"]):
        cum += t["r"]
        out.append(dict(t=t["exit_t"], r=round(cum, 4)))
    return out


def _clean(o):
    """JSON-safe copy: NaN and infinity become None, tuples become lists."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def run(symbol, interval, source="yahoo", seed=7, cost_overrides=None, do_ml=True, quick=False, holdout_store=None, bars_override=None,
        out_path=None, model_dir=None, events=None):
    t0 = time.time()
    if bars_override is not None:
        bars, info, spread = bars_override, dict(source="supplied", rows=len(bars_override), sha256=bt.dataset_hash(bars_override), quality="n/a", issues=[],
                                                  first=md.utc_iso(bars_override[0][0]), last=md.utc_iso(bars_override[-1][0]), notes=[], bid_ask=False), {}
    else:
        bars, info, spread = get_bars(symbol, interval, source)
    if len(bars) < 400:
        raise SystemExit("error: only %d bars; research needs at least 400" % len(bars))
    step = md.INTERVAL_SEC[interval]
    c, cost_src = default_costs(symbol, spread, cost_overrides)
    ev = macro.load_store() if events is None else events
    ev_high = [e["ts"] for e in ev if e["importance"] == "high"]
    exec_cfg = dict(bt.EXEC_DEFAULTS, max_hold=MAX_HOLD.get(interval, 48))
    wf = wf_config(len(bars))
    total_grid = sum(len(st.param_grid(n)) for n in st.STRATEGIES)
    results, pvals = {}, {}
    for name in st.STRATEGIES:
        r = bt.walk_forward(bars, name, c, exec_cfg, wf, interval_sec=step, htf_sec=HTF.get(interval), event_times=ev_high,
                            n_trials_extra=total_grid - len(st.param_grid(name)), holdout_store=holdout_store, symbol=symbol, interval=interval)
        chosen = (r["holdout"] or {}).get("params") or (r["folds"][-1].get("chosen") if r["folds"] and r["folds"][-1].get("chosen") else None)
        stress_rows = r["stress"]                      # out-of-sample only; see backtester.walk_forward
        v = bt.verdict(r, stress_rows)
        oos = r["oos_trades"]
        full = bt.run_strategy(bars, name, chosen or {}, c, exec_cfg, step, HTF.get(interval), ev_high)
        rejections = {}
        for rj in full["rejected"]:
            why = re.sub(r"\d+(\.\d+)?", "#", rj["reason"])                  # group "5 bars" and "7 bars" together
            rejections[why] = rejections.get(why, 0) + 1
        pvals[name] = (r["bootstrap"] or {}).get("p_le_zero", 1.0)
        results[name] = dict(
            status=v["status"], reasons=v["reasons"], checks=v["checks"], oos=r["oos"], holdout=r["holdout"], stress=stress_rows,
            bootstrap=r["bootstrap"], dsr=r["dsr"], n_trials=r["n_trials"], halves=r["halves"], stability=r["stability"], chosen=chosen,
            folds=[{k: v2 for k, v2 in f.items()} for f in r["folds"]], equity=equity_curve(oos),
            by_side=bt.segment(oos, lambda t: t["side"]), by_session=bt.segment(oos, lambda t: t["session"]),
            by_regime=bt.segment(oos, lambda t: t["regime"]), grid_oos=r["all_param_summaries"],
            signals_total=len(full["signals"]), trades_total=len(full["trades"]), rejections=dict(sorted(rejections.items(), key=lambda kv: -kv[1])),
            news_filter=macro.evaluate_news_filter(oos, ev) if ev else dict(verdict="no events stored: run macro.py fetch-fomc and add-nfp", recommended=False),
            record=bt.run_record(bars, name, symbol, interval, c, exec_cfg, wf, bt.GATE_DEFAULTS, seed, dict(params=chosen)))
    names = list(results)
    adj = bt.holm([pvals[n] for n in names])
    for n, a in zip(names, adj):
        results[n]["holm_p"] = a
    out = dict(symbol=symbol, interval=interval, created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), seed=seed, data=info,
               costs=dict(c, source=cost_src), exec=dict(exec_cfg, split=list(exec_cfg["split"])), walk_forward=wf, strategies=results,
               events=dict(count=len(ev), high=len(ev_high)), code_version=bt.code_version(), sample_data=False,
               note="Research on past data. Statuses describe out-of-sample evidence only, not a prediction.")
    if do_ml:
        horizon = exec_cfg["max_hold"] // 2
        cpu = bt.round_trip(c)
        rows = ml.build_dataset(bars, interval_sec=step, horizon=horizon, stride=2 if not quick else 4, cost_per_unit=cpu, event_times=ev_high)
        evl = ml.evaluate_rows(rows, horizon, seed=seed, gbm=dict(n_estimators=12 if quick else 40))
        body = ml.package(evl, rows, symbol, interval, dict(horizon=horizon, cost_per_unit=cpu), seed)
        path = ml.save_model(body, model_dir)
        out["ml"] = dict(decision=evl["decision"], id=body["id"], path=os.path.basename(path), counts=body["counts"], baselines=evl.get("baselines"),
                         metrics=body["metrics"], bins=body.get("bins"), features=ml.FEATURES)
    out["seconds"] = round(time.time() - t0, 1)
    out = _clean(out)
    if out_path != "":
        save_validation(out, out_path)
    return out


def slim(rec, max_points=300):
    """The same record, light enough for the web page: long equity curves are thinned (the first and last points are
    kept) and the per-parameter grid table is dropped. The full record stays in trader/validation.json."""
    out = json.loads(json.dumps(rec))
    for s in out.get("strategies", {}).values():
        eq = s.get("equity") or []
        if len(eq) > max_points:
            step = (len(eq) - 1) / float(max_points - 1)
            s["equity"] = [eq[int(round(i * step))] for i in range(max_points)]
        s.pop("grid_oos", None)
    return out


def render(rec):
    L = ["RESEARCH  %s %s   %s" % (rec["symbol"], rec["interval"], rec["created"]),
         "Data: %s, %d bars %s -> %s, quality %s, sha %s" % (rec["data"]["source"], rec["data"]["rows"], rec["data"]["first"], rec["data"]["last"],
                                                           rec["data"]["quality"], rec["data"]["sha256"][:12]),
         "Costs per unit: spread %.2f, slippage %.2f each side (%s)" % (rec["costs"]["spread"], rec["costs"]["slippage"], rec["costs"]["source"]),
         "Out-of-sample = walk-forward test windows only; holdout = last %d%% of bars, looked at once." % int(rec["walk_forward"]["holdout_frac"] * 100), ""]
    for name, s in rec["strategies"].items():
        o = s["oos"]
        L.append("%-20s %s" % (name, s["status"]))
        if o.get("n"):
            L.append("   OOS: %d trades, win %.0f%%, expectancy %+.2fR (95%% band %s), PF %s, max drawdown %.1fR" % (
                o["n"], o["win_rate"] * 100, o["expectancy"], "n/a" if not o["ci95"][0] else "%+.2f to %+.2f" % tuple(o["ci95"]),
                "n/a" if not o["profit_factor"] else "%.2f" % o["profit_factor"], o["max_dd_r"]))
        else:
            L.append("   OOS: no trades")
        ho = (s["holdout"] or {}).get("summary") or {}
        if ho.get("n"):
            L.append("   Holdout: %d trades, expectancy %+.2fR" % (ho["n"], ho["expectancy"]))
        for r in s["reasons"][:4]:
            L.append("   - " + r)
    if "ml" in rec:
        m = rec["ml"]
        L += ["", "ML (target before stop): %s" % m["decision"]["status"]] + ["   - " + r for r in m["decision"]["reasons"][:4]]
    L += ["", "Nothing here is a forecast or advice. Not financial advice."]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Research runner (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--symbol", default="GC=F")
    r.add_argument("--interval", default="60m", choices=["1m", "5m", "15m", "30m", "60m", "1d"])
    r.add_argument("--source", default="yahoo", choices=["yahoo", "dukascopy", "oanda", "csv"])
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--spread", type=float, default=None, help="round-trip spread in price units")
    r.add_argument("--slippage", type=float, default=None, help="slippage per side in price units")
    r.add_argument("--commission", type=float, default=None, help="commission per unit, round trip")
    r.add_argument("--no-ml", action="store_true")
    r.add_argument("--quick", action="store_true")
    r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ov = dict(spread=a.spread, slippage=a.slippage, commission_per_unit=a.commission)
    rec = run(a.symbol, a.interval, a.source, a.seed, ov, not a.no_ml, a.quick)
    print(json.dumps(slim(rec)) if a.json else render(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
