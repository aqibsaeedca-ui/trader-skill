"""Pending-order alerts for the Gold watcher. Standard library only. Never places, changes or cancels anything.

evaluate() reads one Pending Order Intelligence snapshot (pending.build) plus the paper-order state changes since the last check
and returns the NEW alerts. It reuses watch_extras' gate, so every alert kind is deduplicated per cause, has a cooldown, and counts
toward the same hourly cap.

Kinds
  pending_valid       an order type has a confirmed setup AND passed validation AND every gate is clear
  pending_near        price is close to the trigger or limit of a validated plan or of a paper order that is waiting
  pending_invalid     a setup we alerted on has since traded through its invalidation price
  pending_state       a paper order changed state (triggered, filled, partially filled, cancelled, expired, invalidated)
  pending_spread      the measured spread is far above its recent median
  pending_news        important news is close while a plan exists
  pending_risk        the aggregate planned risk limit or the daily loss lockout is reached
  pending_conflict    a plan would hedge or oppose an open position
  pending_stale       a plan or paper order has outlived its expiry
Research-signal state and broker-order state stay separate: these alerts speak about plans and PAPER orders only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch_extras as wx  # noqa: E402

NEAR_ATR = 0.5
STATE_TITLE = {"TRIGGERED": "triggered", "FILLED": "filled", "PARTIALLY_FILLED": "partially filled", "CANCELLED": "cancelled",
               "EXPIRED": "expired", "INVALIDATED": "invalidated", "REJECTED": "rejected"}


def evaluate(d, settings, state, now, changes=(), live_paper=()):
    """d: pending.build() output. changes: audit entries from paper.update_all. live_paper: paper orders still waiting."""
    cfg = dict(wx.DEFAULTS, **{k: settings[k] for k in wx.DEFAULTS if k in settings})
    g = wx._Gate(state, cfg, now)
    mem = state.setdefault("pending", {})
    live = mem.setdefault("live", {})
    price, atr = d["market"]["price"], d["market"]["atr"] or 0.0
    sym = d["symbol"]

    for name, p in d["panels"].items():
        plan = p["plan"]
        if not plan:
            continue
        sid, typ = plan["signal_id"], plan["order_type"]
        if p["status"] == "RECOMMENDED":
            if g.fire("pend:valid:" + sid, "pending_valid", "%s: validated pending order available" % typ,
                      "%s %s at %.2f, stop %.2f, first target %.2f, size %.2f lots, risk %.2f, net reward to risk %.2f. Evidence: %s. A paper order is available on the dashboard; nothing is sent to a broker."
                      % (typ, plan["direction"], plan["entry"], plan["stop"], plan["tp1"], plan["lots"], plan["risk_currency"], plan["rr_net"], p["evidence"]["status"]),
                      price, once=True, signal_id=sid, order_type=typ):
                live[sid] = dict(order_type=typ, side=plan["direction"].lower(), invalid=plan["invalid_price"], at=now)
            if abs(price - plan["entry"]) <= NEAR_ATR * atr:
                g.fire("pend:near:" + sid, "pending_near", "%s trigger is near" % typ,
                       "Price %.2f is within %.1f ATR of the %s at %.2f." % (price, NEAR_ATR, typ, plan["entry"]), price, signal_id=sid, order_type=typ)
        if plan.get("stale"):
            g.fire("pend:stale:" + sid, "pending_stale", "%s plan is stale" % typ, "The %s plan expired at its expiry time and should not be used." % typ, price, once=True, signal_id=sid)
        if any("important news is close" in b for b in p["blocking"]):
            g.fire("pend:news:%s:%d" % (sid, now // 3600), "pending_news", "News close while a %s plan exists" % typ,
                   next(b for b in p["blocking"] if "important news is close" in b) + ". Order creation is blocked until it passes.", price, signal_id=sid)
        if any("hedge" in x or "oppos" in x for x in d["account"]["risk"]["effects"].get(sid, [])):
            g.fire("pend:conflict:" + sid, "pending_conflict", "%s would conflict with an open position" % typ,
                   "; ".join(d["account"]["risk"]["effects"][sid]) + ". Review it against your open positions.", price, once=True, signal_id=sid)

    for sid, s in list(live.items()):
        beyond = price <= s["invalid"] if s["side"] == "long" else price >= s["invalid"]
        if beyond:
            g.fire("pend:invalid:" + sid, "pending_invalid", "%s setup invalidated" % s["order_type"],
                   "Price %.2f traded through the invalidation level %.2f. Any paper order for it is no longer valid." % (price, s["invalid"]), price, once=True, signal_id=sid)
            del live[sid]
        elif now - s["at"] > 48 * 3600:
            del live[sid]

    for o in live_paper:
        if abs(price - o["price"]) <= NEAR_ATR * atr and o["order_state"] == "AWAITING_TRIGGER":
            g.fire("pend:paper-near:" + o["id"], "pending_near", "Paper %s is close to its price" % o["order_type"],
                   "Price %.2f is within %.1f ATR of paper order %s at %.2f." % (price, NEAR_ATR, o["id"], o["price"]), price, order_id=o["id"])
        if o["order_state"] == "AWAITING_TRIGGER" and now > o["expires_at"]:
            g.fire("pend:paper-stale:" + o["id"], "pending_stale", "Paper %s has outlived its expiry" % o["order_type"],
                   "Paper order %s passed its expiry at %s; the next update will mark it expired." % (o["id"], wx._iso(o["expires_at"])), price, once=True, order_id=o["id"])

    for c in changes:
        if c["to_state"] in STATE_TITLE and c["to_state"] != c.get("from_state"):
            g.fire("pend:state:%s:%s" % (c["order_id"], c["to_state"]), "pending_state", "Paper %s %s" % (c["order_type"], STATE_TITLE[c["to_state"]]),
                   "%s (paper order %s). %s Simulation only; no broker order exists." % (c["order_type"], c["order_id"], c.get("note") or ""), price, once=True, order_id=c["order_id"])

    if d["market"]["spread_basis"].startswith("measured"):
        for p in d["panels"].values():
            hit = next((b for b in p["blocking"] if b.startswith("spread is")), None)
            if hit:
                g.fire("pend:spread", "pending_spread", "Spread is excessive", hit + ". Pending orders would fill at worse prices.", price)
                break
    rk = d["account"]["risk"]
    if rk["lockout"]:
        g.fire("pend:risk:lock:" + wx._day(now), "pending_risk", "Daily loss lockout", "Today's result is at or below your daily loss limit. No new pending order is allowed today.", price, once=True)
    elif any("would exceed" in r for r in rk["reasons"]):
        g.fire("pend:risk:agg:" + wx._day(now), "pending_risk", "Aggregate risk limit reached", next(r for r in rk["reasons"] if "would exceed" in r), price)
    return g.out
