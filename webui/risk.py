#!/usr/bin/env python3
"""Contract specifications, trading costs, position sizing, exposure and risk limits. Standard library only.

Pure functions: no network, no broker connection, no credentials, no orders. Everything is read-only
arithmetic that other parts of the console can call. A result of "NO TRADE" is a normal, expected answer.

Prices are in account-currency units per unit of the instrument (dollars per ounce, dollars per BTC).
Spread, slippage and commission are explicit inputs; nothing here assumes a broker's real numbers.
"""
import math

SIDES = ("long", "short")

# Starting specifications. Broker-specific values (contract size, lot limits) MUST be overridden with the
# broker's own numbers; `broker_specific` marks the ones that differ between brokers.
CONTRACTS = {
    "GC=F": dict(symbol="GC=F", kind="futures", unit="oz", units_per_lot=100.0, tick=0.10, min_lot=1.0, lot_step=1.0,
                 broker_specific=False),
    "MGC=F": dict(symbol="MGC=F", kind="futures", unit="oz", units_per_lot=10.0, tick=0.10, min_lot=1.0, lot_step=1.0,
                  broker_specific=False),
    "XAUUSD": dict(symbol="XAUUSD", kind="cfd", unit="oz", units_per_lot=100.0, tick=0.01, min_lot=0.01, lot_step=0.01,
                   broker_specific=True),
    "BTC-USD": dict(symbol="BTC-USD", kind="spot", unit="BTC", units_per_lot=1.0, tick=0.01, min_lot=0.0001,
                    lot_step=0.0001, broker_specific=True),
}


class RiskError(ValueError):
    """Invalid input. Raised instead of guessing."""


def _need(cond, msg):
    if not cond:
        raise RiskError(msg)


def contract(symbol, **overrides):
    """A copy of the specification, with broker overrides (units_per_lot, tick, min_lot, lot_step)."""
    key = symbol.upper()
    _need(key in CONTRACTS, "unknown symbol %r (add it to risk.CONTRACTS)" % symbol)
    c = dict(CONTRACTS[key])
    for k, v in overrides.items():
        _need(k in ("units_per_lot", "tick", "min_lot", "lot_step"), "cannot override %r" % k)
        _need(v is not None and v > 0, "%s must be positive" % k)
        c[k] = float(v)
    _need(c["min_lot"] >= c["lot_step"] - 1e-12 or c["min_lot"] > 0, "min_lot must be positive")
    return c


def costs(spread=0.0, slippage=0.0, commission_per_lot=0.0, swap_per_unit_day=0.0):
    """spread: full bid-ask spread in price units. slippage: per side, price units. commission_per_lot:
    round trip, account currency. swap_per_unit_day: financing per unit per day held (negative = credit)."""
    for name, v in (("spread", spread), ("slippage", slippage), ("commission_per_lot", commission_per_lot)):
        _need(v >= 0, "%s cannot be negative" % name)
    return dict(spread=float(spread), slippage=float(slippage), commission_per_lot=float(commission_per_lot),
                swap_per_unit_day=float(swap_per_unit_day))


def round_trip_cost_per_unit(c, spec, days_held=0.0):
    """What one unit costs to open and close: the spread, slippage on both sides, commission, financing."""
    c = c or costs()
    return (c["spread"] + 2 * c["slippage"] + c["commission_per_lot"] / spec["units_per_lot"]
            + c["swap_per_unit_day"] * days_held)


def _sgn(side):
    _need(side in SIDES, "side must be 'long' or 'short'")
    return 1 if side == "long" else -1


def _decimals(step):
    s = ("%.10f" % step).rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def position_size(equity, risk_pct, entry, stop, spec, c=None, leverage=None):
    """Size a trade so a stop-out loses about risk_pct of equity INCLUDING costs. Lots are rounded DOWN to
    the lot step. If even the minimum lot risks more than planned, lots is 0 and the trade does not fit."""
    _need(equity > 0, "equity must be positive")
    _need(0 < risk_pct <= 100, "risk_pct must be in (0, 100]")
    _need(entry > 0 and stop > 0, "entry and stop must be positive")
    _need(entry != stop, "entry and stop cannot be equal")
    per_unit = abs(entry - stop) + round_trip_cost_per_unit(c, spec)
    risk_usd = equity * risk_pct / 100.0
    lots_raw = risk_usd / per_unit / spec["units_per_lot"]
    step = spec["lot_step"]
    lots = math.floor(lots_raw / step + 1e-9) * step
    lots = round(lots, _decimals(step))
    fits = lots >= spec["min_lot"] - 1e-12
    if not fits:
        lots = 0.0
    units = lots * spec["units_per_lot"]
    min_units = spec["min_lot"] * spec["units_per_lot"]
    out = dict(lots=lots, units=units, fits=fits, risk_planned=risk_usd, risk_per_unit=per_unit,
               risk_at_stop=units * per_unit, notional=units * entry,
               risk_at_min_lot=min_units * per_unit, risk_at_min_lot_pct=min_units * per_unit / equity * 100.0,
               cost_per_unit=round_trip_cost_per_unit(c, spec),
               reason="" if fits else "the minimum lot (%g) would risk %.2f, more than the %.2f planned"
               % (spec["min_lot"], min_units * per_unit, risk_usd))
    if leverage:
        _need(leverage > 0, "leverage must be positive")
        out["margin"] = units * entry / leverage
    return out


def pnl(side, entry, exit_price, units, c=None, spec=None, days_held=0.0):
    """Profit and loss in account currency for a long or short, before and after costs."""
    sg = _sgn(side)
    gross = (exit_price - entry) * sg * units
    cost = units * round_trip_cost_per_unit(c, spec, days_held) if (c and spec) else 0.0
    return dict(gross=gross, cost=cost, net=gross - cost)


def net_reward_risk(side, entry, stop, target, cost_per_unit=0.0):
    """Reward-to-risk after costs: costs shrink the reward AND enlarge the risk."""
    sg = _sgn(side)
    risk = (entry - stop) * sg
    reward = (target - entry) * sg
    _need(risk > 0, "stop must be on the losing side of entry")
    _need(reward > 0, "target must be on the winning side of entry")
    return dict(gross=reward / risk, net=(reward - cost_per_unit) / (risk + cost_per_unit),
                risk=risk, reward=reward)


def r_multiple(side, entry, stop, exit_price, cost_per_unit=0.0):
    """Realised result in R: net profit per unit divided by the planned price risk per unit."""
    sg = _sgn(side)
    risk = (entry - stop) * sg
    _need(risk > 0, "stop must be on the losing side of entry")
    return ((exit_price - entry) * sg - cost_per_unit) / risk


def exposure(positions, price):
    """Net and gross exposure of open positions. position = dict(side, units, entry, stop=None, symbol=...).
    A long and a short are a hedge: they offset in net exposure but both still carry costs and risk."""
    longs = sum(p["units"] for p in positions if p["side"] == "long")
    shorts = sum(p["units"] for p in positions if p["side"] == "short")
    open_pnl = sum((price - p["entry"]) * _sgn(p["side"]) * p["units"] for p in positions)
    return dict(count=len(positions), long_units=longs, short_units=shorts, net_units=longs - shorts,
                gross_units=longs + shorts, hedged_units=min(longs, shorts), net_notional=(longs - shorts) * price,
                gross_notional=(longs + shorts) * price, open_pnl=open_pnl,
                unprotected=[i for i, p in enumerate(positions) if not p.get("stop")],
                hedged=bool(longs and shorts))


def risk_budget(equity, positions, price, max_open_risk_pct=2.0, daily_loss_limit_pct=3.0, realised_today=0.0,
                max_positions=None):
    """Remaining risk budget and lockouts. Open risk is the worst-case loss if every stop is hit, measured from
    each position's ENTRY (a stop already past break-even adds no risk). Open profit or loss is tracked
    separately in the daily figure. A position without a stop has unbounded risk and is flagged."""
    _need(equity > 0, "equity must be positive")
    ex = exposure(positions, price)
    open_risk = 0.0
    for p in positions:
        if p.get("stop"):
            open_risk += p["units"] * max(0.0, (p["entry"] - p["stop"]) * _sgn(p["side"]))
    budget = equity * max_open_risk_pct / 100.0
    limit = equity * daily_loss_limit_pct / 100.0
    day_pnl = realised_today + ex["open_pnl"]
    alerts = []
    if ex["unprotected"]:
        alerts.append("%d position(s) have no stop" % len(ex["unprotected"]))
    if ex["hedged"]:
        alerts.append("long and short positions are open at the same time")
    lockout = day_pnl <= -limit
    if lockout:
        alerts.append("daily loss limit reached (%.2f of %.2f)" % (-day_pnl, limit))
    if open_risk > budget:
        alerts.append("open risk %.2f exceeds the %.2f budget" % (open_risk, budget))
    if max_positions is not None and len(positions) >= max_positions:
        alerts.append("position count at the limit (%d)" % max_positions)
    return dict(exposure=ex, open_risk=open_risk, budget=budget, remaining=budget - open_risk, daily_pnl=day_pnl,
                daily_loss_limit=limit, lockout=lockout, alerts=alerts)


def evaluate_new_trade(equity, positions, price, side, entry, stop, target, spec, c=None, risk_pct=1.0,
                       max_open_risk_pct=2.0, daily_loss_limit_pct=3.0, realised_today=0.0, max_positions=None,
                       min_net_rr=1.0, allow_hedge=False):
    """TRADE or NO TRADE with every reason. Never places anything; it only answers whether the plan fits."""
    reasons = []
    rb = risk_budget(equity, positions, price, max_open_risk_pct, daily_loss_limit_pct, realised_today, max_positions)
    size = position_size(equity, risk_pct, entry, stop, spec, c)
    cpu = round_trip_cost_per_unit(c, spec)
    rr = net_reward_risk(side, entry, stop, target, cpu)
    if rb["lockout"]:
        reasons.append("daily loss limit reached")
    if rb["exposure"]["unprotected"]:
        reasons.append("an open position has no stop")
    if not allow_hedge and any(p["side"] != side for p in positions):
        reasons.append("it would hedge an open position in the opposite direction")
    if max_positions is not None and len(positions) >= max_positions:
        reasons.append("too many open positions")
    if not size["fits"]:
        reasons.append(size["reason"])
    elif size["risk_at_stop"] > rb["remaining"] + 1e-9:
        reasons.append("risk %.2f is more than the remaining budget %.2f" % (size["risk_at_stop"], rb["remaining"]))
    if rr["net"] < min_net_rr:
        reasons.append("reward-to-risk after costs is %.2f, below the %.2f minimum" % (rr["net"], min_net_rr))
    return dict(decision="NO TRADE" if reasons else "TRADE", reasons=reasons, size=size, net_rr=rr, budget=rb)
