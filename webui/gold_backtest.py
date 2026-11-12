#!/usr/bin/env python3
"""Walk-forward backtest of the gold short-term setups. Standard library only.

It replays history one daily bar at a time. On each day the zones and the four
scenarios are rebuilt from the bars up to that day only (the exact code the
Gold assessment page uses), then the following bars decide whether the setup
fills and where it exits. Nothing from the future leaks into the setups.

Conservative rules:
  * stop is checked before target on the same bar
  * a gap through the stop exits at the open (worse), a gap through a target
    still exits at the target (no bonus)
  * a pullback order is a limit; a breakout needs a daily close beyond the
    trigger and fills at the next open
  * one working order / open trade per setup type at a time
  * a flat cost per ounce is charged on every trade

Past results do not predict future ones. Not financial advice.

    python webui/gold_backtest.py --symbol GC=F --years 10y
"""
import argparse
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold  # noqa: E402

WARMUP = 250


def exit_trade(bars, fill_i, sgn, fill, stop, target, hold, same_bar):
    """Walk forward from the fill bar. Returns (exit_price, exit_index, how)."""
    last_i = min(len(bars) - 1, fill_i + hold)
    for m in range(fill_i, last_i + 1):
        _, h, l, c, o = bars[m]
        gap_ok = m > fill_i or not same_bar          # open is meaningless on a limit-fill bar
        if sgn == 1:
            if l <= stop:
                return (min(o, stop) if gap_ok and o <= stop else stop), m, "stop"
            if h >= target:
                return target, m, "target"
        else:
            if h >= stop:
                return (max(o, stop) if gap_ok and o >= stop else stop), m, "stop"
            if l <= target:
                return target, m, "target"
    return bars[last_i][3], last_i, "time"


def simulate(bars, t, s, window, hold, cost):
    sgn = 1 if s["side"] == "long" else -1
    n = len(bars)
    fill_i = fill = None
    same_bar = False
    if s["kind"] == "pullback":
        for j in range(t + 1, min(n, t + 1 + window)):
            _, h, l, c, o = bars[j]
            if sgn == 1 and l <= s["level"]:
                fill_i, fill, same_bar = j, min(o, s["level"]), True
                break
            if sgn == -1 and h >= s["level"]:
                fill_i, fill, same_bar = j, max(o, s["level"]), True
                break
    else:
        for j in range(t + 1, min(n - 1, t + 1 + window)):
            c = bars[j][3]
            if (sgn == 1 and c > s["level"]) or (sgn == -1 and c < s["level"]):
                fill_i, fill = j + 1, bars[j + 1][4]
                break
    if fill_i is None:
        return None, min(n - 1, t + window)
    risk = (fill - s["stop"]) * sgn
    targets = [x for x in s["targets"] if (x - fill) * sgn > 0]
    if risk <= 0 or not targets:
        return None, fill_i                      # setup no longer valid at the fill price
    out = {"i": fill_i, "t": bars[fill_i][0], "risk": risk, "fill": fill}
    last_exit = fill_i
    for key, tgt in (("r1", targets[0]), ("r2", targets[1] if len(targets) > 1 else None)):
        if tgt is None:
            out[key] = None
            continue
        px, xi, how = exit_trade(bars, fill_i, sgn, fill, s["stop"], tgt, hold, same_bar)
        out[key] = ((px - fill) * sgn - cost) / risk
        out[key + "_how"] = how
        last_exit = max(last_exit, xi)
    out["planned_rr"] = abs(targets[0] - fill) / risk
    out["trend"], out["weak"], out["name"], out["exit_i"] = s["trend"], s["weak"], s["name"], last_exit
    return out, last_exit


def baseline(bars, start, hold, cost, mult=1.5):
    """Same exit mechanics, no zones: enter next open, stop = target = mult x ATR."""
    res = {"long": [], "short": []}
    for side, sgn in (("long", 1), ("short", -1)):
        i = start
        while i < len(bars) - 2:
            a = gold.atr(bars[:i + 1])
            fill_i, fill = i + 1, bars[i + 1][4]
            stop, tgt = fill - sgn * mult * a, fill + sgn * mult * a
            px, xi, _ = exit_trade(bars, fill_i, sgn, fill, stop, tgt, hold, False)
            res[side].append(((px - fill) * sgn - cost) / (mult * a))
            i = xi + 1
    return res


def stats(rs):
    n = len(rs)
    if n == 0:
        return None
    avg = sum(rs) / n
    se = statistics.pstdev(rs) / math.sqrt(n) if n > 1 else float("nan")
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else float("inf")
    streak = cur = 0
    for r in rs:
        cur = cur + 1 if r <= 0 else 0
        streak = max(streak, cur)
    return dict(n=n, win=len(wins) / n * 100, avg=avg, se=se, pf=pf, streak=streak)


def row(label, rs):
    s = stats(rs)
    if not s:
        return "  %-34s %5d" % (label, 0)
    return "  %-34s %5d  %5.0f%%  %+6.2fR  +/-%.2f  %5s  %3d" % (
        label, s["n"], s["win"], s["avg"], s["se"] * 1.96,
        "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"], s["streak"])


HEAD = "  %-34s %5s  %6s  %7s  %6s  %5s  %3s" % ("", "trades", "win", "avg R", "95%", "PF", "run")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward backtest of the gold short-term setups.")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--years", default="10y", choices=["2y", "5y", "10y"])
    ap.add_argument("--window", type=int, default=10, help="bars an order stays working")
    ap.add_argument("--hold", type=int, default=20, help="max bars to hold a trade")
    ap.add_argument("--cost", type=float, default=0.5, help="round-trip cost in $ per ounce")
    ap.add_argument("--json", action="store_true", help="structured output for the web view")
    args = ap.parse_args(argv)
    if not (1 <= args.window <= 60 and 1 <= args.hold <= 120 and 0 <= args.cost <= 50):
        raise SystemExit("error: window 1-60, hold 1-120, cost 0-50")

    bars, _ = gold.fetch_bars(args.symbol, args.years)
    if len(bars) < WARMUP + 100:
        raise SystemExit("error: only %d bars; need %d" % (len(bars), WARMUP + 100))

    busy, trades = {}, []
    for t in range(WARMUP, len(bars) - 2):
        win, pts, extra = gold.short_term_inputs(bars[:t + 1])
        c = gold.compute_setups(win, pts, 5, 120, extra, 100)
        for s in c["setups"]:
            key = s["name"]
            if busy.get(key, -1) >= t:
                continue
            tr, until = simulate(bars, t, s, args.window, args.hold, args.cost)
            busy[key] = until
            if tr:
                tr["setup_day"] = t
                trades.append(tr)

    import datetime as dt
    day = lambda ts: dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    first, lastd = day(bars[WARMUP][0]), day(bars[-1][0])
    trades.sort(key=lambda x: x["i"])
    bh = (bars[-1][3] / bars[WARMUP][3] - 1) * 100
    L = []
    add = L.append
    add("GOLD BACKTEST  %s  %s to %s  (%d daily bars, walk-forward, no look-ahead)" % (args.symbol, first, lastd, len(bars) - WARMUP))
    add("Order stays working %d bars, trade held at most %d bars, cost $%.2f/oz round trip. Buy-and-hold over the span: %+.0f%%." % (
        args.window, args.hold, args.cost, bh))
    add("R = profit divided by the initial risk to the stop. avg R above 0 = made money per unit of risk.")
    add("95% = rough +/- band on avg R (1.96 standard errors); if it spans zero the result is not distinguishable from luck.")
    add("The band is optimistic: trades from different setups overlap in time, so they are not fully independent.")
    add("PF = profit factor (gross wins / gross losses). run = longest losing streak.")
    add("")
    for ex, label in (("r1", "EXIT AT FIRST TARGET"), ("r2", "EXIT AT SECOND TARGET (setups that have one)")):
        add(label)
        add(HEAD)
        pool = [x for x in trades if x[ex] is not None]
        for name in ("LONG on a pullback", "LONG on a breakout", "SHORT on a rally into resistance", "SHORT on a breakdown"):
            add(row(name, [x[ex] for x in pool if x["name"] == name]))
        add(row("ALL SETUPS", [x[ex] for x in pool]))
        add("")
    pool = trades
    add("WHAT SEPARATES GOOD FROM BAD (exit at first target)")
    add(HEAD)
    for lab, f in (("with the trend", lambda x: x["trend"] == "with"),
                   ("counter-trend", lambda x: x["trend"] == "counter"),
                   ("range / no trend", lambda x: x["trend"] == "range"),
                   ("zone with 2+ agreeing levels", lambda x: not x["weak"]),
                   ("weak zone (1 level)", lambda x: x["weak"]),
                   ("planned reward >= 1.5R", lambda x: x["planned_rr"] >= 1.5),
                   ("planned reward < 1.5R (thin)", lambda x: x["planned_rr"] < 1.5)):
        add(row(lab, [x["r1"] for x in pool if f(x)]))
    add("")
    add("SIDE x TREND (exit at first target). The span was a bull market, so longs have a tailwind.")
    add(HEAD)
    for side in ("LONG", "SHORT"):
        for tr, lab in (("with", "with the trend"), ("counter", "counter-trend"), ("range", "range / no trend")):
            add(row("%s, %s" % (side.lower(), lab),
                    [x["r1"] for x in pool if x["name"].startswith(side) and x["trend"] == tr]))
    add("")
    mid = trades[len(trades) // 2]["i"] if trades else 0
    add("IS IT STABLE? (first half of the history vs second half, exit at first target)")
    add(HEAD)
    add(row("first half  (%s ->)" % first, [x["r1"] for x in trades if x["i"] < mid]))
    add(row("second half (-> %s)" % lastd, [x["r1"] for x in trades if x["i"] >= mid]))
    add("")
    base = baseline(bars, WARMUP, args.hold, args.cost)
    add("NO-ZONE BASELINE (enter every time at the next open, stop = target = 1.5 ATR, same costs)")
    add(HEAD)
    add(row("baseline long", base["long"]))
    add(row("baseline short", base["short"]))
    add("  A strategy only earns trust if it beats this noise by more than its 95% band.")
    add("")
    add("Backtests overstate what you can capture: delayed Yahoo bars, no slippage beyond the flat cost, no margin")
    add("or contract-roll effects, and the rules were looked at on this same history. Past results do not predict")
    add("future ones. This is not financial advice.")
    if not args.json:
        print("\n".join(L))
        return 0

    import json

    def js(rs):
        s = stats(rs)
        if not s:
            return None
        return dict(n=s["n"], win=s["win"], avg=s["avg"],
                    ci=None if s["se"] != s["se"] else 1.96 * s["se"],
                    pf=None if s["pf"] == float("inf") else s["pf"], streak=s["streak"])

    names = ("LONG on a pullback", "LONG on a breakout", "SHORT on a rally into resistance", "SHORT on a breakdown")
    pool1 = [x for x in trades if x["r1"] is not None]
    pool2 = [x for x in trades if x["r2"] is not None]
    cum, cumw, curve = 0.0, 0.0, []
    for x in trades:
        cum += x["r1"]
        if x["trend"] == "with":
            cumw += x["r1"]
        curve.append(dict(t=x["t"], r=cum, w=cumw))
    edges = [(-9e9, -0.75, "stopped (-1R)"), (-0.75, -0.25, "-0.5R"), (-0.25, 0.25, "about 0"), (0.25, 0.75, "+0.5R"),
             (0.75, 1.5, "+1R"), (1.5, 3, "+2R"), (3, 9e9, "+3R or more")]
    hist = [dict(label=lab, n=sum(1 for x in pool1 if lo <= x["r1"] < hi)) for lo, hi, lab in edges]
    groups = [dict(label=lab, stats=js([x["r1"] for x in pool1 if f(x)])) for lab, f in (
        ("with the trend", lambda x: x["trend"] == "with"),
        ("against the trend", lambda x: x["trend"] == "counter"),
        ("no clear trend", lambda x: x["trend"] == "range"),
        ("zone with 2+ agreeing levels", lambda x: not x["weak"]),
        ("weak zone (1 level)", lambda x: x["weak"]),
        ("planned reward 1.5R or more", lambda x: x["planned_rr"] >= 1.5),
        ("planned reward under 1.5R", lambda x: x["planned_rr"] < 1.5))]
    data = dict(
        meta=dict(symbol=args.symbol, first=first, last=lastd, bars=len(bars) - WARMUP, window=args.window,
                  hold=args.hold, cost=args.cost, buy_hold=bh, trades=len(trades), t0=trades[0]["t"] if trades else None,
                  t1=trades[-1]["t"] if trades else None),
        setups=[dict(name=n, r1=js([x["r1"] for x in pool1 if x["name"] == n]),
                     r2=js([x["r2"] for x in pool2 if x["name"] == n])) for n in names],
        all=dict(r1=js([x["r1"] for x in pool1]), r2=js([x["r2"] for x in pool2])),
        groups=groups,
        side_trend=[dict(side=side.lower(), trend=tr, stats=js([x["r1"] for x in pool1
                                                                if x["name"].startswith(side) and x["trend"] == tr]))
                    for side in ("LONG", "SHORT") for tr in ("with", "counter", "range")],
        halves=[dict(label="First half", span="%s to %s" % (first, day(trades[len(trades) // 2]["t"]) if trades else ""),
                     stats=js([x["r1"] for x in trades if x["i"] < mid])),
                dict(label="Second half", span="%s to %s" % (day(trades[len(trades) // 2]["t"]) if trades else "", lastd),
                     stats=js([x["r1"] for x in trades if x["i"] >= mid]))],
        baseline=dict(long=js(base["long"]), short=js(base["short"])),
        curve=curve, hist=hist, report="\n".join(L))
    print(json.dumps(data, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
