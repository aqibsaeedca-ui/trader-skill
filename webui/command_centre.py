#!/usr/bin/env python3
"""Gold Trading Command Centre: one analysis, one decision. Standard library only. SIGNAL ONLY: nothing here places, changes or cancels an order.

It gathers everything first (data freshness, broker quote, session, news, structure, key levels, strategies, account) and only then asks the
decision engine for ONE answer: BUY, SELL, WAIT or NO TRADE. The decision is made by HARD GATES. A weighted score is never used to bypass a gate,
no probability is computed from indicator agreement, and a missing input blocks the plan instead of being replaced by a made-up value.

Honest scope of this first version:
  * MetaTrader 5 is NOT connected to this console (the MT5 work lives in a separate project). The executable quote is read from
    trader/broker_quote.json, which that project (or you, by typing a quote on the page) can write. No quote, or a stale quote, blocks every plan.
  * GC=F (delayed Yahoo futures) is the research feed for structure and levels. It is never used as an executable price.
  * Only strategies with evidence grade A from the pre-registered tests may become a plan. None has, so NO TRADE is the expected result today.
"""
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold_core  # noqa: E402
import gold_sessions as gs  # noqa: E402
import macro  # noqa: E402
import marketdata as md  # noqa: E402
import orders  # noqa: E402
import portfolio as pf  # noqa: E402
import risk  # noqa: E402
import scorecard as sc  # noqa: E402
import strategy_policy  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE = os.path.join(ROOT, "trader", "broker_quote.json")
PLANS = os.path.join(ROOT, "trader", "command_plans.json")
SYMBOL = "GC=F"
QUOTE_MAX_AGE = 30                      # seconds: an executable quote older than this is not executable
CC_LIMITS = dict(risk_pct=0.25, max_aggregate_risk_pct=1.0, daily_loss_pct=1.5)      # initial demo risk controls
MAX_SPREAD_FRAC_OF_RISK = 0.25
PKT = "Asia/Karachi"

LIBRARY = (
    dict(id="A", name="Raschke Holy Grail pullback", author="Linda Bradford Raschke and Larry Connors",
         original="ADX(14) above 30, first pullback to the 20-period EMA, stop-entry in the trend direction. Original rules are in their books, which I have not read; the author's website gives no rules.",
         adaptation="H1 trend, M15 pullback, M5 trigger (not built).", sessions="London and New York, tested separately", regime="strong trend",
         status="NOT IMPLEMENTED", evidence="Insufficient", note="No verified independent evidence; needs XAUUSD 1-minute data."),
    dict(id="B", name="Crabel opening-range breakout", author="Toby Crabel",
         original="Verified in a paid working draft: breakout at 0.8 times the ten-day range from the open, no stop, held to the next open, daily data across 84 futures markets.",
         adaptation="30-minute London and New York opening ranges with protective stops (not built).", sessions="London open, New York open", regime="volatility compression then expansion",
         status="NOT IMPLEMENTED", evidence="Insufficient", note="An independent 22-year XAUUSD test of 30-minute opening-range breakouts (12 variants) found 0 passing after costs."),
    dict(id="C", name="Turtle / Donchian breakout", author="Richard Dennis and William Eckhardt",
         original="20-day and 55-day channel breakouts with ATR-based sizing (rules page partly read).", adaptation="Daily benchmark first; M15 or M5 versions are new strategies.",
         sessions="daily", regime="trend", status="NOT RUN", evidence="Insufficient", note="The daily benchmark on 10 years of GC=F is feasible now but has not been run."),
    dict(id="D", name="Asian range mean reversion", author="no verified source", original="No documented original method was verified.",
         adaptation="Fade the boundaries of a completed, confirmed ranging Asian session; stop beyond invalidation (not built).", sessions="Asian", regime="low-volatility range",
         status="NOT IMPLEMENTED", evidence="Insufficient", note="Disabled when directional expansion begins."),
    dict(id="E", name="Liquidity sweep and reclaim (Turtle Soup style)", author="Linda Bradford Raschke and Larry Connors (original: 20-day extremes)",
         original="Fade a break of a prior 20-day extreme that quickly reclaims (book rules not read by me).", adaptation="Asian high or low swept at the London open, M5 reclaim (not built).",
         sessions="London", regime="reversal", status="NOT IMPLEMENTED", evidence="Insufficient",
         note="An independent 2-year broker-data test of fading the London sweep lost -0.13R to -0.16R per trade."),
    dict(id="F", name="VWAP trend continuation", author="gold VWAP/EMA preprint (unverified: the page was behind a bot check)", original="Not verified.",
         adaptation="Price versus session VWAP, H1 trend, EMA slope, confirmed pullback (not built).", sessions="all", regime="trend",
         status="NOT IMPLEMENTED", evidence="Insufficient", note="Needs volume; this data has none, so VWAP is unavailable."),
)


# ------------------------------------------------------------------ broker quote (written by the MT5 project or typed by you) ----------------------

def load_quote(now=None, path=None):
    """The executable quote and its status. Never fabricates one."""
    now = time.time() if now is None else now
    out = dict(status="missing", bid=None, ask=None, spread=None, source=None, time=None, age_sec=None, message="No broker quote is available. MetaTrader 5 is not connected to this console.")
    try:
        with open(path or QUOTE, encoding="utf-8") as f:
            q = json.load(f)
        bid, ask, ts = float(q["bid"]), float(q["ask"]), float(q["time"])
    except (OSError, ValueError, KeyError, TypeError):
        return out
    out.update(bid=bid, ask=ask, spread=ask - bid, source=str(q.get("source") or "unknown"), time=md.utc_iso(ts), age_sec=now - ts)
    if not (bid > 0 and ask > 0 and ask >= bid) or ts > now + 5:
        out.update(status="invalid", message="The saved quote is not valid (bid %s, ask %s)." % (bid, ask))
    elif now - ts > QUOTE_MAX_AGE:
        out.update(status="stale", message="The broker quote is %d seconds old (limit %d). It cannot be used to price an order." % (now - ts, QUOTE_MAX_AGE))
    else:
        out.update(status="ok", message="Quote is fresh.")
    return out


def save_quote(bid, ask, source="manual entry (typed by you, not a live feed)", now=None, path=None):
    now = time.time() if now is None else now
    try:
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        raise ValueError("bid and ask must be numbers")
    if not (bid > 0 and ask > 0) or bid != bid or ask != ask or ask < bid:
        raise ValueError("ask must be at or above bid, and both positive")
    if (ask - bid) / bid > 0.02:
        raise ValueError("a spread above 2% is not a plausible XAUUSD quote")
    p = path or QUOTE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(dict(bid=bid, ask=ask, time=now, source=str(source)[:80], symbol="XAUUSD"), f)
    os.replace(p + ".tmp", p)
    return load_quote(now, p)


# ------------------------------------------------------------------ pieces ----------------------------------------------------------------------

def news_panel(events, now, hours=48):
    """Scheduled events only. Forecast, previous and actual are shown only if the stored record has them; nothing is invented."""
    known = any(e["ts"] >= now for e in events or [])
    up, rc = macro.window(events or [], now, hours, 24)
    row = lambda e: dict(name=e["name"], type=e["type"], utc=e["utc"], minutes=round((e["ts"] - now) / 60.0), importance=e["importance"], forecast=e.get("forecast"),   # noqa: E731
                         previous=e.get("previous"), actual=e.get("actual"), source=e.get("source"), reliability=e.get("reliability"), updated=e.get("observed_at"),
                         relevance=("high: a US release that has moved gold" if e["importance"] == "high" else "medium"))
    ups = sorted(up, key=lambda e: e["ts"])
    nxt = next((e for e in ups if e["importance"] == "high"), None)
    cls = gs.classify(now, events)
    status = "UNKNOWN" if not known else ("BLOCKED" if cls["news_block"] else "CLEAR")
    return dict(status=status, known=known, block=cls["news_block"], next=row(nxt) if nxt else None, upcoming=[row(e) for e in ups[:8]], recent=[row(e) for e in sorted(rc, key=lambda e: -e["ts"])[:4]],
                buffers=dict(before_minutes=gs.PRE_NEWS_MIN, after_minutes=gs.POST_NEWS_MIN, basis="default settings; the best buffer has not been backtested"),
                message={"UNKNOWN": "NEWS STATUS UNKNOWN: no future events are stored, so conditions cannot be declared news-safe.",
                         "BLOCKED": "A high-impact event is inside the block window.", "CLEAR": "No high-impact event inside the block window."}[status],
                lbma=dict(am="10:30 London", pm="15:00 London"), headlines="Qualitative headlines are not part of this panel; see Gold news impact. A headline never sets a direction.")


def session_panel(bars, now, events):
    t = gs.tracker(bars, now, events)
    pkt = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(PKT))
    t["pakistan_time"] = pkt.strftime("%Y-%m-%d %H:%M")
    t["utc"] = t["now"]
    return t


def candidates(res):
    """Plans from the 15m and 1h analyses, with duplicates across timeframes merged (same side and kind, entry within half an hour-ATR)."""
    atr = res["setup"]["1h"]["atr"]
    allp = []
    for tf in ("15m", "1h"):
        sec = res["setup"].get(tf)
        for p in (sec or {}).get("plans", []):
            allp.append(dict(p, tf=tf))
    allp.sort(key=lambda p: (-p["score"], p["dist_atr"]))
    out, merged = [], 0
    for p in allp:
        if any(o["side"] == p["side"] and o["kind"] == p["kind"] and abs(o["entry"] - p["entry"]) <= 0.5 * atr for o in out):
            merged += 1
            continue
        out.append(p)
    return out, merged


def confirmation(p, bars15, now):
    """Is the entry actually confirmed on closed M15 bars? Pullback: the zone was touched within the last 3 bars and the last close is back beyond it.
    Breakout: the last closed bar closed beyond the entry level."""
    closed, _ = md.completed(bars15 or [], 900, now)
    if len(closed) < 4:
        return False, "no 15-minute history to confirm on"
    last = closed[-1]
    lo, hi = p["entry_zone"]
    long = p["side"] == "long"
    if p["kind"] == "breakout":
        ok = last[3] > p["entry"] if long else last[3] < p["entry"]
        return ok, ("the last closed M15 bar closed beyond %.2f" % p["entry"]) if ok else "no M15 bar has closed beyond %.2f yet" % p["entry"]
    touched = any(b[2] <= hi and b[1] >= lo for b in closed[-3:])
    ok = touched and ((last[3] > hi) if long else (last[3] < lo))
    if ok:
        return True, "zone touched and the last closed M15 bar closed back %s it" % ("above" if long else "below")
    return False, "waiting for the zone to be touched and an M15 bar to close back %s it" % ("above" if long else "below")


def evidence_for(kind, side):
    chk = strategy_policy.check(kind, side, strict=False)
    row = None
    try:
        import experiments2
        r = experiments2.load_result() or {}
        row = next((s for s in r.get("strategies", []) if s["kind"] == kind and s["side"] == side), None)
    except Exception:
        row = None
    out = dict(grade=chk["evidence_grade"], reason=chk["evidence_reason"], quality="Validated" if chk["evidence_grade"] == "A" else ("Limited" if chk["evidence_grade"] == "B" else "Insufficient"))
    if row:
        te = row["test"]
        out.update(test_trades=te.get("trades"), test_mean_r=te.get("mean_r"), band=te.get("band_corrected"), train_mean_r=row["train"].get("mean_r"))
    return out


def adapt_core(core, result):
    """One gold_core signal as a candidate for the decision engine. Distances (stop, targets) are kept as OFFSETS from the entry, because absolute GC=F levels are
    futures prices and are never copied onto XAUUSD."""
    if core is None:
        return None
    ref, stop = core["ref_close"], core["stop"]
    dist = abs(ref - stop)
    sg = 1 if core["side"] == "long" else -1
    edge = core.get("opp_edge")
    tp1 = abs(edge - ref) if (core["exit"] == "structure" and edge is not None) else 2.0 * dist
    h = next((x for x in (result or {}).get("hypotheses", []) if x["id"] == core["strategy"]), None)
    ev = dict(grade="A" if core["eligible"] else "C", quality="Validated" if core["eligible"] else "Insufficient",
              reason=("validated on the development window and confirmed on the holdout" if core["eligible"] else
                      ("%s on the development window: %s" % (h["decision"], "; ".join(h["reasons"])) if h else "no backtest result is saved yet")))
    if h and h["development"].get("trades"):
        d = h["development"]
        ev.update(test_trades=d["trades"], test_mean_r=d.get("mean_r"), band=d.get("band_corrected"))
    return dict(name="%s (%s)" % (core["name"], core["strategy"]), strategy=core["strategy"], family=core["family"], side=core["side"], kind="market", market=True, tf="1h", dist_atr=0.0,
                stop_distance=dist, tp1_distance=tp1, exit=core["exit"], atr=core["atr"], ref_close=ref, sign=sg, evidence=ev, entry=ref, stop=stop, entry_zone=[ref, ref],
                targets=[dict(price=ref + sg * tp1)], cancel="Cancel if the next H1 bar does not open in time or if an H1 close goes beyond the stop distance. The signal is valid for one bar only.")


def size_market(p, quote, equity, profile, limits):
    """A MARKET plan from the broker quote: entry at the executable ask (long) or bid (short), stop and target as offsets from it."""
    if quote["status"] != "ok":
        return None
    tick = profile["tick_size"]
    long = p["side"] == "long"
    sg = 1 if long else -1
    entry = quote["ask"] if long else quote["bid"]
    stop = orders.round_to_tick(entry - sg * p["stop_distance"], tick, "down" if long else "up")
    tp1 = orders.round_to_tick(entry + sg * p["tp1_distance"], tick, "down" if long else "up")
    c = risk.costs(spread=quote["spread"], slippage=profile["slippage"], commission_per_lot=profile["commission_per_lot"])
    spec = orders.spec_of(profile)
    cpu = risk.round_trip_cost_per_unit(c, spec)
    plan = dict(order_type="BUY MARKET" if long else "SELL MARKET", entry=entry, stop=stop, tp1=tp1, tp2=None, tp3=None, cost_per_unit=cpu, spread=quote["spread"])
    try:
        rr = risk.net_reward_risk(p["side"], entry, stop, tp1, cpu)
        plan.update(rr_net=rr["net"], rr_gross=rr["gross"])
    except ValueError as e:
        plan.update(rr_net=None, rr_problem=str(e))
    errs = []
    d = orders.min_distance(profile)
    if abs(entry - stop) < d:
        errs.append("stop is closer than the broker's minimum stop distance %.2f" % d)
    if abs(tp1 - entry) < d:
        errs.append("target is closer than the broker's minimum stop distance %.2f" % d)
    if equity and equity > 0:
        sz = risk.position_size(equity, limits["risk_pct"], entry, stop, spec, c)
        plan.update(lots=sz["lots"], units=sz["units"], fits=sz["fits"], max_loss=sz["risk_at_stop"], size_problem=sz["reason"])
    else:
        plan.update(lots=None, units=None, fits=False, max_loss=None, size_problem="account equity has not been entered")
    plan.update(broker_errors=errs, broker_notes=["Stop and target are distances from the executable entry; absolute GC=F structure levels are not copied to XAUUSD. Check the levels on the broker chart."])
    return plan


def size_plan(p, quote, equity, profile, limits):
    """Executable numbers from the broker quote and contract data. Returns None when they cannot be calculated reliably."""
    if quote["status"] != "ok":
        return None
    if p.get("market"):
        return size_market(p, quote, equity, profile, limits)
    tick = profile["tick_size"]
    long = p["side"] == "long"
    typ = ("BUY LIMIT" if long else "SELL LIMIT") if p["kind"] == "pullback" else ("BUY STOP" if long else "SELL STOP")
    entry = orders.round_to_tick(p["entry"], tick, "up" if typ in ("BUY STOP", "SELL LIMIT") else "down")
    stop = orders.round_to_tick(p["stop"], tick, "down" if long else "up")
    tps = [orders.round_to_tick(t["price"], tick, "down" if long else "up") for t in p["targets"][:3]]
    c = risk.costs(spread=quote["spread"], slippage=profile["slippage"], commission_per_lot=profile["commission_per_lot"])
    spec = orders.spec_of(profile)
    cpu = risk.round_trip_cost_per_unit(c, spec)
    plan = dict(order_type=typ, entry=entry, stop=stop, tp1=tps[0], tp2=tps[1] if len(tps) > 1 else None, tp3=tps[2] if len(tps) > 2 else None, cost_per_unit=cpu, spread=quote["spread"])
    try:
        rr = risk.net_reward_risk(p["side"], entry, stop, tps[0], cpu)
        plan.update(rr_net=rr["net"], rr_gross=rr["gross"])
    except ValueError as e:
        plan.update(rr_net=None, rr_problem=str(e))
    if equity and equity > 0:
        sz = risk.position_size(equity, limits["risk_pct"], entry, stop, spec, c)
        plan.update(lots=sz["lots"], units=sz["units"], fits=sz["fits"], max_loss=sz["risk_at_stop"], size_problem=sz["reason"])
    else:
        plan.update(lots=None, units=None, fits=False, max_loss=None, size_problem="account equity has not been entered")
    errs, notes = orders.validate_order(profile, dict(type=typ, price=entry, stop=stop, targets=tps, lots=plan["lots"] if plan.get("fits") else None), quote["bid"], quote["ask"])
    plan.update(broker_errors=errs, broker_notes=notes)
    return plan


# ------------------------------------------------------------------ the decision engine ---------------------------------------------------------

def _g(gid, name, status, detail, hard=True):
    return dict(id=gid, name=name, status=status, detail=detail, hard=hard)


def decide(c):
    """c: dict of already-gathered facts (see build). Returns decision, gates, reasons. Pure: no I/O, so every gate is unit-testable.
    Hard gates: data, broker quote, session, news, evidence, execution, account, duplicate. Soft gates: direction, candidate, confirmation."""
    g = []
    d = c["data"]
    g.append(_g("data", "Research data fresh", "pass" if d["ok"] else "fail", d["message"]))
    q = c["quote"]
    g.append(_g("quote", "Broker quote fresh", "pass" if q["status"] == "ok" else "fail", q["message"]))
    s = c["session"]
    g.append(_g("session", "Market open", "fail" if s["session"] == "closed" else "pass", "Session: %s." % s["label"]))
    n = c["news"]
    g.append(_g("news", "News risk", {"CLEAR": "pass", "BLOCKED": "fail", "UNKNOWN": "unknown"}[n["status"]], n["message"]))
    h4, h1 = c["h4"], c["h1"]
    conflict = h4 not in ("neutral", None) and h1 not in ("neutral", None) and h4 != h1
    g.append(_g("direction", "H4 and H1 direction agree", "wait" if conflict else "pass", "H4 %s, H1 %s." % (h4, h1) + (" They conflict." if conflict else ""), hard=False))
    cand = c.get("candidate")
    if cand is None:
        g.append(_g("candidate", "A Gold strategy has a signal on the latest closed bar", "wait", "No Gold strategy has a signal on the latest closed H1 bar.", hard=False))
        for gid, nm in (("evidence", "Strategy has positive validated net expectancy"), ("confirmation", "Entry confirmed on closed bars"), ("execution", "Spread, stop distance and net reward to risk"),
                        ("account", "Fits the account risk budget"), ("duplicate", "Not a duplicate")):
            g.append(_g(gid, nm, "skip", "No candidate to check.", hard=False))
    else:
        with_trend = h1 in ("neutral", None) or (cand["side"] == "long") == (h1 == "bullish")
        g.append(_g("candidate", "A Gold strategy has a signal on the latest closed bar", "pass" if with_trend else "wait",
                    "%s on the %s chart%s." % (cand["name"], cand["tf"], "" if with_trend else "; it trades against the H1 direction"), hard=False))
        ev = c["evidence"]
        g.append(_g("evidence", "Strategy has positive validated net expectancy", "pass" if ev["grade"] == "A" else "fail",
                    "Evidence grade %s: %s" % (ev["grade"], ev["reason"])))
        ok, why = c["confirmed"]
        g.append(_g("confirmation", "Entry confirmed on closed bars", "pass" if ok else "wait", why, hard=False))
        pl = c.get("plan")
        if pl is None:
            g.append(_g("execution", "Spread, stop distance and net reward to risk", "fail", "No executable numbers: the broker quote is unavailable."))
        else:
            probs = list(pl.get("broker_errors") or [])
            risk_dist = abs(pl["entry"] - pl["stop"])
            if pl["spread"] > MAX_SPREAD_FRAC_OF_RISK * risk_dist:
                probs.append("spread %.2f is more than %d%% of the stop distance %.2f" % (pl["spread"], MAX_SPREAD_FRAC_OF_RISK * 100, risk_dist))
            if pl.get("rr_net") is None or pl["rr_net"] < c["limits"]["min_net_rr"]:
                probs.append("net reward to risk after costs is %s, below %.2f" % ("n/a" if pl.get("rr_net") is None else "%.2f" % pl["rr_net"], c["limits"]["min_net_rr"]))
            if not pl.get("fits"):
                probs.append("position size does not fit: %s" % pl.get("size_problem"))
            g.append(_g("execution", "Spread, stop distance and net reward to risk", "fail" if probs else "pass", "; ".join(probs) if probs else "Within broker rules and cost limits."))
        a = c["account"]
        g.append(_g("account", "Fits the account risk budget", "pass" if a["allowed"] else "fail", "; ".join(a["reasons"]) if not a["allowed"] else "Inside the risk limits."))
        g.append(_g("duplicate", "Not a duplicate", "fail" if c.get("duplicate") else "pass", "An equivalent plan is already active." if c.get("duplicate") else "No equivalent plan is active."))
    hard_bad = [x for x in g if x["hard"] and x["status"] in ("fail", "unknown")]
    soft_bad = [x for x in g if not x["hard"] and x["status"] == "wait"]
    if hard_bad:
        decision = "NO TRADE"
    elif soft_bad:
        decision = "WAIT"
    elif cand is not None:
        decision = "BUY" if cand["side"] == "long" else "SELL"
    else:
        decision = "WAIT"
    blockers = hard_bad if decision == "NO TRADE" else soft_bad
    reasons = [("%s: %s" % (x["name"], x["detail"])) for x in blockers][:3]
    if decision in ("BUY", "SELL"):
        reasons = ["All hard gates passed.", "Evidence grade A for this strategy.", "Entry confirmed on closed bars."]
    return dict(decision=decision, gates=g, reasons=reasons, blocked_by=[x["id"] for x in blockers])


# ------------------------------------------------------------------ plan log -----------------------------------------------------------------------

def load_plans(path=None):
    try:
        with open(path or PLANS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save_plans(plans, path=None):
    p = path or PLANS
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(plans[-200:], f, indent=1)
    os.replace(p + ".tmp", p)


def refresh_states(plans, price, now):
    """Move recorded plans to INVALIDATED or EXPIRED. Never executes anything. Returns the plans that changed."""
    changed = []
    for p in plans:
        if p["status"] != "Awaiting confirmation" and p["status"] != "Confirmed":
            continue
        long = p["decision"] == "BUY"
        if now > p["expires_at"]:
            p["status"] = "Expired"
        elif (price <= p["stop"]) if long else (price >= p["stop"]):
            p["status"] = "Invalidated"
        else:
            continue
        p["updated"] = md.utc_iso(now)
        changed.append(p)
    return changed


def record_plan(plan, now, path=None):
    plans = load_plans(path)
    if any(p["status"] in ("Awaiting confirmation", "Confirmed") and p["decision"] == plan["decision"] and abs(p["entry"] - plan["entry"]) <= 0.15 for p in plans):
        return False
    plans.append(dict(plan, id="cc-%d" % int(now), created=md.utc_iso(now), updated=md.utc_iso(now)))
    save_plans(plans, path)
    return True


# ------------------------------------------------------------------ gathering and the page document -------------------------------------------------

def build(now=None, light=False, bars=None, res=None, events=None, quote=None, state=None, settings=None, plans_path=None, equity_known=None):
    """The whole Command Centre document. light=True returns only the cheap live sections (market, session, news, quote). Every argument can be
    injected, which is how the tests run without a network."""
    now = time.time() if now is None else now
    import gold_intraday as gi
    if bars is None:
        bars = {"15m": gi.get_bars(SYMBOL, "15m", "30d", 60), "60m": gi.get_bars(SYMBOL, "60m", "6mo", 300), "1d": gi.get_bars(SYMBOL, "1d", "2y", 1800),
                "1wk": gi.get_bars(SYMBOL, "1wk", "5y", 3600)}
    events = macro.load_store() if events is None else events
    quote = load_quote(now) if quote is None else quote
    h1c, _ = md.completed(bars["60m"], 3600, now)
    qual = md.quality_report(SYMBOL, [("60m", bars["60m"], "60m")], now)["series"][0]
    data_ok = not (qual["stale"] and qual["market_open"]) and len(h1c) >= 120
    data = dict(ok=data_ok, message=("Research bars are fresh." if data_ok else "Research bars are stale or too few (%s min old while the market should be open)." % ((qual["age_sec"] or 0) // 60)),
                source="GC=F gold futures via Yahoo (delayed, last-trade; research only, not executable)", last_bar=md.utc_iso(h1c[-1][0] + 3600) if h1c else None, bars_1h=len(h1c))
    sess = session_panel(bars["15m"] or bars["60m"], now, events)
    news = news_panel(events, now)
    daily, weekly = bars["1d"], bars["1wk"]
    price = (bars["15m"] or bars["60m"])[-1][3]
    pd_ = daily[-2] if len(daily) > 1 else daily[-1]
    pw = weekly[-2] if len(weekly) > 1 else weekly[-1]
    market = dict(quote=quote, research_price=price, research_source=data["source"], research_time=md.utc_iso((bars["15m"] or bars["60m"])[-1][0]),
                  daily_change=price - pd_[3], daily_change_pct=(price / pd_[3] - 1) * 100, day_high=daily[-1][1], day_low=daily[-1][2],
                  prev_day=dict(high=pd_[1], low=pd_[2]), prev_week=dict(high=pw[1], low=pw[2]),
                  executable_note="Bid and ask are the broker's executable prices when a fresh quote exists. The research price is never used for orders.")
    doc = dict(created=md.utc_iso(now), mode="SIGNAL ONLY", symbol=SYMBOL, data=data, market=market, session=sess, news=news)
    if light:
        doc["light"] = True
        return doc

    res = gi.analyse(SYMBOL) if res is None else res
    import regimes
    ev_high = [e["ts"] for e in events if e["importance"] == "high"]
    snap = regimes.analyse(h1c, event_times=ev_high).snaps[-1]
    s4, s1 = res["setup"]["4h"], res["setup"]["1h"]
    market.update(atr=s1["atr"], volatility=snap["vol"], h1_trend=snap["trend"], regime=snap["label"])
    sup, rs = gi.split_levels(res, only_key=True)
    lvl = lambda l, side: dict(price=l["mid"], zone=[l["lo"], l["hi"]], type=l["kind"], timeframes=l["tfs"], source="; ".join(l["signals"][:3]),   # noqa: E731
                               distance=abs(l["mid"] - res["price"]), distance_atr=abs(l["mid"] - res["price"]) / s1["atr"],
                               confirmation=("confirmed: %d timeframes agree" % len(l["tfs"])) if len(l["tfs"]) >= 2 else "single timeframe, weaker",
                               invalidation="a %s close %s %.2f" % ("1-hour", "below" if side == "support" else "above", (l["lo"] - 0.5 * s1["atr"]) if side == "support" else (l["hi"] + 0.5 * s1["atr"])), side=side)
    structure = dict(h4=s4["bias"], h1=s1["bias"], m15=res["setup"]["15m"]["bias"] if res["setup"].get("15m") else "unavailable", regime=snap["label"],
                     change_of_character_recent=bool(snap.get("last_choch") is not None and len(h1c) - 1 - snap["last_choch"] <= 12),
                     break_of_structure_recent=bool(snap.get("last_bos") is not None and len(h1c) - 1 - snap["last_bos"] <= 12), sweep=snap.get("sweep"),
                     swing_high=snap.get("swing_high"), swing_low=snap.get("swing_low"))
    ranked = sorted([lvl(l, "resistance") for l in rs[:3]] + [lvl(l, "support") for l in sup[:3]], key=lambda x: (-len(x["timeframes"]), x["distance"]))[:3]
    levels = dict(top=ranked, session_ranges=sess.get("previous_ranges"), current_session=sess.get("current"),
                  vwap=dict(value=None, reason="no reliable volume in this data, so session VWAP is unavailable"), prev_day=market["prev_day"], prev_week=market["prev_week"],
                  not_calculated=["fair-value gaps", "order blocks"])
    core_result = gold_core.load_result()
    core, core_info = gold_core.live_candidate(bars["60m"], now, core_result)        # at most ONE opportunity, from strategies A and B only
    cand = adapt_core(core, core_result)
    cfg = settings or orders.load_settings()
    profile, limits = cfg["profile"], dict(cfg["limits"], **CC_LIMITS)
    state = pf.load() if state is None else state
    known = pf.exists() if equity_known is None else equity_known
    plan = size_plan(cand, quote, state["equity"] if known else None, profile, limits) if cand else None
    ev = cand["evidence"] if cand else None
    confirmed = (True, "the signal bar is a closed H1 bar (validated version uses H4 and H1 only; M15 and M5 are context, not tested)") if cand else (False, "")
    account = dict(allowed=True, reasons=[])
    if cand:
        syms = {SYMBOL, profile["symbol"].upper()}
        positions = [p for p in state["positions"] if p["symbol"] in syms]
        resting = [dict(side=p["side"], units=p["units"], price=p["price"], stop=p.get("stop")) for p in state["pending"] if p["symbol"] in syms]
        if plan and plan.get("fits"):
            account = orders.account_risk(state["equity"], known, positions, resting,
                                          [dict(signal_id="cc", direction=cand["side"].upper(), units=plan["units"], entry=plan["entry"], stop=plan["stop"], risk_currency=plan["max_loss"], order_type=plan["order_type"])],
                                          limits, price, state["realised_today"], None, s1["atr"])
        else:
            account = dict(allowed=False, reasons=[plan["size_problem"] if plan else "executable numbers are unavailable without a fresh broker quote"], open_risk=None)
        if not known:
            account = dict(account, allowed=False, reasons=list(dict.fromkeys(account.get("reasons", []) + ["account equity has not been entered; essential risk information is missing"])))
    plans = load_plans(plans_path)
    dup = bool(cand and plan and any(p["status"] in ("Awaiting confirmation", "Confirmed") and p["decision"] == ("BUY" if cand["side"] == "long" else "SELL") and abs(p["entry"] - plan["entry"]) <= 0.15 for p in plans))
    facts = dict(data=data, quote=quote, session=sess["classification"], news=news, h4=s4["bias"], h1=s1["bias"], candidate=cand, evidence=ev, confirmed=confirmed, plan=plan, account=account, duplicate=dup, limits=limits)
    out = decide(facts)
    final = dict(decision=out["decision"], instrument=profile["symbol"], session=sess["classification"]["label"], mode="SIGNAL ONLY", reasons=out["reasons"], blocked_by=out["blocked_by"])
    if out["decision"] in ("BUY", "SELL"):
        sig_state = "Confirmed" if confirmed[0] else "Awaiting confirmation"
        stp = 3600                                         # a market signal on a closed H1 bar is valid for the next bar only
        final.update(strategy=cand["name"], entry_type=plan["order_type"], entry=plan["entry"], stop=plan["stop"], tp1=plan["tp1"], tp2=plan["tp2"], tp3=plan["tp3"],
                     rr_net=plan["rr_net"], lots=plan["lots"], max_loss=plan["max_loss"], signal_status=sig_state, expected_net_return=ev.get("test_mean_r"), evidence_quality=ev["quality"],
                     expires_at=md.utc_iso(now + stp), invalidation=cand["cancel"])
        record_plan(dict(decision=out["decision"], entry=plan["entry"], stop=plan["stop"], expires_at=now + stp, status=sig_state, strategy=final["strategy"]), now, plans_path)
    refresh_states(plans, price, now)
    if plans:
        save_plans(plans, plans_path)
    research = None
    if cand and out["decision"] not in ("BUY", "SELL"):
        research = dict(NOT_EXECUTABLE=True, name=cand["name"], timeframe=cand["tf"], side=cand["side"], reference_close=cand["ref_close"], stop_distance=cand["stop_distance"],
                        target_distance=cand["tp1_distance"], evidence=ev, note="Research signal from a Gold strategy on the latest closed H1 bar. It is NOT a trade plan: at least one gate failed. "
                        "Distances are measured on the GC=F research chart and are not XAUUSD prices.")
    fam = []
    for h in (core_result or {}).get("hypotheses", []):
        d = h["development"]
        fam.append(dict(id=h["id"], family=h["family"], name=h["name"], status=("ELIGIBLE" if h["eligible"] else h["decision"]), trades=d.get("trades"), mean_r=d.get("mean_r"),
                        band=d.get("band_corrected"), reasons=h["reasons"][:2]))
    strategies = dict(selected=(None if out["decision"] not in ("BUY", "SELL") else final["strategy"]), families=fam,
                      strategy_c=dict(name="VWAP/EMA regime continuation", status="RESEARCH ONLY", reason="needs volume, which this data lacks; it can never produce an executable signal"),
                      signal_now=(dict(name=cand["name"], side=cand["side"], eligible=cand["evidence"]["grade"] == "A") if cand else None), research_signals=len(core_info.get("research", [])),
                      backtest_saved=bool(core_result),
                      rejected_reason=("No Gold strategy has passed validation, so none may produce a plan." if not (core_result or {}).get("eligible") else ""))
    doc.update(structure=structure, levels=levels, strategies=strategies, final=final, gates=out["gates"], research_candidate=research, plans=plans[-10:][::-1],
               portfolio=dict(equity=state["equity"], equity_entered=known, positions=len(state["positions"]), pending=len(state["pending"]), realised_today=state["realised_today"], limits=limits, account=account),
               sources=dict(research="GC=F via Yahoo, delayed", executable="broker quote file (trader/broker_quote.json), written by the MT5 project or typed on the page", news="stored official calendar (trader/events)"),
               caution="Signal only. Nothing is ever executed from this page. Past results are not a promise. Not financial advice.")
    return doc


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Gold Trading Command Centre: one analysis, one decision (signal only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--light", action="store_true", help="only the cheap live sections")
    a = ap.parse_args(argv)
    doc = build(light=a.light)
    if a.json:
        print(json.dumps(doc))
    else:
        f = doc.get("final")
        print("decision:", f["decision"] if f else "(light)")
        for g in doc.get("gates", []):
            print("  %-12s %-8s %s" % (g["id"], g["status"], g["detail"][:90]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
