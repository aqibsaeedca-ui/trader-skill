#!/usr/bin/env python3
"""Pending Order Intelligence: BUY STOP, SELL STOP, BUY LIMIT, SELL LIMIT. Standard library only. Read-only.

Builds one document for the dashboard. Each of the four order types gets a complete plan (when one exists), its own
out-of-sample evidence, and every gate that applies. An order is only RECOMMENDED if

    its strategy passed out-of-sample validation on the code now running,
    the plan obeys the broker rules (stop level, tick size, lot step) against the current quote,
    the whole account stays inside its risk limits with this order working (equity must have been entered),
    no important news is close, data is fresh, and the order does not duplicate another.

Fill probability and the chance of reaching the target after a fill are separate numbers, each with an uncertainty band, and
only shown when there are enough historical orders. Zero recommendations is a normal result.
Nothing here places, changes or cancels an order. Paper orders are a simulation.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt  # noqa: E402
import macro  # noqa: E402
import marketdata as md  # noqa: E402
import orders  # noqa: E402
import paper  # noqa: E402
import pending_strategies as pst  # noqa: E402
import portfolio as pf  # noqa: E402
import regimes  # noqa: E402
import research  # noqa: E402
import strategies as st  # noqa: E402

NEWS_LEAD_SEC = 1800
SPREAD_MULTIPLE = 3.0
STATE_LABEL = {"RECOMMENDED": "Validated and clear to place as a paper order", "CANDIDATE": "Research candidate only", "NONE": "No setup right now"}


def _evidence(name, rec, code):
    srec = ((rec or {}).get("strategies") or {}).get(name)
    status = "NOT RUN" if not srec else srec["status"]
    stale = bool(rec and rec.get("code_version") != code)
    if stale:
        status = "STALE: the code changed since this was validated (%s)" % status
    o = (srec or {}).get("orders") or {}
    oos = (srec or {}).get("oos") or {}
    ho = ((srec or {}).get("holdout") or {}).get("summary") or {}
    enough = bool(o.get("enough"))
    return dict(status=status, validated=status.startswith("VALIDATED"), oos_n=oos.get("n", 0), expectancy=oos.get("expectancy"), ci=oos.get("ci95"),
                holdout_n=ho.get("n", 0), holdout_expectancy=ho.get("expectancy"), orders=o or None, enough=enough,
                fill_probability=dict(p=o.get("fill_rate"), ci=o.get("fill_ci"), n=o.get("submitted")) if enough else None,
                hit_probability=dict(p=o.get("target_hit_rate"), ci=o.get("target_hit_ci"), n=o.get("filled")) if enough else None,
                expected_per_submitted=o.get("expected_per_submitted") if enough else None,
                params=(srec or {}).get("chosen"), reasons=(srec or {}).get("reasons", []), validated_on=(rec or {}).get("created"),
                no_probability_reason=None if enough else "fewer than 30 submitted and 30 filled historical orders, so no probability is quoted")


def _alive(sig, closed, c, exec_cfg):
    """Is an order decided earlier still working? Replays the closed bars since: not yet filled, not cancelled, not expired."""
    s = dict(sig)
    trade, why = bt._simulate(closed, s, c, exec_cfg)
    after = len(closed) - 1 - sig["decided_i"]
    if trade is not None:
        return False, "it already triggered %d bar(s) after it was placed" % max(0, (trade["entry_i"] - sig["decided_i"]))
    if why.startswith("cancelled"):
        return False, "price closed beyond its structural stop"
    if why.startswith("not filled") and after >= sig["expires"]:
        return False, "it would have expired"
    return True, ""


def _timeframes(interval, closed, bars, H, now, ev_high, htf_sec):
    """Trend per chart from completed bars. M5 and M1 are listed as unavailable rather than guessed."""
    d = lambda trend: trend.replace("_", " ")
    rows = []
    last = len(closed) - 1
    hs = H.at(last) if H else None
    if hs:
        rows.append(dict(tf="H%d" % (htf_sec // 3600), trend=d(hs["trend"]), bias=hs["bias"], regime=hs["label"], source="built from the %s bars" % interval))
    base = regimes.analyse(closed, event_times=ev_high).snaps[-1]
    rows.append(dict(tf="H1" if interval == "60m" else "M15", trend=d(base["trend"]), bias=base["bias"], regime=base["label"], source="the %s chart this page is built on" % interval))
    if interval != "15m" and "15m" in bars:
        c15, _ = md.completed(bars["15m"], 900, now)
        if len(c15) > 120:
            s15 = regimes.analyse(c15, event_times=ev_high).snaps[-1]
            rows.append(dict(tf="M15", trend=d(s15["trend"]), bias=s15["bias"], regime=s15["label"], source="15m bars (about 30 days)"))
    for tf in ("M5", "M1"):
        rows.append(dict(tf=tf, trend="unavailable", bias="unavailable", regime="unavailable", source="not available from this data source"))
    return rows

def build(symbol="GC=F", interval="60m", now=None, bars=None, validation=None, portfolio_state=None, equity_entered=None, settings=None, events=None,
          paper_orders=None, spread=None, audit=None):
    now = time.time() if now is None else now
    cfg = settings or orders.load_settings()
    profile, limits = cfg["profile"], cfg["limits"]
    step = md.INTERVAL_SEC[interval]
    if bars is None:
        import gold_intraday as gi
        bars = {"15m": gi.get_bars(symbol, "15m", "30d", 60), "60m": gi.get_bars(symbol, "60m", "6mo", 300)}
    raw = bars[interval]
    closed, forming_dropped = md.completed(raw, step, now)
    if len(closed) < 120:
        raise SystemExit("error: only %d closed %s bars; need at least 120" % (len(closed), interval))
    ev = macro.load_store() if events is None else events
    ev_high = [e["ts"] for e in ev if e["importance"] == "high"]
    validation = research.load_validation() if validation is None else validation
    rec = validation.get(research.key(symbol, interval, "pending"))
    code = bt.code_version()
    c = bt.costs(spread=profile["spread"], slippage=profile["slippage"], commission_per_unit=profile["commission_per_lot"] / profile["units_per_lot"])
    exec_cfg = dict(bt.EXEC_DEFAULTS, max_hold=research.MAX_HOLD.get(interval, 48))
    saved_min = pst.DEFAULT_MIN_STOP
    pst.DEFAULT_MIN_STOP = orders.min_distance(profile)
    try:
        names = list(pst.PENDING)
        insts = []
        for n in names:
            chosen = (((rec or {}).get("strategies") or {}).get(n) or {}).get("chosen") or {}
            insts.append(st.make(n, **chosen))
        htf_sec = research.HTF.get(interval)
        sigs, A = bt.generate(closed, insts, step, htf_sec, ev_high, bt.round_trip(c), spread=c["spread"])
    finally:
        pst.DEFAULT_MIN_STOP = saved_min
    H = regimes.HTF(closed, htf_sec, event_times=ev_high) if htf_sec else None
    snap = A.snaps[-1]
    atr = snap["atr"]
    price = raw[-1][3]
    measured = bool(spread and raw[-1][0] in spread)
    sp = spread[raw[-1][0]] if measured else profile["spread"]
    bid, ask = price - sp / 2.0, price + sp / 2.0
    state = pf.validate(portfolio_state) if portfolio_state is not None else pf.load()
    known = pf.exists() if equity_entered is None else equity_entered
    syms = {symbol.upper(), profile["symbol"].upper()}
    positions = [p for p in state["positions"] if p["symbol"] in syms]
    paper_all = paper.load() if paper_orders is None else paper_orders
    paper_live = paper.live_orders(paper_all)
    resting = [dict(order_type=_otype(p["side"], p["kind"]), side=p["side"], units=p["units"], price=p["price"], stop=p.get("stop"), source="portfolio")
               for p in state["pending"] if p["symbol"] in syms]
    resting += [dict(order_type=o["order_type"], side=o["side"], units=o["units"], price=o["price"], stop=o["stop"], source="paper") for o in paper_live]
    quality = md.quality_report(symbol, [(interval, raw, interval)], now)
    q0 = quality["series"][0]
    soon = [e for e in ev if e["importance"] == "high" and 0 <= e["ts"] - now <= NEWS_LEAD_SEC]
    sp_series = sorted(spread.values()) if spread else []
    spread_bad = bool(measured and len(sp_series) >= 20 and sp > SPREAD_MULTIPLE * sp_series[len(sp_series) // 2])
    last_i = len(closed) - 1
    panels, plans = {}, {}
    for name, inst in zip(names, insts):
        typ = inst.order_type
        evid = _evidence(name, rec, code)
        live = None
        for s in sorted(sigs[id(inst)], key=lambda x: -x["decided_i"]):
            if last_i - s["decided_i"] > s["expires"]:
                break
            ok, _ = _alive(s, closed, c, exec_cfg)
            if ok:
                live = s
                break
        recent_refusals = [r["reason"] for r in inst.rejected if last_i - r["i"] <= 12]
        panels[name] = dict(order_type=typ, strategy=name, title=inst.title, side=inst.side, evidence=evid, sig=live, refusals=recent_refusals[-2:])
        if live:
            plans[name] = orders.make_plan(live, profile, limits, bid, ask, state["equity"], now, step)
    # account-wide risk, per plan and for all plans together; OCO only links a BUY STOP with a SELL STOP
    oco_ids = None
    if "buy_stop" in plans and "sell_stop" in plans:
        oco_ids = [plans["buy_stop"]["signal_id"], plans["sell_stop"]["signal_id"]]
    all_plans = [p for p in plans.values() if p["fits"]]
    together = orders.account_risk(state["equity"], known, positions, resting, all_plans, limits, price, state["realised_today"],
                                   [oco_ids] if oco_ids else None, atr)
    out_panels, rows = {}, []
    for name, pan in panels.items():
        plan = plans.get(name)
        blocking, info = [], []
        e = pan["evidence"]
        if not e["validated"]:
            blocking.append("strategy not validated: %s" % e["status"])
        if not plan:
            why = pan["refusals"][-1] if pan["refusals"] else "no confirmed level close enough to the market"
            info.append(why)
        else:
            if plan["broker_errors"]:
                blocking.extend("broker rule: " + x for x in plan["broker_errors"])
            if not plan["fits"]:
                blocking.append("position size does not fit: %s" % plan.get("size_problem"))
            if plan["rr_net"] < limits["min_net_rr"] if plan.get("rr_net") is not None else True:
                blocking.append("net reward to risk after costs is below %.2f" % limits["min_net_rr"])
            alone = orders.account_risk(state["equity"], known, positions, resting, [plan] if plan["fits"] else [], limits, price, state["realised_today"], None, atr)
            blocking.extend(r for r in alone["reasons"] if "no OCO link" not in r)
            dup = orders.duplicates(plan, resting, price, limits["duplicate_pct"])
            if dup:
                blocking.append("a duplicate %s is already working at %.2f" % (plan["order_type"], dup[0]["price"]))
            if soon:
                blocking.append("important news is close: %s in %d min" % (soon[0]["name"], max(1, int((soon[0]["ts"] - now) // 60))))
            if q0["stale"] and q0["market_open"]:
                blocking.append("market data is stale (%d min old while the market should be open)" % ((q0["age_sec"] or 0) // 60))
            if spread_bad:
                blocking.append("spread is %.1f times its recent median" % (sp / sp_series[len(sp_series) // 2]))
            if plan.get("meta", {}).get("liquidity_risk"):
                info.append("another level sits just beyond the trigger: a liquidity sweep and reversal is possible")
            if plan.get("targets_plan") and not plan["targets_plan"]["available"]:
                info.append(plan["targets_plan"]["note"])
            for note in plan["broker_notes"]:
                info.append(note)
            info.extend(alone["effects"].get(plan["signal_id"], []))
            if not spread:
                info.append("spread monitoring needs real bid/ask data; the quote here uses the profile's assumed spread")
        status = "NONE" if not plan else ("RECOMMENDED" if not blocking else "CANDIDATE")
        sig_state = "REJECTED" if (plan and any(b.startswith(("broker rule", "position size", "net reward")) for b in blocking)) else (
            "VALIDATED" if (plan and e["validated"]) else ("CANDIDATE" if plan else None))
        pan.update(plan=plan, blocking=blocking, info=info, status=status, status_text=STATE_LABEL[status], signal_state=sig_state)
        pan.pop("sig")
        out_panels[name] = pan
        ev_exp = e["expected_per_submitted"]
        rows.append(dict(order_type=pan["order_type"], status=status, entry=plan["entry"] if plan else None, stop=plan["stop"] if plan else None,
                         tp1=plan["tp1"] if plan else None, lots=plan["lots"] if plan else None, risk=plan["risk_currency"] if plan else None,
                         rr_net=plan.get("rr_net") if plan else None, fill=e["fill_probability"], hit=e["hit_probability"], ev=ev_exp, oos_n=e["oos_n"],
                         validation=e["status"], blocking=len(blocking)))
    rec_names = [n for n, p in out_panels.items() if p["status"] == "RECOMMENDED"]
    best = None
    if rec_names:
        ranked = sorted(rec_names, key=lambda n: -(out_panels[n]["evidence"]["expected_per_submitted"] if out_panels[n]["evidence"]["expected_per_submitted"] is not None else -9))
        top = ranked[0]
        if out_panels[top]["evidence"]["expected_per_submitted"] is not None:
            best = dict(strategy=top, order_type=out_panels[top]["order_type"], basis="highest validated expected result per submitted order (fill rate x average result after a fill)")
    stoplimit = dict(available=bool(profile["supports_stop_limit"]), types=list(orders.ADVANCED_TYPES),
                     note=("The profile says the account supports stop-limit orders. They are validated against broker rules only; they are not backtested, and after the trigger the limit order may never fill."
                           if profile["supports_stop_limit"] else
                           "BUY STOP LIMIT and SELL STOP LIMIT are unavailable: the broker profile does not confirm support, and MetaTrader 5 is not connected to check it. They are two-price orders and are never treated as ordinary stop or limit orders."))
    return dict(
        symbol=symbol, interval=interval, checked=md.utc_iso(now), sample_data=False, headline=("BEST" if best else "NONE"), best=best,
        recommended=rec_names, panels=out_panels, comparison=rows,
        market=dict(price=price, quote_time=md.utc_iso(raw[-1][0]), bid=bid, ask=ask, spread=sp, spread_basis=("measured from stored bid/ask" if measured else "assumed from the broker profile; no live bid/ask is connected"),
                    last_closed_bar=md.utc_iso(closed[-1][0]), forming_bar_excluded=forming_dropped, regime=snap["label"], trend=snap["trend"], volatility=snap["vol"], atr=atr,
                    session=snap["session"], timeframes=_timeframes(interval, closed, bars, H, now, ev_high, htf_sec), data_quality=quality,
                    unavailable_timeframes=["M5", "M1"], unavailable_note="M5 and M1 history is not available from this data source; execution timing on those charts is not assessed."),
        account=dict(equity=state["equity"], equity_entered=known, risk=together, positions=len(positions), resting=len(resting), realised_today=state["realised_today"],
                     note="Equity and positions are what you entered; MetaTrader 5 is not connected."),
        broker=dict(profile=profile, limits=limits, settings_saved=orders.settings_saved(), verified=profile["verified"],
                    note=("These are placeholder broker values until you save and verify your own." if not profile["verified"] else "Broker values marked verified by you.")),
        stop_limit=stoplimit, oco=dict(proposed=oco_ids, note="OCO exists only in the paper simulator. MetaTrader 5 does not cancel a sibling order by itself."),
        paper=dict(orders=paper_all[-30:][::-1], summary=paper.summary(paper_all), audit=(audit if audit is not None else paper.audit_log())[-15:][::-1],
                   broker_orders="none: no broker is connected, so no broker order exists"),
        evidence=dict(family="pending", validation_found=bool(rec), code_version=code, costs=dict(spread=profile["spread"], slippage=profile["slippage"],
                      commission_per_lot=profile["commission_per_lot"]), why_none={n: p["info"][:1] for n, p in out_panels.items() if p["status"] == "NONE"}),
        notes=["A recommendation needs out-of-sample validation of that order type on the current code. Until then a plan is a research candidate.",
               "Fill probability and probability after a fill are separate and shown only with enough historical orders.",
               "Nothing here places, changes or cancels an order."])


def _otype(side, kind):
    return {("long", "stop"): "BUY STOP", ("short", "stop"): "SELL STOP", ("long", "limit"): "BUY LIMIT", ("short", "limit"): "SELL LIMIT"}.get((side, kind), "BUY STOP LIMIT" if side == "long" else "SELL STOP LIMIT")


def create_paper(symbol, interval, order_type, acknowledge_unvalidated=False, oco=False, now=None, bars=None, settings=None, validation=None,
                 portfolio_state=None, equity_entered=None, events=None, orders_path=None, audit_path=None, spread=None):
    """Make a paper order (or a linked BUY STOP and SELL STOP pair) from a FRESH analysis. The plan is rebuilt here, never taken from the
    browser, so it cannot be forged. Hard blockers (broker rules, risk, news, stale data, duplicates) store the order as REJECTED, so the
    refusal is on the record. An unvalidated order type needs `acknowledge_unvalidated`: it is then a research paper order."""
    if order_type not in orders.ORDER_TYPES:
        raise paper.PaperError("only BUY STOP, SELL STOP, BUY LIMIT and SELL LIMIT can be papered")
    now = time.time() if now is None else now
    step = md.INTERVAL_SEC[interval]
    if bars is None:
        import gold_intraday as gi
        bars = {"15m": gi.get_bars(symbol, "15m", "30d", 60), "60m": gi.get_bars(symbol, "60m", "6mo", 300)}
    existing = paper.load(orders_path)
    d = build(symbol, interval, now, bars, validation, portfolio_state, equity_entered, settings, events, existing, spread, audit=[])
    closed, _ = md.completed(bars[interval], step, now)

    def panel_for(typ):
        return d["panels"][pst.BY_ORDER_TYPE[typ]]

    def one(typ, group):
        pan = panel_for(typ)
        plan = pan["plan"]
        if plan is None:
            raise paper.PaperError("no %s plan exists right now (%s)" % (typ, (pan["info"] or pan["refusals"] or ["no confirmed level close enough"])[0]))
        hard = [b for b in pan["blocking"] if not b.startswith("strategy not validated")]
        if not pan["evidence"]["validated"] and not acknowledge_unvalidated and not hard:
            raise paper.PaperError("%s has not passed out-of-sample validation. Confirm that you want a research paper order to simulate it anyway." % typ)
        return plan, hard, pan["evidence"]["status"]

    other = {"BUY STOP": "SELL STOP", "SELL STOP": "BUY STOP"}.get(order_type)
    if oco and other is None:
        raise paper.PaperError("OCO links a BUY STOP with a SELL STOP only")
    sink, made = [], []
    plan, hard, status = one(order_type, None)
    group = None
    if oco:
        plan2, hard2, status2 = one(other, None)
        if hard or hard2:
            raise paper.PaperError("an OCO pair needs both sides clear: " + "; ".join(hard + hard2))
        group = "oco-" + __import__("hashlib").sha1((plan["signal_id"] + plan2["signal_id"]).encode()).hexdigest()[:8]
        made.append(paper.create(plan2, closed, step, now, [], group, status2, interval, existing, sink))
    made.insert(0, paper.create(plan, closed, step, now, hard, group, status, interval, existing, sink))
    seen = {o['id'] for o in existing}
    if any(o['id'] in seen for o in made):
        raise paper.PaperError('this exact setup is already on record (same signal and bar); wait for a new bar or a new signal')
    existing.extend(made)
    paper.save(existing, orders_path)
    paper._audit(sink, audit_path)
    return made


def paper_view(orders_path=None, audit_path=None):
    allo = paper.load(orders_path)
    return dict(orders=allo[-30:][::-1], summary=paper.summary(allo), audit=paper.audit_log(audit_path)[-15:][::-1],
                broker_orders="none: no broker is connected, so no broker order exists")


def refresh_paper(symbol="GC=F", interval="60m", now=None, bars=None, settings=None, orders_path=None, audit_path=None, events=None):
    """Advance every live paper order against the latest closed bars and save the result and audit entries."""
    now = time.time() if now is None else now
    cfg = settings or orders.load_settings()
    profile = cfg["profile"]
    step = md.INTERVAL_SEC[interval]
    if bars is None:
        import gold_intraday as gi
        bars = {"60m": gi.get_bars(symbol, "60m", "6mo", 300), "15m": gi.get_bars(symbol, "15m", "30d", 60)}
    closed, _ = md.completed(bars[interval], step, now)
    allo = paper.load(orders_path)
    live = [o for o in allo if o["order_state"] in paper.LIVE or o["position_state"] == "OPEN"]
    if not live or len(closed) < 120:
        return dict(changed=[], orders=allo)
    c = bt.costs(spread=profile["spread"], slippage=profile["slippage"], commission_per_unit=profile["commission_per_lot"] / profile["units_per_lot"])
    ev = [] if events is None else events
    evs = regimes.analyse(closed, event_times=[e["ts"] for e in ev if e["importance"] == "high"], interval_sec=step).events
    sink = []
    touched = paper.update_all(allo, closed, c, step, dict(max_hold=research.MAX_HOLD.get(interval, 48)), evs, sink)
    if touched:
        paper.save(allo, orders_path)
        paper._audit(sink, audit_path)
    return dict(changed=[o["id"] for o in touched], orders=allo, audit=sink)


def render(d):
    L = ["PENDING ORDERS  %s %s  checked %s" % (d["symbol"], d["interval"], d["checked"])]
    L.append("NO VALID PENDING ORDER - WAIT FOR CONFIRMATION." if not d["recommended"] else "RECOMMENDED: " + ", ".join(d["panels"][n]["order_type"] for n in d["recommended"]))
    for n, p in d["panels"].items():
        L.append("  %-10s %-11s %s" % (p["order_type"], p["status"], "; ".join(p["blocking"][:2]) or "; ".join(p["info"][:1])))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pending Order Intelligence (read-only).")
    ap.add_argument("--symbol", default="GC=F", choices=["GC=F", "MGC=F"])
    ap.add_argument("--interval", default="60m", choices=["60m", "15m"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = build(a.symbol, a.interval)
    print(json.dumps(research._clean(d)) if a.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
