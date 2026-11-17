"""Recommendation scorecard. Standard library only. Read only: it never places or changes an order.

Every trade plan the console recommends is recorded the moment it is shown, whether or not you take it. Later, closed price bars are
replayed to see what WOULD have happened, and each plan is graded:

  WAITING        not triggered yet, still inside its expiry
  OPEN           triggered, no stop or target resolved yet
  WIN            the first target was reached before the stop
  LOSS           the stop was hit before the first target
  TIMED OUT      triggered but still unresolved after the holding limit; scored at the last close
  NOT TRIGGERED  the entry was never reached before the plan expired (not a win or a loss)
  INVALIDATED    price closed beyond the stop before the entry, so the idea died without a trade

Grading is deliberately conservative, so the record does not flatter the recommendations:
  * A pullback plan is a limit order: price must trade ONE TICK THROUGH the entry. A touch is not a fill, and the fill bar earns no
    target credit (only the stop is checked on it).
  * A breakout plan needs a bar to CLOSE beyond the entry, then fills at the NEXT bar's open. If that open is already past the stop or the
    first target, the plan counts as not triggered.
  * If a stop and a target are both reachable in one bar, the stop is assumed first.
  * A gap through the stop exits at the open, not at the stop.
  * The plan's own split is used: 50% at target 1, 30% at target 2, 20% at target 3, with the stop moved to the entry after target 1.
  * Round-trip costs (the profile's default_cost per unit) are charged on every filled trade.
Results are in R (1R = the distance from the actual fill to the stop). This is a replay on delayed Yahoo bars, not a promise about the future.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "scorecard.json")

STEP = {"15m": 900, "1h": 3600, "4h": 14400, "daily": 86400, "weekly": 604800}
EXPIRY = {"15m": 16, "1h": 12, "4h": 6, "daily": 5, "weekly": 3}          # bars the plan stays valid, as the plan itself says
HOLD = {"15m": 48, "1h": 48, "4h": 30, "daily": 20, "weekly": 12}         # bars a filled trade may stay open before it is scored as timed out
SPLIT = (0.5, 0.3, 0.2)
RES_KEYS = {"15m": "15m", "1h": "1h", "4h": "4h", "daily": "1d", "weekly": "1w"}
ALIAS = {"1d": "daily", "1w": "weekly", "short": "daily", "long": "weekly"}
ENGINE = 2        # 2: R in planned risk; a limit that gaps through fills at the open; an unfinished position is OPEN, never a win
MAX_KEEP = 1500
RESOLVED = ("WIN", "LOSS")
FINAL = ("WIN", "LOSS", "TIMED OUT", "NOT TRIGGERED", "INVALIDATED")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save(recs, path=None):
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(recs[-MAX_KEEP:], f, indent=1)
    os.replace(tmp, p)


def _key(symbol, tf, name, entry, stop):
    return "%s|%s|%s|%.4f|%.4f" % (symbol, tf, name, entry, stop)


def make_record(symbol, tf, plan, created_ts, source="assessment"):
    """A frozen copy of one plan as it was shown. Returns None when the plan cannot be graded."""
    tf = ALIAS.get(tf, tf)
    if tf not in STEP:
        return None
    try:
        entry, stop = float(plan["entry"]), float(plan["stop"])
        tg = [float(t["price"] if isinstance(t, dict) else t) for t in plan["targets"]][:3]
    except (KeyError, TypeError, ValueError):
        return None
    sgn = 1 if plan["side"] == "long" else -1 if plan["side"] == "short" else 0
    risk = (entry - stop) * sgn
    if not sgn or risk <= 0 or not tg or any((t - entry) * sgn <= 0 for t in tg) or plan.get("kind") not in ("pullback", "breakout"):
        return None
    while len(tg) < 3:
        tg.append(tg[-1])
    prof = assets.profile(symbol)
    key = _key(symbol, tf, plan["name"], entry, stop)
    rid = "rc-" + hashlib.sha1(("%s|%d" % (key, int(created_ts))).encode()).hexdigest()[:10]
    return dict(id=rid, key=key, symbol=symbol, tf=tf, name=plan["name"], side=plan["side"], kind=plan["kind"], entry=entry, stop=stop, targets=tg,
                created_ts=created_ts, created=_iso(created_ts), grade=plan.get("grade"), score=plan.get("score"), rank=plan.get("rank"),
                status_then=plan.get("status"), expiry_bars=EXPIRY[tf], hold_bars=HOLD[tf], tick=assets.tick_for(entry), cost=float(prof["default_cost"]),
                why=list(plan.get("why") or [])[:4], source=source, result=None,
                trend=plan.get("trend"), dist_atr=plan.get("dist_atr"), agree=list(plan.get("agree") or []), weak=plan.get("weak"), blend_r=plan.get("blend_r"))


def record(recs, symbol, tf, plans, created_ts, max_rank=3, source="assessment", index=None):
    """Add the plans that were shown (rank 1..max_rank). A plan already recorded and still alive, or recorded within its own expiry, is not added twice.
    `index` (key -> records) lets a long replay avoid rescanning every record; it is kept up to date here."""
    added = []
    if index is None:
        index = {}
        for o in recs:
            index.setdefault(o["key"], []).append(o)
    for p in plans or []:
        if (p.get("rank") or 99) > max_rank:
            continue
        r = make_record(symbol, tf, p, created_ts, source)
        if r is None:
            continue
        window = STEP[r["tf"]] * r["expiry_bars"]
        dup = [o for o in index.get(r["key"], ()) if created_ts - o["created_ts"] < window or not (o.get("result") or {}).get("final")]
        if dup:
            continue
        recs.append(r)
        index.setdefault(r["key"], []).append(r)
        added.append(r)
    return added


def grade(rec, bars, now):
    """Replay the closed bars that started after the plan was recorded. Pure function of the plan, the bars and the clock."""
    step = STEP[rec["tf"]]
    sgn = 1 if rec["side"] == "long" else -1
    entry, stop, tg, tick = rec["entry"], rec["stop"], rec["targets"], rec["tick"]
    seq = [b for b in bars if b[0] >= rec["created_ts"] and b[0] + step <= now]
    out = dict(state="WAITING", checked_bars=len(seq), checked_to=_iso(seq[-1][0] + step) if seq else None, fill_price=None, fill_time=None,
               exit_time=None, best_target=0, r_gross=None, r_net=None, note="", legs=[])
    filled, fill, fill_t = False, None, None
    risk = abs(entry - stop)
    k, rem, real, stop_cur = 0, 1.0, 0.0, stop
    waited, trig = 0, None
    closed, first_limit_bar = False, False
    bars_in = 0
    last_close = None

    def beyond_stop(c):
        return c < stop if sgn == 1 else c > stop

    for idx, b in enumerate(seq):
        t, hi, lo, cl, op = b
        last_close = cl
        if not filled:
            if rec["kind"] == "pullback":
                waited += 1
                if waited > rec["expiry_bars"]:
                    out.update(state="NOT TRIGGERED", note="price never traded through the entry before the plan expired")
                    break
                if (lo <= entry - tick) if sgn == 1 else (hi >= entry + tick):
                    # a bar that opens beyond the limit fills at its open (the better price); otherwise at the limit
                    filled, fill, fill_t, first_limit_bar = True, (min(entry, op) if sgn == 1 else max(entry, op)), t, True
                    if (fill - stop) * sgn <= 0:
                        out.update(state="INVALIDATED", note="the bar opened beyond the stop, so no clean entry existed", exit_time=_iso(t + step))
                        filled = False
                        break
                elif beyond_stop(cl):
                    out.update(state="INVALIDATED", note="price closed beyond the stop before the entry was reached", exit_time=_iso(t + step))
                    break
                else:
                    continue
            else:
                if trig is None:
                    waited += 1
                    if waited > rec["expiry_bars"]:
                        out.update(state="NOT TRIGGERED", note="no bar closed beyond the entry before the plan expired")
                        break
                    if cl > entry if sgn == 1 else cl < entry:
                        trig = idx
                    elif beyond_stop(cl):
                        out.update(state="INVALIDATED", note="price closed beyond the stop before the breakout", exit_time=_iso(t + step))
                        break
                    continue
                fill, fill_t, filled = op, t, True                      # the bar after the confirming close: fill at its open
                if (fill - stop) * sgn <= 0:
                    out.update(state="NOT TRIGGERED", note="the next bar opened beyond the stop, so no clean entry existed")
                    break
                if (tg[0] - fill) * sgn <= 0:
                    out.update(state="NOT TRIGGERED", note="the next bar opened beyond the first target, so the move was already over")
                    break
                # R is always measured in the PLANNED risk (entry to stop): a late fill that is then stopped loses MORE than 1R, as it would in practice
        if filled:
            bars_in += 1
            hit_stop = lo <= stop_cur if sgn == 1 else hi >= stop_cur
            if hit_stop:
                px = min(stop_cur, op) if sgn == 1 else max(stop_cur, op)   # a gap exits at the open
                real += rem * (px - fill) * sgn / risk
                out["legs"].append(dict(price=px, frac=rem, why="stop" if k == 0 else "stop at entry"))
                rem, closed = 0.0, True
                out["exit_time"] = _iso(t + step)
                out["note"] = "stopped out" if k == 0 else "stopped at the entry after target 1 was reached"
                break
            if first_limit_bar:
                first_limit_bar = False                                   # no target credit on a limit order's fill bar
                continue
            while k < 3 and rem > 1e-9 and ((hi >= tg[k]) if sgn == 1 else (lo <= tg[k])):
                real += SPLIT[k] * (tg[k] - fill) * sgn / risk
                out["legs"].append(dict(price=tg[k], frac=SPLIT[k], why="target %d" % (k + 1)))
                rem -= SPLIT[k]
                k += 1
                if k == 1:
                    stop_cur = fill                                       # stop to the entry after target 1, as the plan says
            if k >= 1 and rem > 1e-9 and ((lo <= stop_cur) if sgn == 1 else (hi >= stop_cur)):
                rem_exit = 0.0                                            # bar order unknown: assume the entry stop was touched too
                real += rem * rem_exit
                out["legs"].append(dict(price=fill, frac=rem, why="stop at entry"))
                rem, closed = 0.0, True
                out["exit_time"] = _iso(t + step)
                out["note"] = "stopped at the entry after target %d" % k
                break
            if k == 3 or rem <= 1e-9:
                closed = True
                out["exit_time"] = _iso(t + step)
                out["note"] = "all three targets reached"
                break
            if bars_in >= rec["hold_bars"]:
                real += rem * (cl - fill) * sgn / risk
                out["legs"].append(dict(price=cl, frac=rem, why="time exit"))
                rem, closed = 0.0, True
                if k >= 1:
                    out.update(state="WIN", exit_time=_iso(t + step), note="target %d was reached; the rest was scored at the last close after %d bars" % (k, rec["hold_bars"]))
                else:
                    out.update(state="TIMED OUT", exit_time=_iso(t + step), note="still unresolved after %d bars; scored at the last close" % rec["hold_bars"])
                break
    if filled and out["state"] in ("WAITING",):
        cost_r = rec["cost"] / risk
        if closed:
            out["state"] = "WIN" if k >= 1 else "LOSS"
        else:
            real += rem * ((last_close - fill) * sgn / risk if last_close is not None else 0.0)
            out["state"] = "OPEN"                      # an unfinished position is never counted as a win or a loss
            out["legs"].append(dict(price=last_close, frac=rem, why="mark (not closed)"))
            out["note"] = ("target %d reached and the stop is at the entry, so it cannot lose; the rest is still running (marked at the last close)" % k) if k >= 1 else "triggered; no target or stop yet (marked at the last close)"
        out.update(fill_price=fill, fill_time=_iso(fill_t), best_target=k, r_gross=real, r_net=real - cost_r, risk=risk, risk_fill=abs(fill - stop))
    elif filled and out["state"] in ("TIMED OUT", "WIN"):
        out.update(fill_price=fill, fill_time=_iso(fill_t), best_target=k, r_gross=real, r_net=real - rec["cost"] / risk, risk=risk, risk_fill=abs(fill - stop))
    out["engine"] = ENGINE
    if out["state"] == "WAITING" and now - rec["created_ts"] > (rec["expiry_bars"] + 1) * step and not filled:
        out.update(state="NOT TRIGGERED", note="the plan expired before the entry was reached")
    out["final"] = out["state"] in ("NOT TRIGGERED", "INVALIDATED", "LOSS", "TIMED OUT") or (out["state"] == "WIN" and (closed or "after %d bars" % rec["hold_bars"] in out["note"]))
    return out


def grade_all(recs, bars_by_symbol, now):
    """Grade every record that is not final yet, using the bars we have for its symbol. Returns the records that changed state."""
    changed = []
    for r in recs:
        res = r.get("result") or {}
        if res.get("final") and res.get("engine") == ENGINE:
            continue                            # a result made by an older grading engine is regraded from the bars
        bars = (bars_by_symbol.get(r["symbol"]) or {}).get(r["tf"])
        if not bars:
            continue
        g = grade(r, bars, now)
        if g["state"] != res.get("state"):
            changed.append(r)
        r["result"] = g
    return changed


def wilson(w, n, z=1.96):
    if n <= 0:
        return None
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def _agg(rows):
    st = [(r.get("result") or {}).get("state", "WAITING") for r in rows]
    cnt = {s: st.count(s) for s in ("WIN", "LOSS", "TIMED OUT", "OPEN", "WAITING", "NOT TRIGGERED", "INVALIDATED")}
    w, l = cnt["WIN"], cnt["LOSS"]
    done = [r["result"]["r_net"] for r in rows if (r.get("result") or {}).get("state") in ("WIN", "LOSS", "TIMED OUT") and r["result"].get("r_net") is not None]
    n = w + l
    prot = sum(1 for r in rows if (r.get("result") or {}).get("state") == "OPEN" and (r["result"].get("best_target") or 0) >= 1)
    return dict(protected_open=prot, total=len(rows), counts=cnt, resolved=n, win_rate=(w / n if n else None), win_ci=wilson(w, n), wl_ratio=(w / l if l else None),
                mean_r=(sum(done) / len(done) if done else None), total_r=sum(done), enough=n >= 30,
                triggered=sum(cnt[s] for s in ("WIN", "LOSS", "TIMED OUT", "OPEN")))


def summary(recs):
    out = dict(all=_agg(recs), by={})
    for dim in ("symbol", "tf", "kind", "side", "grade"):
        groups = {}
        for r in recs:
            groups.setdefault(str(r.get(dim)), []).append(r)
        out["by"][dim] = {k: _agg(v) for k, v in sorted(groups.items())}
    return out


def story(r):
    """One plain sentence on what the plan would have done."""
    g = r.get("result") or {}
    s = g.get("state", "WAITING")
    base = "%s %s on the %s chart, entry %.2f, stop %.2f, first target %.2f." % (r["name"], "(recommended " + r["created"].replace("T", " ")[:16] + " UTC)", r["tf"], r["entry"], r["stop"], r["targets"][0])
    if s == "WAITING":
        return base + " Not triggered yet."
    if s == "NOT TRIGGERED":
        return base + " Never triggered: " + g.get("note", "") + ". Nothing would have been lost or won."
    if s == "INVALIDATED":
        return base + " Invalidated before entry: " + g.get("note", "") + ". Nothing would have been lost or won."
    fill = "Filled at %.2f on %s." % (g["fill_price"], g["fill_time"].replace("T", " ")[:16])
    if s == "WIN":
        return base + " " + fill + " It reached target %d. Net result %+.2fR after costs: it would have WON." % (g["best_target"], g["r_net"])
    if s == "LOSS":
        return base + " " + fill + " The stop was hit first. Net result %+.2fR after costs: it would have LOST." % g["r_net"]
    if s == "OPEN" and g.get("best_target", 0) >= 1:
        return base + " " + fill + " Target %d was reached and the stop moved to the entry, so it cannot lose from here; the rest is still running, currently %+.2fR (not final)." % (g["best_target"], g["r_net"])
    if s == "TIMED OUT":
        return base + " " + fill + " Unresolved after the holding limit, scored at %+.2fR." % g["r_net"]
    return base + " " + fill + " Still open, currently %+.2fR (not final)." % g["r_net"]


def view(recs, limit=80):
    rows = []
    for r in sorted(recs, key=lambda x: -x["created_ts"])[:limit]:
        g = r.get("result") or {}
        rows.append(dict(id=r["id"], symbol=r["symbol"], tf=r["tf"], name=r["name"], side=r["side"], kind=r["kind"], grade=r.get("grade"), created=r["created"],
                         entry=r["entry"], stop=r["stop"], targets=r["targets"], state=g.get("state", "WAITING"), r_net=g.get("r_net"), best_target=g.get("best_target", 0),
                         fill_price=g.get("fill_price"), fill_time=g.get("fill_time"), exit_time=g.get("exit_time"), note=g.get("note", ""), story=story(r)))
    return rows


# ---- recording hooks ---------------------------------------------------------------------------------------------------------------

def record_from_json(out, symbol, now=None, path=None):
    """Called with the Gold or Bitcoin assessment JSON, so every plan the page shows is on record."""
    now = time.time() if now is None else now
    recs = load(path)
    added = []
    for tf, sec in (out.get("intraday") or {}).items():
        added += record(recs, symbol, tf, sec.get("plans"), now)
    for key in ("short", "long"):
        if out.get(key):
            added += record(recs, symbol, key, out[key].get("plans"), now)
    if added:
        save(recs, path)
    return added


def record_from_res(res, symbol, now=None, path=None):
    """Called by the watcher with the intraday analysis result."""
    now = time.time() if now is None else now
    recs = load(path)
    added = record_from_res_into(recs, res, symbol, now)
    if added:
        save(recs, path)
    return added


def bars_from_res(res):
    return {tf: res["bars"].get(RES_KEYS[tf]) for tf in STEP}


def snapshot(symbols=("GC=F", "BTC-USD"), now=None, path=None, analyse=None):
    """Record the plans showing now, grade everything not final, save, and return the full scorecard document."""
    now = time.time() if now is None else now
    if analyse is None:
        import gold_intraday as gi
        analyse = gi.analyse
    recs = load(path)
    bars, errors, added = {}, [], 0
    for s in symbols:
        try:
            res = analyse(s)
        except (Exception, SystemExit) as e:
            errors.append("%s: %s" % (s, str(e)[:160]))
            continue
        added += len(record_from_res_into(recs, res, s, now))
        bars[s] = bars_from_res(res)
    changed = grade_all(recs, bars, now)
    save(recs, path)
    return document(recs, now, added, len(changed), errors, _replay_summary())


def _replay_summary():
    """The saved historical backtest, if one has been run (slimmed down for the page)."""
    try:
        import recommendation_backtest as rb
        d = rb.load_doc()
    except Exception:
        return None
    return dict(checked=d["checked"], summary=dict(all=d["summary"]["all"])) if d else None


def record_from_res_into(recs, res, symbol, now):
    added = []
    for tf in ("15m", "1h", "4h", "1d", "1w"):
        sec = res["setup"].get(tf)
        if sec and sec.get("plans"):
            added += record(recs, symbol, tf, sec["plans"], now)
    return added


def document(recs, now, added=0, changed=0, errors=(), replay=None):
    return dict(replay=replay,checked=_iso(now), summary=summary(recs), rows=view(recs), added=added, changed=changed, errors=list(errors),
                method=("Every recommended plan is recorded when shown and graded later against closed bars, whether or not you traded it. "
                        "Limit entries need a tick of penetration, breakouts enter at the next open after a confirming close, a stop wins any same-bar tie, "
                        "gaps exit at the open, the 50/30/20 split with a breakeven stop after target 1 is used, and costs are charged. "
                        "Win rate counts only plans that triggered and resolved; plans that never triggered are listed separately."),
                caution="Yahoo bars are delayed and last-trade only. A small record says little: rates are quoted with 95% bands and flagged until 30 resolved plans exist. Past results are not a promise. Not financial advice.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score every recommended plan against what price did next.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fetch", action="store_true", help="only show the saved record; do not fetch bars or record new plans")
    a = ap.parse_args(argv)
    now = time.time()
    doc = document(load(), now, replay=_replay_summary()) if a.no_fetch else snapshot(now=now)
    if a.json:
        print(json.dumps(doc))
    else:
        s = doc["summary"]["all"]
        print("SCORECARD %s: %d plans recorded, %d triggered, %d won, %d lost" % (doc["checked"], s["total"], s["triggered"], s["counts"]["WIN"], s["counts"]["LOSS"]))
        for r in doc["rows"][:15]:
            print(" ", r["state"].ljust(13), r["symbol"], r["tf"], r["name"], "" if r["r_net"] is None else "%+.2fR" % r["r_net"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
