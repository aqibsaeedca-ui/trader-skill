"""Paper pending orders: a simulator with a full lifecycle, OCO links and an audit log. Standard library only.

THESE ARE NOT BROKER ORDERS. Nothing here is sent anywhere. The simulator replays closed bars with exactly the fill rules
the backtest uses (a limit must be traded through, a stop fills at the worse of the open and the trigger plus slippage, a
stop is assumed first in an ambiguous bar, no target is credited on a limit's fill bar), so paper results mean the same
thing as the research results.

Three kinds of state are kept apart on purpose:
  research signal   CANDIDATE, VALIDATED, REJECTED          (a recommendation; stored nowhere here)
  paper order       AWAITING_TRIGGER, TRIGGERED, FILLED, PARTIALLY_FILLED, CANCELLED, EXPIRED, INVALIDATED, REJECTED
  paper position    OPEN, CLOSED
There is no broker-order state, because no broker is connected. PARTIALLY_FILLED exists for completeness; the bar
simulator never produces it, and the dashboard says so.

OCO (one cancels the other) exists only here. MetaTrader 5 does not link orders by itself; a live version would need broker
support or a client-side controller whose connectivity, latency and failure risks you accept. When two linked orders trigger on
the same bar the simulator keeps BOTH and flags the failure, because that is what a controller cannot prevent.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "paper_orders.json")
AUDIT = os.path.join(ROOT, "trader", "pending_audit.json")

ORDER_STATES = ("AWAITING_TRIGGER", "TRIGGERED", "FILLED", "PARTIALLY_FILLED", "CANCELLED", "EXPIRED", "INVALIDATED", "REJECTED")
SIGNAL_STATES = ("CANDIDATE", "VALIDATED", "REJECTED")
LIVE = ("AWAITING_TRIGGER", "TRIGGERED", "PARTIALLY_FILLED")
ID = re.compile(r"^po-[0-9a-f]{10}$")
TEXT = re.compile(r"^[^\x00-\x1f<>]{0,300}$")


class PaperError(ValueError):
    """Bad input to the paper simulator."""


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save(orders, path=None):
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(orders[-500:], f, indent=1)
    os.replace(tmp, p)


def audit_log(path=None):
    return load(path or AUDIT)


def _audit(events, path=None):
    if not events:
        return
    log = load(path or AUDIT)
    log.extend(events)
    save(log[-5000:], path or AUDIT)


def _move(order, to, ts, note, price=None, sink=None):
    """Record one state change on the order and in the audit sink. Idempotent: the same state twice is ignored."""
    if order["order_state"] == to and to != "FILLED":
        return False
    frm = order["order_state"]
    order["order_state"] = to
    entry = dict(ts=_iso(ts), recorded_at=_iso(time.time()), order_id=order["id"], signal_id=order["signal_id"], from_state=frm, to_state=to,
                 note=note, price=price)
    order["history"].append(entry)
    if sink is not None:
        sink.append(dict(entry, symbol=order["symbol"], order_type=order["order_type"], strategy=order["strategy"]))
    return True


def create(plan, bars, bar_seconds, now=None, blocking=(), oco_group=None, evidence_status="NOT RUN", interval="60m", others=(), sink=None):
    """Make one paper order from a plan. If anything blocks it, the order is stored as REJECTED with the reasons, so a refusal is
    on the record too. `bars` are the closed bars the plan was built from; the order's life starts at the last of them."""
    now = time.time() if now is None else now
    if not bars:
        raise PaperError("no closed bars to start the order from")
    if plan["order_type"] not in ("BUY STOP", "SELL STOP", "BUY LIMIT", "SELL LIMIT"):
        raise PaperError("only the four ordinary pending order types can be papered")
    start_t = bars[-1][0]
    reasons = list(blocking) + ["broker rule: " + e for e in plan.get("broker_errors", [])]
    if not plan.get("fits"):
        reasons.append("position size does not fit: %s" % plan.get("size_problem", "volume below the minimum lot"))
    if plan.get("stale"):
        reasons.append("the recommendation has already expired")
    dup = [o for o in others if o["order_state"] in LIVE and o["order_type"] == plan["order_type"] and o["symbol"] == plan["symbol"]
           and abs(o["price"] - plan["entry"]) <= 0.0015 * plan["entry"]]
    if dup:
        reasons.append("a duplicate %s is already waiting at %.2f" % (plan["order_type"], dup[0]["price"]))
    oid = "po-" + __import__("hashlib").sha1(("%s|%s|%d" % (plan["signal_id"], plan["order_type"], start_t)).encode()).hexdigest()[:10]
    o = dict(id=oid, created=_iso(now), created_ts=now, symbol=plan["symbol"], interval=interval, order_type=plan["order_type"], side=plan["direction"].lower(),
             kind=plan["kind"], price=plan["entry"], stop=plan["stop"], targets=[t for t in (plan.get("tp1"), plan.get("tp2"), plan.get("tp3")) if t is not None],
             lots=plan["lots"], units=plan["units"], risk_currency=plan["risk_currency"], expires_bars=plan["expires_bars"], start_t=start_t,
             expires_at=start_t + plan["expires_bars"] * bar_seconds, signal_id=plan["signal_id"], signal_ts=plan["signal_ts"], strategy=plan["strategy"],
             regime=plan["regime"], invalidation=plan["invalidation"], invalid_price=plan["invalid_price"], level=plan["meta"].get("level"),
             atr=plan.get("atr"), oco_group=oco_group, evidence_status=evidence_status, targets_plan=plan.get("targets_plan"), order_state="CANDIDATE",
             position_state=None, history=[], fill=None, exit=None, r_result=None, flags=[], reasons=reasons, last_checked_t=start_t)
    if reasons:
        _move(o, "REJECTED", now, "; ".join(reasons), sink=sink)
    else:
        _move(o, "AWAITING_TRIGGER", now, "paper order placed (simulation only; nothing was sent to a broker)", price=plan["entry"], sink=sink)
    return o


def _signal(o):
    return dict(id=o["id"], strategy=o["strategy"], side=o["side"], kind=o["kind"], price=o["price"], stop=o["stop"], targets=o["targets"], expires=o["expires_bars"],
                decided_i=None, decided_t=o["start_t"], regime=o["regime"], session="", atr=o.get("atr") or 0.0, risk=abs(o["price"] - o["stop"]), note="",
                invalidation="", meta={})


def _sweep_invalidates(o, events, start_i):
    """A stop order waits on a level. If that level was swept and reversed after the order was placed, the breakout idea is dead."""
    if o["kind"] != "stop" or o.get("level") is None:
        return None
    want = "bear" if o["side"] == "long" else "bull"
    tol = 0.5 * (o.get("atr") or 0.0) + 1e-9
    for e in events:
        if e["type"] == "sweep" and e["i"] > start_i and e["reversal"] == want and abs(e["level"] - o["level"]) <= tol:
            return e
    return None


def advance(o, bars, c, step, exec_cfg=None, events=(), sink=None, oco=None):
    """Replay the closed bars since the order was placed and move it through its lifecycle. Returns True if anything changed.
    oco = (time of the first fill in this order's OCO group, how many members filled then, id of that first member) or None."""
    if o["order_state"] not in LIVE and not (o["order_state"] == "FILLED" and o["position_state"] == "OPEN"):
        return False
    cfg = dict(bt.EXEC_DEFAULTS, **(exec_cfg or {}))
    idx = next((k for k, b in enumerate(bars) if b[0] == o["start_t"]), None)
    if idx is None:
        return False                                              # the start bar is not in this data: leave the order alone
    sig = _signal(o)
    sig["decided_i"] = idx
    n_after = len(bars) - 1 - idx
    changed = False
    sweep = _sweep_invalidates(o, events, idx) if o["order_state"] in LIVE else None
    trade, why = bt._simulate(bars, sig, c, cfg)
    sibling_first = bool(oco and oco[0] is not None and oco[2] != o["id"] and o["order_state"] in LIVE)
    if trade is None:
        if sibling_first and oco[0] <= o["expires_at"]:
            changed |= _move(o, "CANCELLED", oco[0], "OCO: linked order %s filled, so this one was cancelled" % oco[2], sink=sink)
        elif sweep is not None:
            changed |= _move(o, "INVALIDATED", sweep["t"], "liquidity sweep: the level at %.2f was swept and price reversed, so this is not a confirmed breakout" % o["level"], sink=sink)
        elif why.startswith("cancelled"):
            changed |= _move(o, "INVALIDATED", bars[-1][0], "closed beyond the structural stop %.2f before the order filled" % o["invalid_price"], sink=sink)
        elif why.startswith("not filled"):
            if n_after >= o["expires_bars"]:
                changed |= _move(o, "EXPIRED", o["expires_at"], "not filled within %d bars" % o["expires_bars"], sink=sink)
        elif n_after >= 1:
            changed |= _move(o, "CANCELLED", bars[-1][0], "could not be filled as planned: %s" % why, sink=sink)
        o["last_checked_t"] = bars[-1][0]
        return changed
    if sweep is not None and sweep["t"] <= trade["entry_t"] and o["order_state"] in LIVE:
        changed |= _move(o, "INVALIDATED", sweep["t"], "liquidity sweep reversed the level before the order filled", sink=sink)
        return changed
    if sibling_first and trade["entry_t"] > oco[0]:                 # a linked order filled earlier: this one would have been cancelled
        changed |= _move(o, "CANCELLED", oco[0], "OCO: linked order %s filled first, so this one was cancelled" % oco[2], sink=sink)
        return changed
    if o["order_state"] in LIVE:
        if o["kind"] == "stop":
            changed |= _move(o, "TRIGGERED", trade["entry_t"], "trigger %.2f reached; a live stop order would now become a market order" % o["price"], price=o["price"], sink=sink)
        changed |= _move(o, "FILLED", trade["entry_t"], "filled at %.2f%s" % (trade["entry"], " (slippage %+.2f against the trigger)" % trade["entry_slip"] if o["kind"] == "stop" else ""),
                         price=trade["entry"], sink=sink)
        o["fill"] = dict(price=trade["entry"], ts=_iso(trade["entry_t"]), t=trade["entry_t"], slip=trade["entry_slip"])
        o["position_state"] = "OPEN"
        changed = True
        if oco and oco[0] == trade["entry_t"] and oco[1] > 1 and "oco_failed" not in o["flags"]:
            o["flags"].append("oco_failed")                       # both triggered on one bar: nothing could have cancelled the other
            o["history"].append(dict(ts=_iso(trade["entry_t"]), recorded_at=_iso(time.time()), order_id=o["id"], signal_id=o["signal_id"], from_state="FILLED",
                                     to_state="FILLED", note="OCO FAILED: a linked order filled on the same bar, so both are open", price=trade["entry"]))
            if sink is not None:
                sink.append(dict(o["history"][-1], symbol=o["symbol"], order_type=o["order_type"], strategy=o["strategy"]))
    last = trade["exits"][-1]
    if last["how"] != "end of data" and o["position_state"] != "CLOSED":
        o["position_state"] = "CLOSED"
        o["exit"] = dict(price=last["price"], ts=_iso(trade["exit_t"]), t=trade["exit_t"], how=trade["how"], legs=trade["exits"])
        o["r_result"] = trade["r"]
        o["history"].append(dict(ts=_iso(trade["exit_t"]), recorded_at=_iso(time.time()), order_id=o["id"], signal_id=o["signal_id"], from_state="FILLED",
                                 to_state="FILLED", note="position closed: %s, result %+.2fR" % (trade["how"], trade["r"]), price=last["price"]))
        if sink is not None:
            sink.append(dict(o["history"][-1], symbol=o["symbol"], order_type=o["order_type"], strategy=o["strategy"]))
        changed = True
    o["last_checked_t"] = bars[-1][0]
    return changed


def oco_context(orders, bars, c, cfg):
    """For each OCO group: when its first member fills, how many fill on that bar, and who. Decided BEFORE any state is recorded, so a
    sibling that would have been cancelled is never logged as filled."""
    groups = {}
    for o in orders:
        g = o.get("oco_group")
        if not g or not (o["order_state"] in LIVE or o.get("fill")):
            continue
        t = o["fill"]["t"] if o.get("fill") else None
        if t is None:
            idx = next((k for k, b in enumerate(bars) if b[0] == o["start_t"]), None)
            if idx is not None:
                sig = _signal(o)
                sig["decided_i"] = idx
                tr, _ = bt._simulate(bars, sig, c, cfg)
                t = tr["entry_t"] if tr else None
        if t is not None:
            groups.setdefault(g, []).append((t, o["id"]))
    out = {}
    for g, fills in groups.items():
        first = min(t for t, _ in fills)
        out[g] = (first, sum(1 for t, _ in fills if t == first), min(i for t, i in fills if t == first))
    return out

def cancel(orders, order_id, reason="cancelled by you", now=None, sink=None):
    now = time.time() if now is None else now
    if not ID.match(order_id or ""):
        raise PaperError("not a paper order id")
    if not TEXT.match(reason or ""):
        raise PaperError("reason has unsupported characters")
    o = next((x for x in orders if x["id"] == order_id), None)
    if o is None:
        raise PaperError("no such paper order")
    if o["order_state"] not in LIVE:
        raise PaperError("only a waiting order can be cancelled (this one is %s)" % o["order_state"])
    _move(o, "CANCELLED", now, reason, sink=sink)
    return o


def update_all(orders, bars, c, step, exec_cfg=None, events=(), sink=None):
    """Advance every live paper order against the latest closed bars, honouring OCO links. Returns the orders that changed."""
    cfg = dict(bt.EXEC_DEFAULTS, **(exec_cfg or {}))
    ctx = oco_context(orders, bars, c, cfg)
    touched = []
    for o in orders:
        if advance(o, bars, c, step, exec_cfg, events, sink, ctx.get(o.get("oco_group"))):
            touched.append(o)
    return touched

def live_orders(orders):
    return [o for o in orders if o["order_state"] in LIVE]


def open_positions(orders):
    return [o for o in orders if o["position_state"] == "OPEN"]


def summary(orders):
    by = {}
    for o in orders:
        by[o["order_state"]] = by.get(o["order_state"], 0) + 1
    closed = [o for o in orders if o["position_state"] == "CLOSED" and o["r_result"] is not None]
    return dict(total=len(orders), by_state=by, waiting=len(live_orders(orders)), open=len(open_positions(orders)), closed=len(closed),
                total_r=sum(o["r_result"] for o in closed),
                note="Paper orders only. No broker order exists; nothing was sent anywhere.")
