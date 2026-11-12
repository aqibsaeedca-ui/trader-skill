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


def fetch_bars(symbol, years="2y"):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
        urllib.parse.quote(symbol, safe="="), years)
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
    for t, h, l, c in zip(ts, q["high"], q["low"], q["close"]):
        if None not in (h, l, c):
            bars.append((t, float(h), float(l), float(c)))
    return bars, res.get("meta", {})


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        _, h, l, _ = bars[i]
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


def report(symbol, a, meta, args):
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
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.risk_pct <= 0 or args.risk_pct > 100:
        raise SystemExit("error: --risk-pct must be between 0 and 100")
    bars, meta = fetch_bars(args.symbol)
    if len(bars) < 60:
        raise SystemExit("error: only %d bars returned; need at least 60" % len(bars))
    a = analyse(bars)
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        print(report(args.symbol, a, meta, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
