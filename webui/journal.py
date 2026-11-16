"""Trading journal. Standard library only.

Every signal gets a stable ID. The journal records why it was taken or refused, which strategy and model versions
produced it, a reference to the exact data it was based on (hash and time range, not an image), the planned risk, and
later what really happened so planned and realised risk can be compared. Entries are stored in trader/journal.json.
Nothing here touches a broker.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "journal.json")
TAG = re.compile(r"^[a-z0-9 _-]{1,30}$")
TEXT = re.compile(r"^[^\x00-\x1f<>]{0,500}$")
MAX_ENTRIES = 5000


def now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(entries, path=None):
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries[-MAX_ENTRIES:], f, indent=1)
    os.replace(tmp, p)


def add_signal(sig, decision, reasons, ctx, path=None):
    """Record a signal and what was decided about it. ctx carries symbol, interval, versions and the data reference.
    Re-adding the same signal ID updates nothing and returns the existing entry, so IDs stay unique."""
    entries = load(path)
    for e in entries:
        if e["id"] == sig["id"]:
            return e
    if decision not in ("TRADE", "NO TRADE"):
        raise ValueError("decision must be TRADE or NO TRADE")
    e = dict(id=sig["id"], kind="signal", created=now_iso(), symbol=ctx["symbol"], interval=ctx["interval"], strategy=sig["strategy"],
             side=sig["side"], decided_t=sig["decided_t"], planned=dict(entry=sig["price"], stop=sig["stop"], targets=sig["targets"],
                                                                      risk=abs(sig["price"] - sig["stop"])),
             rationale=sig.get("note", ""), invalidation=sig.get("invalidation", ""), regime=sig.get("regime"), decision=decision,
             reasons=list(reasons), versions=dict(code=ctx.get("code_version"), strategy_status=ctx.get("strategy_status"), model=ctx.get("model_id")),
             data_ref=ctx.get("data_ref"), fill=None, close=None, tags=[], notes="")
    entries.append(e)
    _save(entries, path)
    return e


def _find(entries, sid):
    for e in entries:
        if e["id"] == sid:
            return e
    raise KeyError("no journal entry %s" % sid)


def record_fill(sid, price, units, ts=None, path=None):
    entries = load(path)
    e = _find(entries, sid)
    if e["decision"] != "TRADE":
        raise ValueError("this signal was recorded as NO TRADE; a fill cannot be added")
    if price <= 0 or units <= 0:
        raise ValueError("price and units must be positive")
    sg = 1 if e["side"] == "long" else -1
    plan = e["planned"]
    e["fill"] = dict(price=price, units=units, ts=now_iso(ts), slippage=(price - plan["entry"]) * sg,
                     realised_risk=(price - plan["stop"]) * sg)
    _save(entries, path)
    return e


def close_trade(sid, exit_price, ts=None, notes="", tags=(), stop_moved=False, path=None):
    entries = load(path)
    e = _find(entries, sid)
    if not e.get("fill"):
        raise ValueError("record the fill first")
    if exit_price <= 0:
        raise ValueError("exit_price must be positive")
    if not TEXT.match(notes or ""):
        raise ValueError("notes contain unsupported characters")
    tags = [str(t).lower() for t in tags]
    if any(not TAG.match(t) for t in tags):
        raise ValueError("tags must be short lowercase words")
    sg = 1 if e["side"] == "long" else -1
    f, plan = e["fill"], e["planned"]
    gross = (exit_price - f["price"]) * sg
    r_plan = gross / plan["risk"] if plan["risk"] else None
    r_real = gross / f["realised_risk"] if f["realised_risk"] > 0 else None
    auto = []
    if f["slippage"] > 0.25 * plan["risk"]:
        auto.append("high slippage")
    if r_real is not None and r_real < -1.2:
        auto.append("loss larger than planned")
    if stop_moved:
        auto.append("stop moved")
    if f["realised_risk"] > 1.25 * plan["risk"]:
        auto.append("risk larger than planned")
    e["close"] = dict(price=exit_price, ts=now_iso(ts), pnl_per_unit=gross, pnl=gross * f["units"], r_vs_planned_risk=r_plan,
                      r_vs_realised_risk=r_real, risk_ratio=f["realised_risk"] / plan["risk"] if plan["risk"] else None,
                      post_trade=dict(planned_risk=plan["risk"], realised_risk=f["realised_risk"], slippage=f["slippage"], auto_flags=auto))
    e["tags"] = sorted(set(tags) | set(auto))
    e["notes"] = notes or ""
    _save(entries, path)
    return e


def analyse(entries):
    """What keeps going wrong? Tags that recur, average slippage and how planned and realised risk compare."""
    closed = [e for e in entries if e.get("close")]
    sigs = [e for e in entries if e["kind"] == "signal"]
    tags = {}
    for e in closed:
        for t in e["tags"]:
            tags[t] = tags.get(t, 0) + 1
    slip = [e["fill"]["slippage"] / e["planned"]["risk"] for e in closed if e["planned"]["risk"]]
    rr = [e["close"]["risk_ratio"] for e in closed if e["close"]["risk_ratio"] is not None]
    rs = [e["close"]["r_vs_realised_risk"] for e in closed if e["close"]["r_vs_realised_risk"] is not None]
    return dict(signals=len(sigs), taken=sum(1 for e in sigs if e["decision"] == "TRADE"), refused=sum(1 for e in sigs if e["decision"] == "NO TRADE"),
                closed=len(closed), open=sum(1 for e in sigs if e.get("fill") and not e.get("close")),
                recurring=[dict(tag=t, count=c) for t, c in sorted(tags.items(), key=lambda kv: -kv[1]) if c >= 2],
                tag_counts=tags, mean_slippage_in_r=(sum(slip) / len(slip) if slip else None),
                mean_risk_ratio=(sum(rr) / len(rr) if rr else None), mean_r=(sum(rs) / len(rs) if rs else None),
                note="Planned risk is what the plan said; realised risk is measured from the real fill to the planned stop.")


def recent(entries, n=30):
    return list(reversed(entries[-n:]))
