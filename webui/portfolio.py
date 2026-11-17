"""Read-only portfolio risk monitor. Standard library only.

Positions, pending orders and limits are typed in by you (or pasted from your platform) and stored in
trader/portfolio.json. Nothing here talks to a broker, and nothing places, changes or cancels an order. It only does
arithmetic on what you entered: exposure, remaining risk budget, loss-limit status, protection and conflict checks, and
what-if scenarios.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "portfolio.json")
SYMBOL = re.compile(r"^[A-Za-z0-9=._-]{1,20}$")
TEXT = re.compile(r"^[A-Za-z0-9 ._,:;'()/+=-]{0,120}$")

DEFAULT = dict(equity=2700.0, currency="USD", realised_today=0.0, realised_date=None,
               limits=dict(max_open_risk_pct=2.0, daily_loss_limit_pct=3.0, max_positions=3, max_net_units=None),
               positions=[], pending=[], updated=None)


def _num(v, name, positive=True, optional=False):
    if v is None or v == "":
        if optional:
            return None
        raise ValueError("%s is required" % name)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("%s must be a finite number" % name)
    if positive and x <= 0:
        raise ValueError("%s must be positive" % name)
    return x


def validate(raw):
    """Clean, bounded copy of an untrusted state. Raises ValueError with a plain message on anything wrong."""
    if not isinstance(raw, dict):
        raise ValueError("state must be an object")
    out = json.loads(json.dumps(DEFAULT))
    out["equity"] = _num(raw.get("equity", out["equity"]), "equity")
    out["realised_today"] = _num(raw.get("realised_today", 0.0), "realised_today", positive=False)
    cur = str(raw.get("currency", "USD"))
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise ValueError("currency must be a 3-letter code")
    out["currency"] = cur
    d = raw.get("realised_date")
    if d is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)):
        raise ValueError("realised_date must be YYYY-MM-DD")
    out["realised_date"] = d
    lim = raw.get("limits") or {}
    out["limits"] = dict(
        max_open_risk_pct=_num(lim.get("max_open_risk_pct", 2.0), "max_open_risk_pct"),
        daily_loss_limit_pct=_num(lim.get("daily_loss_limit_pct", 3.0), "daily_loss_limit_pct"),
        max_positions=int(_num(lim.get("max_positions", 3), "max_positions")),
        max_net_units=_num(lim.get("max_net_units"), "max_net_units", optional=True))
    for k in ("max_open_risk_pct", "daily_loss_limit_pct"):
        if out["limits"][k] > 100:
            raise ValueError("%s cannot exceed 100" % k)
    for kind, maxn in (("positions", 50), ("pending", 50)):
        rows = raw.get(kind) or []
        if not isinstance(rows, list) or len(rows) > maxn:
            raise ValueError("%s must be a list of at most %d" % (kind, maxn))
        clean = []
        for n, r in enumerate(rows):
            if not isinstance(r, dict):
                raise ValueError("%s[%d] must be an object" % (kind, n))
            sym = str(r.get("symbol", ""))
            if not SYMBOL.match(sym):
                raise ValueError("%s[%d]: bad symbol" % (kind, n))
            side = r.get("side")
            if side not in ("long", "short"):
                raise ValueError("%s[%d]: side must be long or short" % (kind, n))
            note = str(r.get("note", ""))
            if not TEXT.match(note):
                raise ValueError("%s[%d]: note has unsupported characters" % (kind, n))
            row = dict(id=str(r.get("id") or "%s%d" % (kind[0], n + 1))[:40], symbol=sym.upper(), side=side,
                       units=_num(r.get("units"), "units"), stop=_num(r.get("stop"), "stop", optional=True), note=note)
            if kind == "positions":
                row["entry"] = _num(r.get("entry"), "entry")
                row["target"] = _num(r.get("target"), "target", optional=True)
            else:
                kd = r.get("kind", "limit")
                if kd not in ("limit", "stop", "stop_limit"):
                    raise ValueError("pending[%d]: kind must be limit, stop or stop_limit" % n)
                row["kind"] = kd
                row["price"] = _num(r.get("price"), "price")
                row["target"] = _num(r.get("target"), "target", optional=True)
            clean.append(row)
        out[kind] = clean
    out["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return out


def exists(path=None):
    """Has the book ever been saved? Until then equity is only a default, and risk-based recommendations stay blocked."""
    return os.path.exists(path or PATH)


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            return validate(json.load(f))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT))


def save(state, path=None):
    clean = validate(state)
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=1)
    os.replace(tmp, p)
    return clean


def monitor(state, symbol, price, atr=None):
    """Everything the dashboard shows about your book for one symbol, at the given price."""
    state = validate(state)
    pos = [p for p in state["positions"] if p["symbol"] == symbol.upper()]
    pend = [p for p in state["pending"] if p["symbol"] == symbol.upper()]
    lim = state["limits"]
    rb = risk.risk_budget(state["equity"], pos, price, lim["max_open_risk_pct"], lim["daily_loss_limit_pct"],
                          state["realised_today"], lim["max_positions"])
    ex = rb["exposure"]
    alerts = list(rb["alerts"])
    sides = {p["side"] for p in pend}
    if len(sides) == 2:
        alerts.append("pending orders on both sides: a buy and a sell are waiting at the same time")
    for s in ("long", "short"):
        same = sorted(p["price"] for p in pend if p["side"] == s)
        for a, b in zip(same, same[1:]):
            if b - a <= 0.002 * price:
                alerts.append("two %s pending orders within 0.2%% of each other (%.2f and %.2f)" % (s, a, b))
                break
    for p in pend:
        if any(q["side"] != p["side"] for q in pos) and not ex["hedged"]:
            alerts.append("a pending %s order would hedge an open position" % p["side"])
            break
        if not p.get("stop"):
            alerts.append("pending order %s has no stop" % p["id"])
    if lim["max_net_units"] is not None and abs(ex["net_units"]) > lim["max_net_units"]:
        alerts.append("net exposure %.2f units is above your limit of %.2f" % (abs(ex["net_units"]), lim["max_net_units"]))
    status = "LIMIT" if rb["lockout"] or rb["remaining"] < 0 else "WARN" if alerts else "OK"
    scen = []
    if pos:
        for m in ((-3, -2, -1, 1, 2, 3) if atr else ()):
            px = price + m * atr
            scen.append(dict(label="%+d ATR move to %.2f" % (m, px), price=px, pnl=sum((px - p["entry"]) * (1 if p["side"] == "long" else -1) * p["units"] for p in pos)))
        stops = sum(-(p["entry"] - p["stop"]) * (1 if p["side"] == "long" else -1) * p["units"] for p in pos if p.get("stop"))
        tgts = sum((p["target"] - p["entry"]) * (1 if p["side"] == "long" else -1) * p["units"] for p in pos if p.get("target"))
        scen.append(dict(label="every stop is hit", price=None, pnl=stops, note="positions without a stop are not counted"))
        scen.append(dict(label="every target is hit", price=None, pnl=tgts, note="positions without a target are not counted"))
    legs = []
    for p in pos:
        sg = 1 if p["side"] == "long" else -1
        legs.append(dict(id=p["id"], side=p["side"], units=p["units"], entry=p["entry"], stop=p.get("stop"), target=p.get("target"),
                         open_pnl=(price - p["entry"]) * sg * p["units"],
                         to_stop_atr=((price - p["stop"]) * sg / atr) if (p.get("stop") and atr) else None,
                         to_target_atr=((p["target"] - price) * sg / atr) if (p.get("target") and atr) else None))
    return dict(status=status, equity=state["equity"], currency=state["currency"], price=price, exposure=ex, open_risk=rb["open_risk"],
                risk_budget=rb["budget"], risk_remaining=rb["remaining"], daily_pnl=rb["daily_pnl"], daily_loss_limit=rb["daily_loss_limit"],
                lockout=rb["lockout"], alerts=alerts, positions=legs, pending=pend, scenarios=scen, limits=lim,
                open_pnl_pct=(ex["open_pnl"] / state["equity"] * 100.0), updated=state["updated"],
                note="Read only. Figures come from the positions you entered; nothing is read from or sent to a broker.")
