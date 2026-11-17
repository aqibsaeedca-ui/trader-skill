"""Backtest of EVERY recommendation the console gives. Standard library only. Read only: no orders, no accounts.

It walks through history one decision point at a time. At each point it rebuilds the analysis from ONLY the bars that were fully closed at
that moment, using exactly the code the live Gold and Bitcoin pages use, records the plans the page would have ranked in its top three, and then
grades each plan with the same conservative rules as the live scorecard (see scorecard.py): a limit entry needs a tick of penetration, a breakout
needs a closing candle and fills at the next open, a stop wins any same-bar tie, gaps exit at the open, the 50/30/20 split with a breakeven stop
after target 1, and costs are charged.

Nothing is tuned on these results, so this is a fair test of the rules as they stand, not a fit to history. Limits you should know:
  * Yahoo keeps only about 30 days of 15 minute bars, 6 months of 1 hour bars, 2 years of daily and 5 years of weekly bars. Older decision points
    have no 15 minute chart, so they produce 1h, 4h, daily and weekly plans only, and their confluence lacks the 15 minute levels.
  * Plans from neighbouring decision points overlap in time, so they are NOT independent trades. The 95% bands assume independence and are
    therefore too narrow. Treat them as a floor on the uncertainty.
  * Delayed last-trade bars, no real bid and ask, flat per-unit cost.
Past results do not predict future ones. Not financial advice.

    python webui/recommendation_backtest.py --json
"""
import argparse
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scorecard as sc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "trader", "recommendation_backtest.json")
OPPS = os.path.join(ROOT, "trader", "replay_opportunities.json")
ALL = os.path.join(ROOT, "trader", "replay_all.json")
DEFAULT_SYMBOLS = ("GC=F", "BTC-USD")
MIN_BARS = 60


def _upto(bars, times, T, step):
    """Bars that were fully closed at time T (a bar starting at t closes at t + step)."""
    return bars[:bisect.bisect_right(times, T - step)]


def decision_points(m15, h1, every=4):
    """(time, has_15m) pairs. With 15 minute history: every `every`-th 15m close. Before it: every `every`-th 1h close."""
    pts = []
    first15 = m15[MIN_BARS][0] + 900 if len(m15) > MIN_BARS else None
    for i in range(MIN_BARS, len(h1) - 1, every):                  # the last bar of each series may still be forming, so it is never used
        T = h1[i][0] + 3600
        if first15 is None or T < first15:
            pts.append((T, False))
    for i in range(MIN_BARS, len(m15) - 1, every):
        pts.append((m15[i][0] + 900, True))
    return pts


def run_symbol(symbol, d, w, h1, m15, every=4, analyse_bars=None, now=None, bars_out=None):
    """Replay one market. Returns (records, coverage). Bars are tuples (t, high, low, close, open)."""
    import gold_intraday as gi
    if analyse_bars is None:
        analyse_bars = gi.analyse_bars
    td, tw, th, tm = ([b[0] for b in x] for x in (d, w, h1, m15))
    recs, used, skipped, index = [], 0, 0, {}
    pts = decision_points(m15, h1, every)
    for T, with15 in pts:
        dd, ww, hh = _upto(d, td, T, 86400), _upto(w, tw, T, 604800), _upto(h1, th, T, 3600)
        mm = _upto(m15, tm, T, 900) if with15 else []
        if min(len(dd), len(ww), len(hh)) < MIN_BARS or (with15 and len(mm) < MIN_BARS):
            skipped += 1
            continue
        try:
            res = analyse_bars(symbol, dd, ww, hh, mm)
        except (Exception, SystemExit):
            skipped += 1
            continue
        used += 1
        for tf in ("15m", "1h", "4h", "1d", "1w"):
            sec = res["setup"].get(tf)
            if sec and sec.get("plans"):
                sc.record(recs, symbol, tf, sec["plans"], T, source="replay", index=index)
    if now is None:                         # the newest bar of each series may be forming: treat 'now' as the START of the newest finest bar
        now = (m15 or h1)[-1][0]
    bars = {"15m": m15, "1h": h1, "4h": gi.resample(h1, 4), "daily": d, "weekly": w}
    times = {k: [b[0] for b in v] for k, v in bars.items()}
    for r in recs:                          # grade on the slice that starts at the recommendation, so each plan only scans the bars after it
        i = bisect.bisect_left(times[r["tf"]], r["created_ts"])
        r["result"] = sc.grade(r, bars[r["tf"]][i:], now)
    if bars_out is not None:
        bars_out[symbol] = bars
    cov = dict(symbol=symbol, decision_points=used, skipped=skipped, first=sc._iso(pts[0][0]) if pts else None, last=sc._iso(pts[-1][0]) if pts else None,
               plans=len(recs))
    return recs, cov


def document(recs, covs, now):
    d = sc.document(recs, now)
    d["rows"] = d["rows"][:120]
    d.update(kind="replay", coverage=covs,
             caution=("This is a replay of the rules as they stand, not a fit to history. Plans from nearby decision points overlap, so they are not independent "
                      "trades and the 95% bands are too narrow. Yahoo limits how far back each chart goes (15 minute: about 30 days, 1 hour: 6 months), "
                      "so most 4 hour, daily and weekly plans have few resolved results. Delayed last-trade bars, flat costs. Past results are not a promise. Not financial advice."),
             method=("At every decision point the analysis was rebuilt from only the bars closed at that moment, using the same code as the live pages, and the plans the page "
                     "would have ranked in its top three were graded with the live scorecard's conservative rules: limit entries need a tick of penetration, breakouts need a "
                     "closing candle and fill at the next open, a stop wins any same-bar tie, gaps exit at the open, 50/30/20 targets with a breakeven stop after target 1, costs charged."))
    return d


def save_doc(doc, path=None):
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, p)


def load_doc(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("kind") == "replay" else None
    except (OSError, ValueError):
        return None


def run(symbols=DEFAULT_SYMBOLS, every=4, fetch=None, path=None, analyse_bars=None):
    now = time.time()
    if fetch is None:
        import gold_intraday as gi

        def fetch(s):
            return (gi.get_bars(s, "1d", "2y", 1800), gi.get_bars(s, "1wk", "5y", 3600), gi.get_bars(s, "60m", "6mo", 300), gi.get_bars(s, "15m", "30d", 60))
    allrecs, covs, errors, all_bars = [], [], [], {}
    for s in symbols:
        try:
            d, w, h1, m15 = fetch(s)
            recs, cov = run_symbol(s, d, w, h1, m15, every, analyse_bars, bars_out=all_bars)
        except (Exception, SystemExit) as e:
            errors.append("%s: %s" % (s, str(e)[:160]))
            continue
        allrecs += recs
        covs.append(cov)
    doc = document(allrecs, covs, now)
    if allrecs:                             # why the replayed recommendations lost, from recorded evidence only
        try:
            import loss_attribution as la
            import macro
            doc["attribution"] = la.build(allrecs, all_bars, macro.load_store(), "historical replay")
            first = {}
            for x in sorted(allrecs, key=lambda y: y["created_ts"]):      # the first recommendation of each opportunity represents it (used by the experiments)
                first.setdefault(x.get("opportunity"), x)
            if path is None:
                os.makedirs(os.path.dirname(OPPS), exist_ok=True)
                with open(OPPS + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(list(first.values()), f)
                os.replace(OPPS + ".tmp", OPPS)
                slim = [dict(id=x["id"], opportunity=x.get("opportunity"), created_ts=x["created_ts"], symbol=x["symbol"], tf=x["tf"], kind=x["kind"], side=x["side"], state=(x.get("result") or {}).get("state"),
                             fill_time=(x.get("result") or {}).get("fill_time"), exit_time=(x.get("result") or {}).get("exit_time"), r_net=(x.get("result") or {}).get("r_net"),
                             cost_r=((x["cost"] / (x["result"]["risk"])) if (x.get("result") or {}).get("risk") else None), final=(x.get("result") or {}).get("final")) for x in allrecs]
                with open(ALL + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(slim, f)
                os.replace(ALL + ".tmp", ALL)
        except Exception as e:
            doc["attribution_error"] = str(e)[:200]
    doc["errors"] = errors
    doc["seconds"] = round(time.time() - now, 1)
    if covs:
        save_doc(doc, path)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest every recommendation the console gives.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--every", type=int, default=4, help="decision point every N bars (default 4: hourly on the 15 minute chart)")
    ap.add_argument("--symbol", action="append", help="repeat for several; default Gold and Bitcoin")
    a = ap.parse_args(argv)
    doc = run(tuple(a.symbol) if a.symbol else DEFAULT_SYMBOLS, max(1, a.every))
    if a.json:
        print(json.dumps(doc))
    else:
        s = doc["summary"]["all"]
        print("REPLAY: %d plans, %d triggered, %d won, %d lost (%s)" % (s["total"], s["triggered"], s["counts"]["WIN"], s["counts"]["LOSS"], doc["checked"]))
    return 0 if not doc["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
