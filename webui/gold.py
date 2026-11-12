#!/usr/bin/env python3
"""Gold assessment: trend, volatility, levels and risk-based position size.

Standard library only. Reads delayed daily bars from Yahoo's unofficial chart
endpoint (the same one trader-alerts uses). Read-only: it never places or
suggests an order. Statistics describe the past; they do not predict price.

    python webui/gold.py --symbol GC=F --account 10000 --risk-pct 1
"""
import argparse
import json
import math
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "trader-skill-gold/1.0 (+local research tool)"
# contract size in troy ounces for the common gold futures
OUNCES = {"GC=F": 100, "MGC=F": 10, "XAUUSD=X": 1}


def fetch_bars(symbol, years="2y", interval="1d"):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(symbol, safe="="), years, interval)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise SystemExit("error: Yahoo answered HTTP %d for %s (gold futures are GC=F)" % (e.code, symbol))
    except (urllib.error.URLError, ValueError) as e:
        raise SystemExit("error: could not read Yahoo data: %s" % e)
    try:
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit("error: Yahoo returned no data for %s" % symbol)
    bars = []
    for t, h, l, c, o in zip(ts, q["high"], q["low"], q["close"], q["open"]):
        if None not in (h, l, c, o):
            bars.append((t, float(h), float(l), float(c), float(o)))   # index 4 = open
    return bars, res.get("meta", {})


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l = bars[i][1], bars[i][2]
        pc = bars[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n if len(trs) >= n else None


def daily_returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def ann_vol(rets):
    return statistics.pstdev(rets) * math.sqrt(252) * 100 if len(rets) > 2 else None


def ordinal(x):
    if x is None:
        return "n/a"
    n = int(round(x))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


def pct(a, b):
    return (a / b - 1) * 100 if b else None


def fmt(x, nd=2, pre="", suf=""):
    return "n/a" if x is None else "%s%s%s" % (pre, format(x, ",.%df" % nd), suf)


def analyse(bars):
    closes = [b[3] for b in bars]
    last = closes[-1]
    out = {"last": last, "bars": len(bars)}
    for n in (20, 50, 200):
        out["sma%d" % n] = sma(closes, n)
    out["atr14"] = atr(bars)
    rets = daily_returns(closes)
    out["vol20"] = ann_vol(rets[-20:])
    out["vol1y"] = ann_vol(rets[-252:])
    # where today's 20-day vol sits among every 20-day window in the sample
    wins = [ann_vol(rets[i - 20:i]) for i in range(20, len(rets) + 1)]
    wins = [w for w in wins if w is not None]
    out["vol_rank"] = (sum(1 for w in wins if w <= out["vol20"]) / len(wins) * 100) if wins and out["vol20"] else None
    yr = bars[-252:]
    out["hi52"], out["lo52"] = max(b[1] for b in yr), min(b[2] for b in yr)
    out["hi20"], out["lo20"] = max(b[1] for b in bars[-20:]), min(b[2] for b in bars[-20:])
    out["hi50"], out["lo50"] = max(b[1] for b in bars[-50:]), min(b[2] for b in bars[-50:])
    out["ret"] = {lab: (pct(last, closes[-1 - n]) if len(closes) > n else None)
                  for lab, n in (("1 month", 21), ("3 months", 63), ("6 months", 126), ("12 months", 252))}
    out["dd"] = pct(last, out["hi52"])
    return out


def trend_read(a):
    last, s20, s50, s200 = a["last"], a["sma20"], a["sma50"], a["sma200"]
    if None in (s50, s200):
        return "not enough history for a trend read"
    pts = sum([last > s20 if s20 else 0, last > s50, last > s200, s50 > s200])
    label = {4: "strong uptrend", 3: "uptrend", 2: "mixed / range-bound",
             1: "downtrend", 0: "strong downtrend"}[pts]
    return "%s (%d of 4 checks bullish: price>20d, price>50d, price>200d, 50d>200d)" % (label, pts)


# ---------------------------------------------------------------- trade zones

def swings(bars, k):
    """Fractal swing highs/lows: a bar whose high (low) beats k bars each side."""
    out = []
    for i in range(k, len(bars) - k):
        h, l = bars[i][1], bars[i][2]
        around = [j for j in range(i - k, i + k + 1) if j != i]
        if all(h >= bars[j][1] for j in around):
            out.append((i, "swing high", h))
        if all(l <= bars[j][2] for j in around):
            out.append((i, "swing low", l))
    return out


def fib_levels(bars):
    """Retracements of the leg between the window's highest high and lowest low."""
    hi_i = max(range(len(bars)), key=lambda i: bars[i][1])
    lo_i = min(range(len(bars)), key=lambda i: bars[i][2])
    hi, lo = bars[hi_i][1], bars[lo_i][2]
    out = []
    for r in (0.382, 0.5, 0.618):
        price = hi - r * (hi - lo) if lo_i < hi_i else lo + r * (hi - lo)
        out.append((price, "fib %s%%" % ("%g" % (r * 100))))
    return out


def make_zones(levels, tol, cap):
    """Group nearby levels into zones (no wider than cap); more distinct levels = more confluence."""
    zones = []
    for price, label in sorted(levels):
        if zones and price - zones[-1]["hi"] <= tol and price - zones[-1]["lo"] <= cap:
            z = zones[-1]
            z["hi"] = price
            z["labels"].append(label)
        else:
            zones.append({"lo": price, "hi": price, "labels": [label]})
    for z in zones:
        z["mid"] = (z["lo"] + z["hi"]) / 2
        z["n"] = len(set(z["labels"]))
    return zones


def split_zones(zones, last, count=3):
    sup = sorted([z for z in zones if z["mid"] <= last], key=lambda z: -z["mid"])[:count]
    res = sorted([z for z in zones if z["mid"] > last], key=lambda z: z["mid"])[:count]
    return sup, res


def zone_text(z, last, atr):
    labels = sorted(set(z["labels"]))
    shown = ", ".join(labels[:4]) + (" +%d" % (len(labels) - 4) if len(labels) > 4 else "")
    rng = "%s - %s" % (fmt(z["lo"], 0, "$"), fmt(z["hi"], 0, "$")) if z["hi"] - z["lo"] >= 1 else fmt(z["mid"], 0, "$")
    return "%-17s %s (%s)  %s from price, %.1f ATR   [%s]" % (
        rng, "*" * min(z["n"], 5), "%d level%s" % (z["n"], "" if z["n"] == 1 else "s"),
        fmt(pct(z["mid"], last), 1, suf="%"), abs(z["mid"] - last) / atr, shown)


def rr_text(entry, stop, targets):
    risk = abs(entry - stop)
    parts = []
    for t in targets:
        r = abs(t - entry) / risk if risk else 0
        parts.append("%s = %.1fR%s" % (fmt(t, 0, "$"), r, "" if r >= 1.5 else " (thin)"))
    return ", ".join(parts) if parts else "no level found beyond"


def size_text(entry, stop, args, symbol):
    if not args.account:
        return None
    risk_usd = args.account * args.risk_pct / 100
    oz = risk_usd / abs(entry - stop)
    lot = OUNCES.get(symbol.upper())
    s = "%s oz risks %s" % (fmt(oz, 1), fmt(risk_usd, 0, "$"))
    if lot:
        s += " (%.2f x %s; %d whole, %d micro)" % (oz / lot, symbol.upper(), int(oz // lot), int(oz // 10))
        if oz < 10:
            one = 10 * abs(entry - stop)
            s += ". One micro (10 oz) would risk %s = %s of the account" % (fmt(one, 0, "$"), fmt(one / args.account * 100, 1, suf="%"))
    # what fits a small account: unleveraged gold (ETF / fractional shares) and the smallest spot lot
    stop_pct = abs(entry - stop) / entry
    pos = risk_usd / stop_pct
    unlev = "unleveraged (gold ETF/fractional): hold %s = %s of account" % (fmt(pos, 0, "$"), fmt(pos / args.account * 100, 0, suf="%"))
    if pos > args.account:
        unlev = "unleveraged (gold ETF): the stop is so tight that sizing to risk exceeds the account; capped at the whole account = risk %s" % fmt(args.account * stop_pct, 0, "$")
    one_oz = abs(entry - stop)
    s += "\n       Small-account fit    %s; smallest spot lot (1 oz) risks %s = %s" % (
        unlev, fmt(one_oz, 0, "$"), fmt(one_oz / args.account * 100, 1, suf="%"))
    return s


def compute_setups(bars, trend_pts, k, fib_window, extra_levels, step, min_risk_atr=0.0):
    """Zones and the four scenarios, using only the bars given (no look-ahead).
    min_risk_atr > 0 pushes any stop at least that many ATRs from the entry (used intraday,
    where a stop inside normal noise is just a donation)."""
    last = bars[-1][3]
    atr_v = atr(bars)
    levels = [(p, lab) for _, lab, p in swings(bars[-fib_window:], k)]
    levels += fib_levels(bars[-fib_window:])
    levels += extra_levels
    base = int(last // step) * step
    for m in range(-3, 5):
        levels.append((base + m * step, "round $%d" % (base + m * step)))
    lo_b, hi_b = last * 0.85, last * 1.15
    levels = [(p, l) for p, l in levels if lo_b <= p <= hi_b]
    zones = make_zones(levels, 0.6 * atr_v, 1.2 * atr_v)
    sup, res = split_zones(zones, last)

    def best(zs):
        """Nearest zone with at least two agreeing levels, else just the nearest."""
        for z in zs:
            if z["n"] >= 2:
                return z
        return zs[0] if zs else None
    bsup, bres = best(sup), best(res)
    bias = {4: "bullish", 3: "bullish", 2: "neutral", 1: "bearish", 0: "bearish"}[trend_pts]

    def trend_of(side):
        if bias == "neutral":
            return "range"
        return "with" if (side == "long") == (bias == "bullish") else "counter"

    setups = []

    def setup(name, side, kind, level, stop, targets, z):
        if min_risk_atr:
            sgn = 1 if side == "long" else -1
            if (level - stop) * sgn < min_risk_atr * atr_v:
                stop = level - sgn * min_risk_atr * atr_v
        setups.append(dict(name=name, side=side, kind=kind, level=level, stop=stop, targets=targets,
                           trend=trend_of(side), weak=z["n"] < 2, zone=z))

    if bsup:
        z = bsup
        setup("LONG on a pullback", "long", "pullback", z["mid"], z["lo"] - 0.5 * atr_v,
              [r["mid"] for r in res[:2]], z)
    if bres:
        z = bres
        setup("LONG on a breakout", "long", "breakout", z["hi"] + 0.25 * atr_v, z["lo"] - 0.25 * atr_v,
              [r["mid"] for r in res if r["mid"] > z["mid"]][:1], z)
        setup("SHORT on a rally into resistance", "short", "pullback", z["mid"], z["hi"] + 0.5 * atr_v,
              [s["mid"] for s in sup[:2]], z)
    if bsup:
        z = bsup
        setup("SHORT on a breakdown", "short", "breakout", z["lo"] - 0.25 * atr_v, z["hi"] + 0.25 * atr_v,
              [s["mid"] for s in sup if s["mid"] < z["mid"]][:1], z)
    return dict(last=last, atr=atr_v, bias=bias, sup=sup, res=res, setups=setups, levels=levels)


def wants(args, part):
    """part is 'intraday', 'short' (daily) or 'long' (weekly)."""
    return args.horizon in ("all", "both", part) and not (args.horizon == "both" and part == "intraday")


_INTRA = {}


def intraday_result(symbol):
    if symbol not in _INTRA:
        import gold_intraday
        _INTRA[symbol] = gold_intraday.analyse(symbol)
    return _INTRA[symbol]


def intraday_report(symbol, args):
    import gold_intraday as gi
    res = intraday_result(symbol)
    sup, rs = gi.split_levels(res, only_key=True)
    L = ["KEY LEVELS WHERE TIMEFRAMES AGREE (price %s)" % fmt(res["price"], 2, "$")]
    for title, rows in (("  Above:", list(reversed(rs))), ("  Below:", sup)):
        L.append(title)
        for l in rows:
            rng = "%s - %s" % (fmt(l["lo"], 0, "$"), fmt(l["hi"], 0, "$")) if l["hi"] - l["lo"] >= 1 else fmt(l["mid"], 0, "$")
            L.append("    %-17s %-15s %s from price  [%s]" % (rng, "+".join(l["tfs"]), fmt(l["dist_pct"], 2, suf="%"),
                                                              ", ".join(l["signals"][:4])))
        if not rows:
            L.append("    none within 5%")
    L.append("")
    for tf in gi.INTRADAY:
        L += horizon_section("%s SETUPS (%s chart, hours to days)" % (gi.CFG[tf]["name"].upper(), tf), tf,
                             res["bars"][tf], 0, 0, 0, [], 0, args, symbol, c=res["setup"][tf])
        L.append("")
    return L


def horizon_section(title, unit, bars, trend_pts, k, fib_window, extra_levels, step, args, symbol, c=None):
    if c is None:
        c = compute_setups(bars, trend_pts, k, fib_window, extra_levels, step)
    last, atr_v, bias, sup, res = c["last"], c["atr"], c["bias"], c["sup"], c["res"]
    L = []
    add = L.append
    add(title)
    add("  Bias %s, 1 %s ATR = %s (%s of price). Price %s." % (bias, unit, fmt(atr_v, 0, "$"), fmt(atr_v / last * 100, 1, suf="%"), fmt(last, 2, "$")))
    add("  Resistance zones above (more * = more levels agree):")
    for z in reversed(res):
        add("    " + zone_text(z, last, atr_v))
    if not res:
        add("    none found within 15%")
    add("  Support zones below:")
    for z in sup:
        add("    " + zone_text(z, last, atr_v))
    if not sup:
        add("    none found within 15%")
    add("")
    add("  Scenarios (conditions, not predictions; act only if price actually gets there and you agree):")
    note_for = {"with": "with the trend", "counter": "counter-trend, higher risk", "range": "range / no clear trend"}
    for s in c["setups"]:
        z = s["zone"]
        note = "; ".join(n for n in (note_for[s["trend"]], "weak zone, 1 level" if s["weak"] else "") if n)
        add("   - %s%s" % (s["name"], "  [" + note + "]" if note else ""))
        if s["kind"] == "pullback":
            who = "buyers " if s["side"] == "long" else "sellers"
            add("       Zone to watch for %s  %s - %s" % (who, fmt(z["lo"], 0, "$"), fmt(z["hi"], 0, "$")))
        else:
            add("       Trigger: %s close %s %s" % (unit, "above" if s["side"] == "long" else "below", fmt(s["level"], 0, "$")))
        add("       Invalidation (stop)  %s   |   risk per oz %s" % (fmt(s["stop"], 0, "$"), fmt(abs(s["level"] - s["stop"]), 0, "$")))
        add("       Targets              %s" % rr_text(s["level"], s["stop"], s["targets"]))
        sz = size_text(s["level"], s["stop"], args, symbol)
        if sz:
            add("       Size                 %s" % sz)
    return L


def short_term_inputs(daily):
    """Trend points, 120-bar window and extra levels for the daily scenarios."""
    closes = [b[3] for b in daily]
    last = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    pts = sum([last > s20, last > s50, last > s200, s50 > s200]) if None not in (s20, s50, s200) else 0
    wk = daily[-6:-1]                      # prior 5 sessions as the "week"
    H, Lo, C = max(b[1] for b in wk), min(b[2] for b in wk), wk[-1][3]
    P = (H + Lo + C) / 3
    extra = [(P, "weekly pivot"), (2 * P - Lo, "pivot R1"), (2 * P - H, "pivot S1"),
             (s20, "20d avg"), (s50, "50d avg")]
    return daily[-120:], pts, [(p, l) for p, l in extra if p]


_WEEKLY = {}


def weekly_bars(symbol):
    if symbol not in _WEEKLY:
        _WEEKLY[symbol] = fetch_bars(symbol, "5y", "1wk")[0]
    return _WEEKLY[symbol]


def long_term_inputs(weekly):
    """Trend points and extra levels for the weekly scenarios."""
    wc = [b[3] for b in weekly]
    w10, w40 = sma(wc, 10), sma(wc, 40)
    wlast = wc[-1]
    wpts = 2
    if w10 and w40:
        wpts = sum([wlast > w10, wlast > w40, w10 > w40, wc[-1] > wc[-13] if len(wc) > 13 else False])
    extra = [(p, l) for p, l in ((w10, "10w avg"), (w40, "40w avg (~200d)")) if p]
    extra += [(max(b[1] for b in weekly[-52:]), "52w high"), (min(b[2] for b in weekly[-52:]), "52w low")]
    return wpts, extra


def zone_dict(z, last, atr_v):
    return dict(lo=z["lo"], hi=z["hi"], mid=z["mid"], n=z["n"], labels=sorted(set(z["labels"])),
                dist_pct=pct(z["mid"], last), dist_atr=abs(z["mid"] - last) / atr_v)


def setup_dict(s, args, symbol):
    risk = abs(s["level"] - s["stop"])
    d = dict(name=s["name"], side=s["side"], kind=s["kind"], level=s["level"], stop=s["stop"],
             risk_per_oz=risk, trend=s["trend"], weak=s["weak"],
             zone=[s["zone"]["lo"], s["zone"]["hi"]],
             targets=[dict(price=t, r=abs(t - s["level"]) / risk if risk else 0) for t in s["targets"]])
    if args.account and risk:
        risk_usd = args.account * args.risk_pct / 100
        stop_pct = risk / s["level"]
        etf = min(risk_usd / stop_pct, args.account)
        d["size"] = dict(risk_usd=risk_usd, etf_usd=etf, etf_pct=etf / args.account * 100,
                         etf_capped=risk_usd / stop_pct > args.account, etf_risk=etf * stop_pct,
                         one_oz_risk=risk, one_oz_pct=risk / args.account * 100,
                         micro_risk=10 * risk, micro_pct=10 * risk / args.account * 100)
    return d


def section_dict(c, args, symbol):
    return dict(bias=c["bias"], atr=c["atr"], last=c["last"],
                sup=[zone_dict(z, c["last"], c["atr"]) for z in c["sup"]],
                res=[zone_dict(z, c["last"], c["atr"]) for z in c["res"]],
                setups=[setup_dict(s, args, symbol) for s in c["setups"]])


def build_json(symbol, bars, a, meta, args):
    out = {"symbol": symbol, "currency": meta.get("currency", "USD"), "analysis": a,
           "trend_read": trend_read(a), "account": args.account, "risk_pct": args.risk_pct,
           "horizon": args.horizon}
    if wants(args, "intraday"):
        try:
            import gold_intraday as gi
            res = intraday_result(symbol)
            sup, rs = gi.split_levels(res, only_key=True)
            out["intraday"] = {tf: section_dict(res["setup"][tf], args, symbol) for tf in gi.INTRADAY}
            out["confluence"] = {"price": res["price"], "support": sup, "resistance": rs}
        except SystemExit as e:
            out["intraday_error"] = str(e)
    if wants(args, "short"):
        win, spts, extra = short_term_inputs(bars)
        out["short"] = section_dict(compute_setups(win, spts, 5, 120, extra, 100), args, symbol)
    if wants(args, "long"):
        try:
            weekly = weekly_bars(symbol)
            wpts, extra = long_term_inputs(weekly)
            out["long"] = section_dict(compute_setups(weekly, wpts, 4, 104, extra, 250), args, symbol)
        except SystemExit as e:
            out["long_error"] = str(e)
    extra_txt = zones_report(symbol, bars, a, args) if args.horizon != "none" else None
    out["report"] = report(symbol, a, meta, args, extra_txt)
    return out


def zones_report(symbol, daily, a, args):
    out = []
    pts = 0
    closes = [b[3] for b in daily]
    last = closes[-1]
    s20, s50, s200 = a["sma20"], a["sma50"], a["sma200"]
    if None not in (s20, s50, s200):
        pts = sum([last > s20, last > s50, last > s200, s50 > s200])
    if wants(args, "intraday"):
        try:
            out += intraday_report(symbol, args)
        except SystemExit as e:
            out.append("INTRADAY SETUPS: could not load intraday data (%s)" % e)
            out.append("")
    if wants(args, "short"):
        win, spts, extra = short_term_inputs(daily)
        out += horizon_section("SHORT-TERM ZONES (daily chart, days to a few weeks)", "daily", win, spts, 5, 120,
                               extra, 100, args, symbol)
        out.append("")
    if wants(args, "long"):
        try:
            weekly = weekly_bars(symbol)
        except SystemExit as e:
            out.append("LONG-TERM ZONES: could not load weekly data (%s)" % e)
            return out
        wpts, extra = long_term_inputs(weekly)
        out += horizon_section("LONG-TERM ZONES (weekly chart, weeks to months)", "weekly", weekly, wpts, 4, 104,
                               extra, 250, args, symbol)
        out.append("")
    return out


def report(symbol, a, meta, args, extra=None):
    L = []
    add = L.append
    last = a["last"]
    add("%s  %s  (%d daily bars, delayed)" % (symbol, meta.get("currency", "USD"), a["bars"]))
    add("Last close          %s" % fmt(last, pre="$"))
    add("")
    add("TREND")
    for n in (20, 50, 200):
        s = a["sma%d" % n]
        add("  %3d-day average   %s   price is %s vs it" % (n, fmt(s, pre="$"), fmt(pct(last, s) if s else None, 1, suf="%")))
    add("  Read              %s" % trend_read(a))
    add("  Returns           " + "   ".join("%s %s" % (k, fmt(v, 1, suf="%")) for k, v in a["ret"].items()))
    add("")
    add("VOLATILITY")
    add("  ATR(14)           %s per day  (%s of price)" % (fmt(a["atr14"], pre="$"), fmt(a["atr14"] / last * 100 if a["atr14"] else None, 2, suf="%")))
    add("  20-day vol        %s annualised" % fmt(a["vol20"], 1, suf="%"))
    add("  1-year vol        %s annualised" % fmt(a["vol1y"], 1, suf="%"))
    add("  Vol percentile    %s  (today's 20-day vol vs every 20-day window in this sample; high = unusually jumpy)" % ordinal(a["vol_rank"]))
    add("")
    add("LEVELS")
    add("  20-day range      %s - %s" % (fmt(a["lo20"], pre="$"), fmt(a["hi20"], pre="$")))
    add("  50-day range      %s - %s" % (fmt(a["lo50"], pre="$"), fmt(a["hi50"], pre="$")))
    add("  52-week range     %s - %s" % (fmt(a["lo52"], pre="$"), fmt(a["hi52"], pre="$")))
    add("  From 52w high     %s     From 52w low  %s" % (fmt(a["dd"], 1, suf="%"), fmt(pct(last, a["lo52"]), 1, suf="%")))
    add("")
    oz = OUNCES.get(symbol.upper())
    stop = args.stop
    basis = "your stop"
    if stop is None and a["atr14"]:
        stop = a["atr14"] * args.atr_mult
        basis = "%.1f x ATR(14)" % args.atr_mult
    add("POSITION SIZE (risk-based, long or short)")
    if not args.account or not stop:
        add("  Enter an account size (and optionally a stop distance) to see this.")
    else:
        risk_usd = args.account * args.risk_pct / 100
        units = risk_usd / stop                     # ounces you can hold
        add("  Account %s, risking %s%% = %s if the stop is hit" % (fmt(args.account, 0, "$"), args.risk_pct, fmt(risk_usd, 0, "$")))
        add("  Stop distance     %s per ounce  (%s)" % (fmt(stop, pre="$"), basis))
        add("  Max exposure      %s oz = %s notional (%s of account)" % (fmt(units, 1), fmt(units * last, 0, "$"), fmt(units * last / args.account * 100, 0, suf="%")))
        if oz:
            add("  In contracts      %s x %s (%d oz each)  -> %s contracts, rounded down" % (
                fmt(units / oz, 2), symbol.upper(), oz, int(units // oz)))
            if units < oz:
                add("                    That is under one full %s contract. The micro (MGC=F, 10 oz) would be %d." % (symbol.upper(), int(units // 10)))
        add("  Stop at           long: %s    short: %s" % (fmt(last - stop, pre="$"), fmt(last + stop, pre="$")))
        add("  Note              futures are leveraged; a gap past your stop can lose more than the amount above.")
    add("")
    if extra:
        L.extend(extra)
    add("Zones are built from past swing highs/lows, moving averages, Fibonacci retracements and round numbers.")
    add("Statistics describe the past and do not predict price. This is not financial advice.")
    add("Delayed Yahoo data. Your broker's price is the one that fills.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gold assessment (read-only).")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--account", type=float, default=None, help="account size in dollars")
    ap.add_argument("--risk-pct", type=float, default=1.0, help="percent of account risked per trade")
    ap.add_argument("--stop", type=float, default=None, help="stop distance in dollars per ounce (default: ATR based)")
    ap.add_argument("--atr-mult", type=float, default=2.0)
    ap.add_argument("--horizon", choices=["all", "intraday", "short", "long", "both", "none"], default="all",
                    help="trade-zone sections: intraday (15m/1h/4h), short (daily), long (weekly), all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.risk_pct <= 0 or args.risk_pct > 100:
        raise SystemExit("error: --risk-pct must be between 0 and 100")
    bars, meta = fetch_bars(args.symbol)
    if len(bars) < 60:
        raise SystemExit("error: only %d bars returned; need at least 60" % len(bars))
    a = analyse(bars)
    if args.json:
        print(json.dumps(build_json(args.symbol, bars, a, meta, args), indent=1, allow_nan=False))
    else:
        extra = zones_report(args.symbol, bars, a, args) if args.horizon != "none" else None
        print(report(args.symbol, a, meta, args, extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
