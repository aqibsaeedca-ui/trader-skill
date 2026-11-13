#!/usr/bin/env python3
"""Multi-timeframe gold levels, confluence and setups (15m, 1h, 4h, daily, weekly).

Standard library only. Yahoo has no 4-hour bars, so 4h is built from 1-hour bars
in fixed 4-hour UTC blocks. Intraday history is short (15m about 30 days, 1h about
6 months), and the quotes are delayed.

Each timeframe gets two passes:
  * "own" zones, from that chart's own swings, averages, Fibonacci levels and
    round numbers. These alone decide whether a timeframe "agrees" at a level.
  * "setup" zones, the same plus the zones of every higher timeframe added as
    extra levels, so a 15m setup shows when it sits on a 4h or daily level.

Confluence = a price area where own zones from two or more timeframes overlap.
Statistics describe the past and do not predict price. Not financial advice.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold  # noqa: E402

INTRADAY = ("15m", "1h", "4h")
ALL_TF = ("15m", "1h", "4h", "1d", "1w")
CFG = {                       # swing width, lookback bars, round-number step ($)
    "15m": dict(k=4, win=120, step=25, name="15-minute"),
    "1h": dict(k=4, win=120, step=50, name="1-hour"),
    "4h": dict(k=3, win=90, step=100, name="4-hour"),
}
WEIGHT = {"15m": 1, "1h": 1, "4h": 2, "1d": 3, "1w": 3}
_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def get_bars(symbol, interval, rng, ttl):
    return cached((symbol, interval, rng), ttl, lambda: gold.fetch_bars(symbol, rng, interval)[0])


def resample(bars, hours):
    """Fixed UTC blocks of N hours. The last block may still be forming."""
    sec = hours * 3600
    out, cur = [], None
    for t, h, l, c, o in bars:
        key = t // sec
        if cur is None or cur[0] != key:
            if cur:
                out.append(tuple(cur[1]))
            cur = [key, [t, h, l, c, o]]
        else:
            b = cur[1]
            b[1], b[2], b[3] = max(b[1], h), min(b[2], l), c
    if cur:
        out.append(tuple(cur[1]))
    return out


def trend_points(bars):
    closes = [b[3] for b in bars]
    last = closes[-1]
    s20, s50, s200 = gold.sma(closes, 20), gold.sma(closes, 50), gold.sma(closes, 200)
    if None in (s20, s50, s200):
        return 2          # not enough bars for a verdict: treat as neutral
    return sum([last > s20, last > s50, last > s200, s50 > s200])


def avg_levels(bars):
    closes = [b[3] for b in bars]
    out = []
    for n in (20, 50):
        v = gold.sma(closes, n)
        if v:
            out.append((v, "%d-bar avg" % n))
    return out


def zone_levels(c, tag):
    return [(z["mid"], "%s zone" % tag) for z in c["sup"] + c["res"]]


def solid(z):
    """A zone with real evidence of its own, not only round numbers."""
    labels = set(z["labels"])
    return z["n"] >= 2 and any(not l.startswith("round") for l in labels)


def analyse(symbol, ttl15=60):
    """Everything the page and the watcher need, from delayed Yahoo bars."""
    daily = get_bars(symbol, "1d", "2y", 1800)
    weekly = get_bars(symbol, "1wk", "5y", 3600)
    h1 = get_bars(symbol, "60m", "6mo", 300)
    m15 = get_bars(symbol, "15m", "30d", ttl15)
    if min(len(daily), len(weekly), len(h1), len(m15)) < 60:
        raise SystemExit("error: not enough bars returned for one of the timeframes")
    bars = {"15m": m15, "1h": h1, "4h": resample(h1, 4), "1d": daily, "1w": weekly}

    own, setup = {}, {}
    dwin, dpts, dextra = gold.short_term_inputs(daily)
    own["1d"] = setup["1d"] = gold.compute_setups(dwin, dpts, 5, 120, dextra, 100)
    wpts, wextra = gold.long_term_inputs(weekly)
    own["1w"] = setup["1w"] = gold.compute_setups(weekly, wpts, 4, 104, wextra, 250)

    prev = daily[-2] if len(daily) > 1 else daily[-1]
    prev_levels = [(prev[1], "prev day high"), (prev[2], "prev day low"), (prev[3], "prev day close")]
    higher = {"4h": ("1d", "1w"), "1h": ("4h", "1d", "1w"), "15m": ("1h", "4h", "1d", "1w")}
    for tf in ("4h", "1h", "15m"):                 # high to low, so each can use the ones above
        cfg = CFG[tf]
        b = bars[tf]
        pts = trend_points(b)
        base = avg_levels(b) + ([] if tf == "4h" else prev_levels)
        own[tf] = gold.compute_setups(b, pts, cfg["k"], cfg["win"], base, cfg["step"])
        inject = []
        for h in higher[tf]:
            src = own[h] if h in ("1d", "1w") else own[h]
            inject += zone_levels(src, h)
        setup[tf] = gold.compute_setups(b, pts, cfg["k"], cfg["win"], base + inject, cfg["step"],
                                        min_risk_atr=1.0)

    price = m15[-1][3]
    levels = confluence(own, price, setup["1h"]["atr"])
    for tf, unit in (("15m", "15m"), ("1h", "1h"), ("4h", "4h"), ("1d", "daily"), ("1w", "weekly")):
        gold.attach_plans(setup[tf], unit, levels, 1.0 if tf in INTRADAY else 0.0)
    return dict(symbol=symbol, price=price, bars=bars, own=own, setup=setup, levels=levels,
                asof=m15[-1][0])


def confluence(own, price, atr_ref):
    """Tight price areas where individual levels (swings, Fibonacci, averages, pivots) from
    two or more timeframes line up. Round numbers are left out: they exist on every chart."""
    items = set()
    for tf, c in own.items():
        for p, lab in c["levels"]:
            if lab.startswith("round") or abs(p - price) / price > 0.05:
                continue
            src = "1d" if lab.startswith("prev day") else tf      # a daily level, whichever chart shows it
            items.add((round(p, 2), src, "%s %s" % (src, lab)))
    items = sorted(items)
    tol = 0.5 * atr_ref                       # levels this close count as the same area
    clusters = []
    for p, tf, lab in items:
        if clusters and p - clusters[-1]["parts"][-1][0] <= tol and p - clusters[-1]["parts"][0][0] <= 2 * tol:
            clusters[-1]["parts"].append((p, tf, lab))
        else:
            clusters.append(dict(parts=[(p, tf, lab)]))
    out = []
    for c in clusters:
        parts = c["parts"]
        tfs = sorted({t for _, t, _ in parts}, key=ALL_TF.index)
        lo, hi = min(p for p, _, _ in parts), max(p for p, _, _ in parts)
        out.append(dict(lo=lo, hi=hi, mid=(lo + hi) / 2, tfs=tfs, count=len(parts),
                        score=sum(WEIGHT[t] for t in tfs),
                        kind="confluence" if len(tfs) >= 2 else "single",
                        signals=[lab for _, _, lab in parts][:6]))
    return out


def key_levels(res):
    """Levels worth alerting on: 2+ timeframes agree, or 3+ levels stack on one 1h-or-higher chart."""
    return [l for l in res["levels"]
            if l["kind"] == "confluence" or (l["tfs"][0] in ("1h", "4h", "1d", "1w") and l["count"] >= 3)]


def distance(price, lo, hi):
    if lo <= price <= hi:
        return 0.0
    return min(abs(price - lo), abs(price - hi))


def level_dict(l, price):
    return dict(lo=l["lo"], hi=l["hi"], mid=l["mid"], tfs=l["tfs"], score=l["score"], kind=l["kind"],
                count=l["count"], signals=l["signals"], dist_pct=(l["mid"] / price - 1) * 100)


def split_levels(res, only_key=True):
    price = res["price"]
    lv = key_levels(res) if only_key else res["levels"]
    sup = sorted([l for l in lv if l["mid"] <= price], key=lambda l: -l["mid"])[:4]
    rs = sorted([l for l in lv if l["mid"] > price], key=lambda l: l["mid"])[:4]
    return [level_dict(l, price) for l in sup], [level_dict(l, price) for l in rs]


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Multi-timeframe gold levels and confluence.")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = analyse(a.symbol)
    sup, rs = split_levels(res, only_key=False)
    if a.json:
        print(json.dumps(dict(price=res["price"], support=sup, resistance=rs), indent=1))
        return 0
    print("%s  %s  (delayed)" % (a.symbol, gold.fmt(res["price"], 2, "$")))
    for tf in INTRADAY:
        c = res["setup"][tf]
        print("  %-3s bias %-8s ATR %s" % (tf, c["bias"], gold.fmt(c["atr"], 1, "$")))
    print("Confluence above:")
    for l in reversed(rs):
        print("  %s-%s  %s  [%s]" % (gold.fmt(l["lo"], 0, "$"), gold.fmt(l["hi"], 0, "$"), "+".join(l["tfs"]), ", ".join(l["signals"])))
    print("Confluence below:")
    for l in sup:
        print("  %s-%s  %s  [%s]" % (gold.fmt(l["lo"], 0, "$"), gold.fmt(l["hi"], 0, "$"), "+".join(l["tfs"]), ", ".join(l["signals"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
