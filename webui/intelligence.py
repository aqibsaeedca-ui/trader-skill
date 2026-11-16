#!/usr/bin/env python3
"""Gold (or Bitcoin) intelligence dashboard data. Standard library only. Read-only.

Assembles market overview, strategy assessments, portfolio risk, performance evidence and the journal into one JSON
document for the Gold intelligence page. The rule it enforces everywhere:

    NO TRADE unless  (1) the strategy passed its out-of-sample validation gate, on the SAME code now running,
                     (2) it has a confirmed trigger on the last CLOSED bar,
                     (3) the plan passes the portfolio risk limits after costs,
                     (4) if an accepted calibrated model exists, its probability clears its validated threshold.

A probability or an expected return is shown only when a validated model or strategy produced it. Otherwise the page
says why there is none. Nothing is invented, and nothing here places an order.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402
import backtester as bt  # noqa: E402
import journal as jr  # noqa: E402
import macro  # noqa: E402
import marketdata as md  # noqa: E402
import ml  # noqa: E402
import portfolio as pf  # noqa: E402
import regimes  # noqa: E402
import research  # noqa: E402
import risk  # noqa: E402
import strategies as st  # noqa: E402


def _dir(trend):
    return 1 if trend.endswith("bull") else -1 if trend.endswith("bear") else 0


def alignment(trends):
    """trends: {timeframe: trend label}. Aligned only if every known timeframe leans the same way."""
    d = {k: _dir(v) for k, v in trends.items() if v and v != "unknown"}
    if not d:
        return dict(label="unknown", by_tf=trends)
    vals = set(d.values())
    label = "aligned bullish" if vals == {1} else "aligned bearish" if vals == {-1} else "mixed"
    return dict(label=label, by_tf=trends, bull=sum(1 for v in d.values() if v == 1), bear=sum(1 for v in d.values() if v == -1),
                flat=sum(1 for v in d.values() if v == 0))


def _levels(A, price, atr, n=3):
    sup, res = A.nearest(price)
    f = lambda L: [dict(price=l["price"], source=l["source"], dist_atr=abs(l["price"] - price) / atr if atr else None) for l in L[:n]]
    return dict(support=f(sup), resistance=f(res))


def _structure(A, price, atr):
    n = A.i
    ev = lambda e: dict(type=e["type"], dir=e["dir"], level=e["level"], bars_ago=n - e["i"]) if e else None
    sweeps = [e for e in A.events if e["type"] == "sweep"][-3:]
    near = lambda lo, hi: atr and (lo - 3 * atr <= price <= hi + 3 * atr)
    return dict(bias=A.bias, last_bos=ev(A.last_bos), last_choch=ev(A.last_choch),
                sweeps=[dict(level=s["level"], source=s["source"], reversal=s["reversal"], bars_ago=n - s["i"], major=s.get("major", False)) for s in sweeps],
                fvg=[dict(dir=g["dir"], lo=g["lo"], hi=g["hi"], status=g["status"], bars_ago=n - g["i"]) for g in A.fvgs if near(g["lo"], g["hi"])][:4],
                order_blocks=[dict(dir=o["dir"], lo=o["lo"], hi=o["hi"], bars_ago=n - o["i"]) for o in A.obs if near(o["lo"], o["hi"])][:3],
                swing_high=A.swing_highs[-1]["price"] if A.swing_highs else None, swing_low=A.swing_lows[-1]["price"] if A.swing_lows else None)


def _risk_costs(c, spec):
    return risk.costs(spread=c["spread"], slippage=c["slippage"], commission_per_lot=c["commission_per_unit"] * spec["units_per_lot"])


def assess(name, sigs, A, price, ctx):
    """One strategy: its evidence, any candidate on the last closed bar, the gates, and the verdict."""
    rec = ctx["record"]
    srec = (rec or {}).get("strategies", {}).get(name)
    status = "NOT RUN" if not srec else srec["status"]
    stale = bool(rec and rec.get("code_version") != ctx["code_version"])
    if stale:
        status = "STALE: the code changed since this was validated (%s)" % status
    n_last = A.i
    cands = [s for s in sigs if s["decided_i"] == n_last]
    recent = [dict(side=s["side"], bars_ago=n_last - s["decided_i"], price=s["price"], id=s["id"]) for s in sigs if 0 < n_last - s["decided_i"] <= 6]
    oos = (srec or {}).get("oos") or {}
    ho = ((srec or {}).get("holdout") or {}).get("summary") or {}
    out = dict(strategy=name, title=st.STRATEGIES[name].title, interval=ctx["interval"], evidence=dict(
        status=status, validated=status.startswith("VALIDATED"), oos_n=oos.get("n", 0), oos_expectancy=oos.get("expectancy"), oos_ci=oos.get("ci95"),
        holdout_n=ho.get("n", 0), holdout_expectancy=ho.get("expectancy"), reasons=(srec or {}).get("reasons", []),
        validated_on=(rec or {}).get("created"), params=(srec or {}).get("chosen")), candidate=None, recent=recent, blocking=[], info=[])
    blocking, info = out["blocking"], out["info"]
    if not out["evidence"]["validated"]:
        blocking.append("strategy not validated: %s" % status)
    if not cands:
        blocking.append("no confirmed trigger on the last closed bar")
    else:
        s = cands[0]
        cpu = bt.round_trip(ctx["costs"])
        cand = dict(id=s["id"], side=s["side"], entry=s["price"], stop=s["stop"], targets=s["targets"], note=s["note"], invalidation=s["invalidation"],
                    kind=s["kind"], regime=s["regime"], cost_per_unit=cpu, risk_per_unit=s["risk"])
        try:
            rr = risk.net_reward_risk(s["side"], s["price"], s["stop"], s["targets"][0], cpu)
            cand["rr_gross"], cand["rr_net"] = rr["gross"], rr["net"]
            ev = risk.evaluate_new_trade(ctx["portfolio"]["equity"], [p for p in ctx["portfolio"]["positions"] if p["symbol"] == ctx["symbol"].upper()],
                                         price, s["side"], s["price"], s["stop"], s["targets"][0], ctx["spec"], _risk_costs(ctx["costs"], ctx["spec"]),
                                         ctx["risk_pct"], ctx["portfolio"]["limits"]["max_open_risk_pct"], ctx["portfolio"]["limits"]["daily_loss_limit_pct"],
                                         ctx["portfolio"]["realised_today"], ctx["portfolio"]["limits"]["max_positions"], ctx["min_net_rr"])
            cand["sizing"] = dict(lots=ev["size"]["lots"], units=ev["size"]["units"], fits=ev["size"]["fits"], risk_at_stop=ev["size"]["risk_at_stop"],
                                  risk_at_min_lot_pct=ev["size"]["risk_at_min_lot_pct"], notional=ev["size"]["notional"])
            blocking.extend(ev["reasons"])
        except risk.RiskError as e:
            blocking.append("plan is not valid: %s" % e)
        model = ctx["model"]
        if model:
            try:
                x = ml.feature_row(A, n_last, 1 if s["side"] == "long" else -1, ctx["event_times"])
                p = ml.predict(model, x)
                cand["probability"] = dict(p=p["p"], interval=p["interval"], bin_n=p["bin_n"], model_id=p["model_id"], threshold=model["threshold"],
                                           meaning="calibrated chance that the target is reached before the stop within the model's horizon")
                if model["threshold"] is not None and p["p"] < model["threshold"]:
                    blocking.append("model probability %.2f is below its validated threshold %.2f" % (p["p"], model["threshold"]))
            except (ValueError, KeyError, IndexError) as e:
                info.append("model could not score this setup: %s" % e)
        else:
            info.append("no calibrated probability: %s" % ctx["model_note"])
        cand["expected_r"] = out["evidence"]["oos_expectancy"] if out["evidence"]["validated"] else None
        if not out["evidence"]["validated"]:
            info.append("expected return is not shown because this strategy has not been validated")
        out["candidate"] = cand
    out["decision"] = "NO TRADE" if blocking else cands[0]["side"].upper()
    return out


def performance(rec):
    if not rec:
        return dict(available=False, message="No research run yet for this market and interval. Run Gold research.")
    out = dict(available=True, created=rec["created"], data=rec["data"], costs=rec["costs"], walk_forward=rec["walk_forward"], code_version=rec["code_version"],
               seconds=rec.get("seconds"), strategies={}, ml=rec.get("ml"))
    for name, s in rec["strategies"].items():
        regime_pf = {k: dict(n=v["n"], profit_factor=v["profit_factor"], expectancy=v["expectancy"]) for k, v in s["by_regime"].items()}
        out["strategies"][name] = dict(status=s["status"], reasons=s["reasons"], oos=s["oos"], holdout=(s["holdout"] or {}).get("summary"), equity=s["equity"],
                                       stress=s["stress"], by_regime=regime_pf, by_session=s["by_session"], by_side=s["by_side"], halves=s["halves"],
                                       rejections=s.get("rejections", {}), signals_total=s.get("signals_total"), trades_total=s.get("trades_total"),
                                       bootstrap=s["bootstrap"], dsr=s["dsr"], holm_p=s.get("holm_p"), news_filter=s.get("news_filter"))
    return out


def spread_analysis(spread):
    if not spread:
        return dict(available=False, note="This data has no bid/ask, so spread is not measured. Costs in the backtest are assumptions.")
    v = sorted(spread.values())
    return dict(available=True, n=len(v), median=v[len(v) // 2], p95=v[int(len(v) * 0.95)], worst=v[-1], note="Measured from stored bid and ask data.")


def build(symbol="GC=F", interval="60m", now=None, bars=None, validation=None, portfolio_state=None, events=None, model_dir=None,
          journal_entries=None, spread=None, risk_pct=1.0, min_net_rr=1.0):
    now = time.time() if now is None else now
    step = md.INTERVAL_SEC[interval]
    if bars is None:
        import gold_intraday as gi
        bars = {"15m": gi.get_bars(symbol, "15m", "30d", 60), "60m": gi.get_bars(symbol, "60m", "6mo", 300), "1d": gi.get_bars(symbol, "1d", "2y", 1800)}
    raw = bars[interval]
    closed, forming_dropped = md.completed(raw, step, now)
    if len(closed) < 120:
        raise SystemExit("error: only %d closed %s bars; need at least 120" % (len(closed), interval))
    price, atr_ref = raw[-1][3], None
    ev = macro.load_store() if events is None else events
    ev_high = [e["ts"] for e in ev if e["importance"] == "high"]
    validation = research.load_validation() if validation is None else validation
    rec = validation.get(research.key(symbol, interval))
    c, cost_src = (bt.costs(**{k: rec["costs"][k] for k in ("spread", "slippage", "commission_per_unit")}), rec["costs"].get("source", "")) if rec \
        else research.default_costs(symbol, spread)
    state = pf.validate(portfolio_state) if portfolio_state is not None else pf.load()
    spec = risk.contract(symbol)
    insts = []
    for name in st.STRATEGIES:
        chosen = (((rec or {}).get("strategies") or {}).get(name) or {}).get("chosen") or {}
        insts.append(st.make(name, **chosen))
        insts[-1].interval = interval
    htf_sec = research.HTF.get(interval)
    sigs, A = bt.generate(closed, insts, step, htf_sec, ev_high, bt.round_trip(c))
    snap = A.snaps[-1]
    atr = snap["atr"]
    H = regimes.HTF(closed, htf_sec, event_times=ev_high) if htf_sec else None
    h_snap = H.at(len(closed) - 1) if H else None
    trends = {interval: snap["trend"]}
    if h_snap:
        trends["%dh" % (htf_sec // 3600) if htf_sec < 86400 * 2 else "weekly"] = h_snap["trend"]
    if interval != "1d" and "1d" in bars:
        d_closed, _ = md.completed(bars["1d"], 86400, now)
        if len(d_closed) > 80:
            trends["1d"] = regimes.analyse(d_closed).snaps[-1]["trend"]
    model = ml.latest_accepted(symbol, interval, model_dir)
    if model and model.get("code_version") != ml.code_version():
        model = None
    model_note = "no research run yet" if not rec else (rec.get("ml") or {}).get("decision", {}).get("status", "no model") + (
        ": " + "; ".join(((rec.get("ml") or {}).get("decision", {}).get("reasons") or [])[:2]))
    ctx = dict(symbol=symbol, interval=interval, record=rec, code_version=bt.code_version(), costs=c, spec=spec, portfolio=state, risk_pct=risk_pct,
               min_net_rr=min_net_rr, model=model, model_note=model_note, event_times=ev_high)
    assessments = [assess(name, sigs[id(inst)], A, price, ctx) for name, inst in zip(st.STRATEGIES, insts)]
    live = [a for a in assessments if a["decision"] != "NO TRADE"]
    if live:
        top = live[0]
        decision = dict(action=top["decision"], strategy=top["strategy"], reasons=["all gates passed for %s" % top["title"]])
    else:
        why = []
        for a in assessments:
            for b in a["blocking"]:
                if b not in why:
                    why.append(b)
        decision = dict(action="NO TRADE", strategy=None, reasons=why[:8])
    quality = md.quality_report(symbol, [(interval, raw, interval)], now)
    up, recent = macro.window(ev, now, 72, 24, high_only=True)
    sup = _levels(A, price, atr)
    mon = pf.monitor(state, symbol, price, atr)
    n = len(raw)
    bid = ask = None
    if spread and raw[-1][0] in spread:
        bid, ask = price - spread[raw[-1][0]] / 2, price + spread[raw[-1][0]] / 2
    return dict(
        symbol=symbol, interval=interval, checked=md.utc_iso(now), sample_data=False, decision=decision,
        overview=dict(provider=quality["provider"], quote_time=md.utc_iso(raw[-1][0]), price=price, bid=bid, ask=ask,
                      spread=(spread or {}).get(raw[-1][0]), bid_ask_note=("derived from the last bar's measured spread" if bid else "not available from this data source"),
                      last_closed_bar=md.utc_iso(closed[-1][0]), forming_bar_excluded=forming_dropped, regime=snap["label"], trend=snap["trend"], volatility=snap["vol"],
                      adx=snap["adx"], atr=atr, session=snap["session"], alignment=alignment(trends), bias=snap["bias"],
                      levels=sup, structure=_structure(A, price, atr), data_quality=quality, events=dict(upcoming=up, recent=recent, have=len(ev))),
        assessments=assessments, risk_monitor=mon, performance=performance(rec),
        spread_analysis=spread_analysis(spread),
        journal=dict(summary=jr.analyse(jr.load() if journal_entries is None else journal_entries),
                     recent=jr.recent(jr.load() if journal_entries is None else journal_entries, 10)),
        evidence=dict(costs_source=cost_src, model=(dict(id=model["id"], created=model["created"]) if model else None), model_note=model_note,
                      code_version=ctx["code_version"], validation_found=bool(rec)),
        notes=["Statuses describe out-of-sample evidence on past data. They are not predictions.",
               "Nothing is shown as profitable unless its independent out-of-sample results support it, and nothing here places orders.",
               "Prices are delayed last-trade bars unless a bid/ask source is stored."])


def render(d):
    L = ["%s %s  checked %s" % (d["symbol"], d["interval"], d["checked"]), "DECISION: %s" % d["decision"]["action"]]
    L += ["  - " + r for r in d["decision"]["reasons"]]
    o = d["overview"]
    L += ["", "Price %.2f at %s | regime %s | trend %s | alignment %s" % (o["price"], o["quote_time"], o["regime"], o["trend"], o["alignment"]["label"])]
    for a in d["assessments"]:
        L.append("%-24s %-8s evidence: %s" % (a["title"], a["decision"], a["evidence"]["status"]))
    L += ["", "Risk monitor: %s, open risk %.2f of %.2f budget" % (d["risk_monitor"]["status"], d["risk_monitor"]["open_risk"], d["risk_monitor"]["risk_budget"]), ""]
    return "\n".join(L + d["notes"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gold intelligence dashboard data (read-only).")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--interval", default="60m", choices=["15m", "60m"])
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not 0 < a.risk_pct <= 100:
        raise SystemExit("error: --risk-pct must be between 0 and 100")
    d = build(a.symbol, a.interval, risk_pct=a.risk_pct)
    print(json.dumps(research._clean(d)) if a.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
