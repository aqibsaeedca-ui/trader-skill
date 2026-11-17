"""Pending-order rules, execution plans and account-wide risk. Standard library only. Nothing here talks to a broker.

The four ordinary pending orders follow MetaTrader 5 semantics:

    BUY STOP    above the Ask, buys when price rises to the trigger (breakout continuation)
    SELL STOP   below the Bid, sells when price falls to the trigger (breakdown continuation)
    BUY LIMIT   below the Ask, buys when price falls to the limit (pullback entry)
    SELL LIMIT  above the Bid, sells when price rises to the limit (pullback entry)

BUY STOP LIMIT and SELL STOP LIMIT are different, two-price orders: a stop trigger first, then a limit order that is
placed only after the trigger and may never fill. They are modelled for validation only and stay OFF until the broker
profile says the account supports them.

A broker profile is data you enter (stop level, tick size, account mode, lot rules). Defaults are placeholders and the
profile says so until you mark it verified. Real MT5 values cannot be read here because MT5 is not connected.
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, "trader", "pending_settings.json")

ORDER_TYPES = ("BUY STOP", "SELL STOP", "BUY LIMIT", "SELL LIMIT")
ADVANCED_TYPES = ("BUY STOP LIMIT", "SELL STOP LIMIT")
SIDE = {"BUY STOP": "long", "SELL STOP": "short", "BUY LIMIT": "long", "SELL LIMIT": "short",
        "BUY STOP LIMIT": "long", "SELL STOP LIMIT": "short"}
KIND = {"BUY STOP": "stop", "SELL STOP": "stop", "BUY LIMIT": "limit", "SELL LIMIT": "limit"}
SPLIT = (0.5, 0.3, 0.2)

DEFAULT_PROFILE = dict(
    symbol="XAUUSD", digits=2, tick_size=0.01, point=0.01, stops_level_points=30, freeze_level_points=0,
    account_mode="hedging", supports_stop_limit=False, units_per_lot=100.0, min_lot=0.01, lot_step=0.01, max_lot=100.0,
    spread=0.30, slippage=0.10, commission_per_lot=0.0, verified=False)
DEFAULT_LIMITS = dict(risk_pct=0.5, max_aggregate_risk_pct=1.0, daily_loss_pct=1.5, min_net_rr=1.0, duplicate_pct=0.15,
                      max_distance_atr=2.0, require_oco_for_opposing=True)


class OrderError(ValueError):
    """Invalid settings or order input."""


def _num(v, name, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise OrderError("%s must be a number" % name)
    if x != x or x in (float("inf"), float("-inf")):
        raise OrderError("%s must be finite" % name)
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise OrderError("%s must be between %s and %s" % (name, lo, hi))
    return x


def clean_settings(raw):
    """Validated copy of {profile, limits}. Raises OrderError with a plain message."""
    raw = raw if isinstance(raw, dict) else {}
    p, lim = dict(DEFAULT_PROFILE), dict(DEFAULT_LIMITS)
    rp, rl = raw.get("profile") or {}, raw.get("limits") or {}
    for k, lo, hi in (("tick_size", 1e-6, 1000), ("point", 1e-6, 1000), ("units_per_lot", 1e-6, 1e7), ("min_lot", 1e-6, 1000),
                      ("lot_step", 1e-6, 1000), ("max_lot", 1e-6, 100000), ("spread", 0, 10000), ("slippage", 0, 10000),
                      ("commission_per_lot", 0, 100000)):
        if k in rp:
            p[k] = _num(rp[k], k, lo, hi)
    for k in ("digits", "stops_level_points", "freeze_level_points"):
        if k in rp:
            p[k] = int(_num(rp[k], k, 0, 100000))
    if "symbol" in rp:
        s = str(rp["symbol"])
        if not (1 <= len(s) <= 20) or not all(c.isalnum() or c in "=._-" for c in s):
            raise OrderError("symbol has unsupported characters")
        p["symbol"] = s.upper()
    if rp.get("account_mode", p["account_mode"]) not in ("hedging", "netting"):
        raise OrderError("account_mode must be hedging or netting")
    p["account_mode"] = rp.get("account_mode", p["account_mode"])
    for k in ("supports_stop_limit", "verified"):
        if k in rp:
            if not isinstance(rp[k], bool):
                raise OrderError("%s must be true or false" % k)
            p[k] = rp[k]
    if p["min_lot"] > p["max_lot"]:
        raise OrderError("min_lot cannot exceed max_lot")
    for k, lo, hi in (("risk_pct", 0.01, 5), ("max_aggregate_risk_pct", 0.01, 10), ("daily_loss_pct", 0.1, 20), ("min_net_rr", 0.1, 10),
                      ("duplicate_pct", 0.0, 5), ("max_distance_atr", 0.1, 20)):
        if k in rl:
            lim[k] = _num(rl[k], k, lo, hi)
    if "require_oco_for_opposing" in rl:
        if not isinstance(rl["require_oco_for_opposing"], bool):
            raise OrderError("require_oco_for_opposing must be true or false")
        lim["require_oco_for_opposing"] = rl["require_oco_for_opposing"]
    if lim["risk_pct"] > lim["max_aggregate_risk_pct"]:
        raise OrderError("risk per setup cannot exceed the aggregate risk limit")
    return dict(profile=p, limits=lim)


def load_settings(path=None):
    try:
        with open(path or SETTINGS, encoding="utf-8") as f:
            return clean_settings(json.load(f))
    except (OSError, ValueError):
        return clean_settings({})


def save_settings(raw, path=None):
    s = clean_settings(raw)
    p = path or SETTINGS
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, p)
    return s


def settings_saved(path=None):
    return os.path.exists(path or SETTINGS)


# ------------------------------------------------------------------ prices and rules

def round_to_tick(price, tick, mode="nearest"):
    """Snap to the broker's tick size. 'up' and 'down' keep an order on the safe side of the level it was built from."""
    q = price / tick
    r = {"up": math.ceil(q - 1e-9), "down": math.floor(q + 1e-9), "nearest": round(q)}[mode]
    dec = max(0, int(round(-math.log10(tick)))) if tick < 1 else 0
    return round(r * tick, dec + 2)


def min_distance(profile):
    return profile["stops_level_points"] * profile["point"]


def spec_of(profile):
    return dict(symbol=profile["symbol"], kind="cfd", unit="oz", units_per_lot=profile["units_per_lot"], tick=profile["tick_size"],
                min_lot=profile["min_lot"], lot_step=profile["lot_step"], broker_specific=True)


def on_tick(price, tick):
    return abs(price / tick - round(price / tick)) < 1e-6


def validate_order(profile, order, bid, ask):
    """Broker-rule check for one order. order = dict(type, price, stop, targets, lots, [stoplimit]). Returns (errors, notes).
    Errors mean the broker would reject it. Notes are things you should know (for example a frozen order)."""
    errors, notes = [], []
    t = order["type"]
    if t in ADVANCED_TYPES and not profile["supports_stop_limit"]:
        return ["%s is unavailable: the broker profile does not confirm stop-limit support" % t], notes
    if t not in ORDER_TYPES and t not in ADVANCED_TYPES:
        return ["unknown order type %s" % t], notes
    if bid <= 0 or ask <= 0 or ask < bid:
        return ["market quote is not valid (bid %s, ask %s)" % (bid, ask)], notes
    d, tick = min_distance(profile), profile["tick_size"]
    price, stop, side = order["price"], order["stop"], SIDE[t]
    if not on_tick(price, tick):
        errors.append("price %s is not a multiple of the tick size %s" % (price, tick))
    base = t.replace(" LIMIT", "") if t in ADVANCED_TYPES else t
    if base == "BUY STOP" and price < ask + d - 1e-9:
        errors.append("BUY STOP must be at least %.2f above the Ask (%.2f)" % (d, ask))
    elif base == "SELL STOP" and price > bid - d + 1e-9:
        errors.append("SELL STOP must be at least %.2f below the Bid (%.2f)" % (d, bid))
    elif base == "BUY LIMIT" and price > ask - d + 1e-9:
        errors.append("BUY LIMIT must be at least %.2f below the Ask (%.2f)" % (d, ask))
    elif base == "SELL LIMIT" and price < bid + d - 1e-9:
        errors.append("SELL LIMIT must be at least %.2f above the Bid (%.2f)" % (d, bid))
    if t in ADVANCED_TYPES:
        sl = order.get("stoplimit")
        if sl is None:
            errors.append("a stop-limit order needs a limit price as well as a trigger")
        else:
            ok = sl < price if side == "long" else sl > price
            if not ok or abs(price - sl) < d - 1e-9:
                errors.append("the limit price must sit beyond the trigger by at least the stop level, on the pullback side")
            notes.append("After the trigger a limit order is placed; it can stay unfilled if price does not come back to %.2f." % sl)
    if side == "long":
        if stop > price - d + 1e-9:
            errors.append("stop loss must be at least %.2f below the order price" % d)
    elif stop < price + d - 1e-9:
        errors.append("stop loss must be at least %.2f above the order price" % d)
    if not on_tick(stop, tick):
        errors.append("stop loss is not on the tick size")
    prev = None
    for k, tp in enumerate(order.get("targets") or [], 1):
        far = (tp - price) if side == "long" else (price - tp)
        if far < d - 1e-9:
            errors.append("TP%d must be at least %.2f beyond the order price on the profit side" % (k, d))
        if prev is not None and ((tp <= prev) if side == "long" else (tp >= prev)):
            errors.append("TP%d must be further than TP%d" % (k, k - 1))
        if not on_tick(tp, tick):
            errors.append("TP%d is not on the tick size" % k)
        prev = tp
    lots = order.get("lots")
    if lots is not None:
        if lots < profile["min_lot"] - 1e-9 or lots > profile["max_lot"] + 1e-9:
            errors.append("volume %.4f is outside the broker's %.4f to %.4f lots" % (lots, profile["min_lot"], profile["max_lot"]))
        elif abs(lots / profile["lot_step"] - round(lots / profile["lot_step"])) > 1e-6:
            errors.append("volume %.4f is not a multiple of the lot step %s" % (lots, profile["lot_step"]))
    fz = profile["freeze_level_points"] * profile["point"]
    market = ask if side == "long" else bid
    if fz and abs(price - market) <= fz:
        notes.append("the order is inside the freeze level (%.2f): it could not be changed or cancelled while it is this close" % fz)
    return errors, notes


# ------------------------------------------------------------------ execution plan

def target_plan(profile, lots, targets):
    """How to manage up to three targets on THIS account. One MT5 position has one take-profit, so extra targets need
    extra orders (hedging) or a client-side controller (netting)."""
    n = len(targets)
    step, mn = profile["lot_step"], profile["min_lot"]
    if n <= 1:
        return dict(mode="single_target", available=True, legs=[dict(target=targets[0] if targets else None, lots=lots)],
                    note="One take-profit on the whole position.")
    weights = [w / sum(SPLIT[:n]) for w in SPLIT[:n]]
    legs = [round(math.floor(lots * w / step + 1e-9) * step, 6) for w in weights]
    legs[0] = round(legs[0] + max(0.0, lots - sum(legs)), 6)               # rounding remainder goes to the first leg
    if profile["account_mode"] == "hedging" and all(x >= mn - 1e-9 for x in legs):
        return dict(mode="split_orders", available=True, legs=[dict(target=t, lots=x) for t, x in zip(targets, legs)],
                    note="%d separate orders, each with its own take-profit. Each one needs its own stop, and they fill together." % n)
    why = ("a netting account merges same-direction orders into one position with one take-profit" if profile["account_mode"] == "netting"
           else "the volume is too small to split into %d legs of at least the minimum lot (%s)" % (n, mn))
    return dict(mode="client_side_partial_exits", available=False, legs=[dict(target=t, lots=x) for t, x in zip(targets, weights)],
                note="Separate take-profits are NOT available on this account (%s). Partial exits would have to be closed by hand or by a "
                     "client-side controller you run and monitor; it is not simulated as broker-supported." % why)


def make_plan(sig, profile, limits, bid, ask, equity, now=None, bar_seconds=3600):
    """Complete execution plan for one pending-order signal. Never invents a number: sizing comes from risk.position_size
    after costs, and the plan carries every broker-rule error it would trigger."""
    now = time.time() if now is None else now
    tick = profile["tick_size"]
    side, typ = sig["side"], sig["meta"]["order_type"]
    sg = 1 if side == "long" else -1
    price = round_to_tick(sig["price"], tick, "up" if typ in ("BUY STOP", "SELL LIMIT") else "down")
    stop = round_to_tick(sig["stop"], tick, "down" if side == "long" else "up")
    tps = [round_to_tick(t, tick, "down" if side == "long" else "up") for t in sig["targets"][:3]]
    cpu = profile["spread"] + 2 * profile["slippage"] + profile["commission_per_lot"] / profile["units_per_lot"]
    spec = spec_of(profile)
    c = risk.costs(spread=profile["spread"], slippage=profile["slippage"], commission_per_lot=profile["commission_per_lot"])
    size = risk.position_size(equity, limits["risk_pct"], price, stop, spec, c)
    plan = dict(symbol=profile["symbol"], direction=side.upper(), order_type=typ, kind=KIND[typ], entry=price, stop=stop,
                tp1=tps[0] if len(tps) > 0 else None, tp2=tps[1] if len(tps) > 1 else None, tp3=tps[2] if len(tps) > 2 else None,
                lots=size["lots"], units=size["units"], fits=size["fits"], risk_currency=size["risk_at_stop"],
                risk_per_unit=size["risk_per_unit"], cost_per_unit=cpu, signal_ts=sig["decided_t"], strategy=sig["strategy"], signal_id=sig["id"],
                regime=sig["regime"], expires_bars=sig["expires"], expires_at=sig["decided_t"] + sig["expires"] * bar_seconds,
                invalidation=sig["invalidation"], invalid_price=sig["meta"].get("invalid_price", stop), note=sig["note"], atr=sig.get("atr"),
                distance_from_market=abs(price - (ask if side == "long" else bid)), meta=sig["meta"])
    if not size["fits"]:
        plan["size_problem"] = size["reason"]
    if plan["tp1"] is not None and ((tps[0] - price) * sg) > 0 and (price - stop) * sg > 0:
        rr = risk.net_reward_risk(side, price, stop, tps[0], cpu)
        plan.update(rr_gross=rr["gross"], rr_net=rr["net"], net_reward_currency=size["units"] * (rr["reward"] - cpu))
    plan["targets_plan"] = target_plan(profile, size["lots"], [t for t in tps if t is not None]) if size["fits"] else None
    errs, notes = validate_order(profile, dict(type=typ, price=price, stop=stop, targets=tps, lots=size["lots"] if size["fits"] else None), bid, ask)
    plan["broker_errors"], plan["broker_notes"] = errs, notes
    plan["stale"] = now > plan["expires_at"]
    return plan


# ------------------------------------------------------------------ account-wide risk

def _exposure_effect(positions, plan):
    """Plain-language effect of the plan on current directional exposure."""
    net = sum(p["units"] * (1 if p["side"] == "long" else -1) for p in positions)
    sg = 1 if plan["direction"] == "LONG" else -1
    if not positions:
        return "opens a new %s exposure" % plan["direction"].lower()
    if net == 0:
        return "creates an opposing hedge" if any(p["side"] != plan["direction"].lower() for p in positions) else "adds exposure"
    if sg * net > 0:
        return "increases current %s exposure" % ("long" if net > 0 else "short")
    return "reduces current exposure" if abs(plan["units"]) <= abs(net) else "reverses current exposure (opposing %s)" % plan["direction"].lower()


def account_risk(equity, equity_known, positions, resting, plans, limits, price, realised_today=0.0, oco_groups=None, atr=None, leverage=None):
    """Account-wide check of the plans together with what is already open and resting.

    positions / resting: dicts with side, units, entry or price, stop. plans: candidate plans. oco_groups: lists of plan
    signal IDs where at most one can fill (paper simulator only). Returns allowed, reasons, per-plan effects, scenarios."""
    reasons, effects = [], {}
    if not equity_known:
        reasons.append("account equity has not been entered; essential risk information is missing")
    if equity <= 0:
        reasons.append("account equity is not positive")
    budget = equity * limits["max_aggregate_risk_pct"] / 100.0
    rb = risk.risk_budget(max(equity, 1e-9), positions, price, limits["max_aggregate_risk_pct"], limits["daily_loss_pct"], realised_today, None)
    resting_risk = sum(r["units"] * abs(r["price"] - r["stop"]) for r in resting if r.get("stop"))
    unprotected = [r for r in resting if not r.get("stop")]
    if unprotected:
        reasons.append("%d resting order(s) have no stop loss, so their risk is unbounded" % len(unprotected))
    if rb["lockout"]:
        reasons.append("daily loss lockout: today's result is at or below -%.1f%% of equity" % limits["daily_loss_pct"])
    if rb["exposure"]["unprotected"]:
        reasons.append("%d open position(s) have no stop loss" % len(rb["exposure"]["unprotected"]))
    groups = [set(g) for g in (oco_groups or [])]
    seen, plan_risk, ordered = set(), 0.0, sorted(plans, key=lambda p: -p["risk_currency"])
    for g in groups:                                         # one-cancels-the-other: only the largest member can ever be live
        members = [p for p in plans if p["signal_id"] in g]
        if members:
            plan_risk += max(p["risk_currency"] for p in members)
            seen |= {p["signal_id"] for p in members}
    plan_risk += sum(p["risk_currency"] for p in ordered if p["signal_id"] not in seen)
    planned_total = rb["open_risk"] + resting_risk + plan_risk
    if planned_total > budget + 1e-9:
        reasons.append("planned open risk %.2f would exceed the %.2f aggregate limit (%.1f%% of equity)" % (planned_total, budget, limits["max_aggregate_risk_pct"]))
    for p in plans:
        e = []
        if p["risk_currency"] > equity * limits["risk_pct"] / 100.0 + 1e-6:
            e.append("risk %.2f is above the %.2f per-setup limit" % (p["risk_currency"], equity * limits["risk_pct"] / 100.0))
        e.append(_exposure_effect(positions, p))
        opp = [r for r in resting if r["side"] != p["direction"].lower()]
        if opp and limits["require_oco_for_opposing"] and not any(p["signal_id"] in g for g in groups):
            e.append("an opposing order is resting and they are not linked by OCO: both could fill")
        if leverage:
            e.append("margin about %.2f" % (p["units"] * p["entry"] / leverage))
        effects[p["signal_id"]] = e
    sides = {p["direction"] for p in plans} | {r["side"].upper() for r in resting}
    scen = []
    if len(sides) == 2:
        both = rb["open_risk"] + resting_risk + sum(p["risk_currency"] for p in plans)
        linked = all(any(p["signal_id"] in g for g in groups) for p in plans if p["direction"] != next(iter(sides)))
        scen.append(dict(label="bullish and bearish orders both trigger", worst_case_loss=both, covered_by_oco=bool(groups) and linked,
                         note="Independently placed MT5 orders do not cancel each other. A paper OCO link only exists in the simulator." if not groups else
                              "Linked by OCO in the paper simulator only; a live version needs a client-side controller or broker support."))
        if not (groups and linked) and limits["require_oco_for_opposing"]:
            reasons.append("bullish and bearish orders could both trigger and there is no OCO link (worst case %.2f)" % both)
    scen.append(dict(label="every resting order fills and every stop is hit", worst_case_loss=rb["open_risk"] + resting_risk + plan_risk))
    return dict(allowed=not reasons, reasons=reasons, effects=effects, open_risk=rb["open_risk"], resting_risk=resting_risk, plan_risk=plan_risk,
                planned_total=planned_total, budget=budget, remaining=budget - planned_total, lockout=rb["lockout"], daily_pnl=rb["daily_pnl"],
                scenarios=scen)


def duplicates(plan, others, price, tolerance_pct):
    """Existing resting or paper orders of the same type within tolerance of this price."""
    tol = price * tolerance_pct / 100.0
    return [o for o in others if o.get("order_type") == plan["order_type"] and abs(o["price"] - plan["entry"]) <= tol]
