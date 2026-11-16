#!/usr/bin/env python3
"""Economic calendar, observed event reactions and news-filter testing. Standard library only.

Every event records WHEN it was released (UTC), WHERE the date came from and how reliable that source is, and WHEN
we recorded it. Reactions are measured from past price data and reported as observed behaviour. Nothing here
claims that a release causes a move, and a news filter is only recommended if trades near events measurably did
worse in the history, with enough trades to say so.

Sources
  FOMC statements and minutes   official, parsed from federalreserve.gov (run `fetch-fomc`)
  NFP                           derived from the BLS rule (third Friday after the reference week); marked "derived"
                                and meant to be checked. BLS refuses automated downloads, so no scraping is attempted.
  CPI, PCE, anything else       your own CSV (`import-csv`), marked "manual". Columns: datetime_utc,name,type,
                                importance,actual,forecast,previous,source
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "trader", "events", "events.json")
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = "trader-skill-research/1.0 (local research tool)"
RELIABILITY = {"official": 3, "manual": 2, "derived": 1}
TYPES = ("FOMC", "FOMC_MINUTES", "NFP", "CPI", "PCE", "FED_SPEECH", "OTHER")
HIGH = {"FOMC", "NFP", "CPI", "PCE"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                       "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})


# ------------------------------------------------------------------------------------ time zones

def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def us_dst(d):
    """Is US daylight saving time in force on this calendar date? (second Sunday of March to first Sunday of November)"""
    return _nth_weekday(d.year, 3, 6, 2) <= d < _nth_weekday(d.year, 11, 6, 1)


def ny_to_utc(year, month, day, hour, minute=0):
    d = dt.date(year, month, day)
    off = 4 if us_dst(d) else 5
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc).timestamp() + off * 3600)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def make_event(ts, name, typ, source, reliability, importance=None, actual=None, forecast=None, previous=None, notes="", observed_at=None):
    if typ not in TYPES:
        raise ValueError("unknown event type %r" % typ)
    if reliability not in RELIABILITY:
        raise ValueError("unknown reliability %r" % reliability)
    return dict(ts=int(ts), utc=iso(ts), name=name, type=typ, importance=importance or ("high" if typ in HIGH else "medium"),
                source=source, reliability=reliability, actual=actual, forecast=forecast, previous=previous, notes=notes,
                observed_at=iso(observed_at if observed_at is not None else time.time()))


# ------------------------------------------------------------------------------------ sources

def parse_fomc_html(html, observed_at=None):
    """Decision statements (14:00 New York, last day of the meeting) and minutes releases from the Fed calendar page."""
    events = []
    parts = re.split(r"(\d{4}) FOMC Meetings", html)
    for k in range(1, len(parts) - 1, 2):
        year, body = int(parts[k]), parts[k + 1]
        marks = [m for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', body)]
        for n, m in enumerate(marks):
            block = body[m.end(): marks[n + 1].start() if n + 1 < len(marks) else len(body)]
            dm = re.search(r'fomc-meeting__date[^>]*>\s*([^<]+)<', block)
            if not dm:
                continue
            first_month = MONTHS.get(m.group(1).split("/")[0].strip().lower()[:3])
            span = re.match(r"\s*(\d{1,2})\s*-\s*(\d{1,2})", dm.group(1))
            if not first_month or not span:
                continue
            d1, d2 = int(span.group(1)), int(span.group(2))
            month, y = first_month, year
            if d2 < d1:                                       # the meeting crosses a month end
                month += 1
                if month > 12:
                    month, y = 1, y + 1
            if "(unscheduled" in dm.group(1).lower():
                continue
            events.append(make_event(ny_to_utc(y, month, d2, 14), "FOMC rate decision", "FOMC", "federalreserve.gov", "official",
                                     observed_at=observed_at, notes="statement 14:00 New York time"))
            rel = re.search(r"Released\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", block)
            if rel and MONTHS.get(rel.group(1).lower()[:3]):
                events.append(make_event(ny_to_utc(int(rel.group(3)), MONTHS[rel.group(1).lower()[:3]], int(rel.group(2)), 14),
                                         "FOMC minutes", "FOMC_MINUTES", "federalreserve.gov", "official", importance="medium",
                                         observed_at=observed_at))
    return events


def fetch_fomc(opener=None):
    """One request to the Fed's public calendar page. Run only when asked."""
    opener = opener or urllib.request.build_opener()
    req = urllib.request.Request(FED_URL, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit("error: could not read the Fed calendar page (%s)" % type(e).__name__)
    ev = parse_fomc_html(html)
    if not ev:
        raise SystemExit("error: the Fed page layout was not recognised; no events were parsed")
    return ev


def nfp_dates(year_from, year_to, observed_at=None):
    """Employment Situation release dates from the BLS rule: the third Friday after the Saturday that ends the
    reference week (the week containing the 12th). 08:30 New York. Derived, not official: check against bls.gov."""
    out = []
    for y in range(year_from, year_to + 1):
        for m in range(1, 13):
            twelfth = dt.date(y, m, 12)
            sat = twelfth + dt.timedelta(days=(5 - twelfth.weekday()) % 7)
            rel = sat + dt.timedelta(days=20)
            out.append(make_event(ny_to_utc(rel.year, rel.month, rel.day, 8, 30), "Nonfarm payrolls", "NFP", "BLS release rule (derived)",
                                  "derived", observed_at=observed_at, notes="derived from the reference-week rule; verify at bls.gov"))
    return out


def _num(x):
    if x is None or str(x).strip() in ("", "-", "n/a"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def parse_csv(text, observed_at=None):
    """Your own calendar. Returns (events, rejected). Bad rows are listed, never dropped silently."""
    rows = list(csv.DictReader(text.lstrip("﻿").splitlines()))
    events, rejected = [], []
    for n, r in enumerate(rows, start=2):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        raw, name = r.get("datetime_utc", ""), r.get("name", "")
        try:
            if not raw or not name:
                raise ValueError("needs datetime_utc and name")
            ts = int(raw) if re.fullmatch(r"\d{9,11}", raw) else int(dt.datetime.strptime(raw.replace("T", " ").rstrip("Z")[:19], "%Y-%m-%d %H:%M:%S" if len(raw) > 16 else "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp())
            typ = (r.get("type") or "OTHER").upper()
            events.append(make_event(ts, name, typ if typ in TYPES else "OTHER", r.get("source") or "user CSV", "manual",
                                     importance=(r.get("importance") or "").lower() or None, actual=_num(r.get("actual")),
                                     forecast=_num(r.get("forecast")), previous=_num(r.get("previous")), observed_at=observed_at))
        except ValueError as e:
            rejected.append((n, str(e)))
    return events, rejected


# ------------------------------------------------------------------------------------ calendar store

def merge(existing, new):
    """One event per (type, release time). The better source wins; a worse one never replaces a better one."""
    by = {(e["type"], e["ts"]): e for e in existing}
    for e in new:
        k = (e["type"], e["ts"])
        cur = by.get(k)
        if cur is None or RELIABILITY[e["reliability"]] >= RELIABILITY[cur["reliability"]]:
            by[k] = e
    return sorted(by.values(), key=lambda e: e["ts"])


def load_store(path=None):
    try:
        with open(path or STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_store(events, path=None):
    p = path or STORE
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=1)
    os.replace(tmp, p)


def window(events, now, hours_ahead=48, hours_back=24, high_only=False):
    """Events around now: (upcoming, recent). Release times are scheduled in advance, so listing them is allowed."""
    ev = [e for e in events if not high_only or e["importance"] == "high"]
    up = [e for e in ev if now <= e["ts"] <= now + hours_ahead * 3600]
    rc = [e for e in ev if now - hours_back * 3600 <= e["ts"] < now]
    return up, rc


# ------------------------------------------------------------------------------------ observed reactions

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def event_study(bars, events, ks=(1, 3, 6), spread=None):
    """How price behaved after past releases, against how it behaves on any bar. Observed, not causal.

    For an event released at t: the reference is the close of the last bar BEFORE t, the scale is the ATR known just
    before t. For each k, the move is the close k bars later. `excess` compares the mean absolute event move with the
    mean absolute k-bar move over the whole sample. If actual and forecast are given, `surprise_agree` is how often the
    move had the same sign as (actual - forecast); it is a frequency, not a mechanism."""
    if len(bars) < 60:
        return {}
    atr = ind.atr(bars, 14)
    times = [b[0] for b in bars]
    import bisect
    control = {}
    for k in ks:
        mv = [abs(bars[i + k][3] - bars[i][3]) / atr[i] for i in range(14, len(bars) - k) if atr[i]]
        control[k] = sum(mv) / len(mv) if mv else None
    groups = {}
    for e in events:
        j = bisect.bisect_left(times, e["ts"])
        if j < 15 or j + max(ks) >= len(bars) or not atr[j - 1] or times[j] - e["ts"] > 3 * (times[1] - times[0]):
            continue
        base, ref = atr[j - 1], bars[j - 1][3]
        rec = dict(ts=e["ts"], moves={k: (bars[j - 1 + k][3] - ref) / base for k in ks},
                   range_exp={k: (max(b[1] for b in bars[j:j + k]) - min(b[2] for b in bars[j:j + k])) / base for k in ks})
        if spread:
            pre = [spread[t] for t in times[max(0, j - 12):j] if t in spread]
            post = [spread[t] for t in times[j:j + 3] if t in spread]
            if pre and post:
                rec["spread_ratio"] = (sum(post) / len(post)) / (sum(pre) / len(pre))
        if e.get("actual") is not None and e.get("forecast") is not None and e["actual"] != e["forecast"]:
            rec["surprise"] = 1 if e["actual"] > e["forecast"] else -1
        groups.setdefault(e["type"], []).append(rec)
    out = {}
    for typ, recs in groups.items():
        row = dict(n=len(recs), by_k={})
        for k in ks:
            ab = [abs(r["moves"][k]) for r in recs]
            m = sum(ab) / len(ab)
            sd = math.sqrt(sum((x - m) ** 2 for x in ab) / (len(ab) - 1)) if len(ab) > 1 else None
            row["by_k"][k] = dict(mean_abs_move_atr=m, se=(sd / math.sqrt(len(ab)) if sd else None),
                                  excess=(m / control[k] if control[k] else None),
                                  mean_range_exp=sum(r["range_exp"][k] for r in recs) / len(recs),
                                  control_mean_abs_move_atr=control[k])
        sp = [r["spread_ratio"] for r in recs if "spread_ratio" in r]
        row["spread_ratio"] = (sum(sp) / len(sp)) if sp else None
        sur = [r for r in recs if "surprise" in r]
        if sur:
            k = ks[-1]
            agree = sum(1 for r in sur if (r["moves"][k] > 0) == (r["surprise"] > 0))
            row["surprise_agree"] = dict(n=len(sur), agree=agree, rate=agree / len(sur), note="frequency of same sign, not a mechanism")
        row["enough"] = len(recs) >= 20
        out[typ] = row
    return out


def evaluate_news_filter(trades, events, minutes_before=30, minutes_after=60, high_only=True, min_inside=30):
    """Would skipping trades near releases have helped in this history? Compares trades that started inside an event
    window with those outside it. Recommended only if the inside trades are worse AND the difference is statistically
    clear with enough trades; otherwise the answer is that no evidence supports a filter."""
    evs = [e["ts"] for e in events if not high_only or e["importance"] == "high"]

    def inside(t):
        return any(e - minutes_before * 60 <= t["entry_t"] <= e + minutes_after * 60 for e in evs)
    ins = [t["r"] for t in trades if inside(t)]
    out = [t["r"] for t in trades if not inside(t)]

    def ms(v):
        n = len(v)
        m = sum(v) / n if n else None
        s = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else None
        return n, m, s
    n1, m1, s1 = ms(ins)
    n2, m2, s2 = ms(out)
    res = dict(window=dict(before_min=minutes_before, after_min=minutes_after), inside=dict(n=n1, expectancy=m1),
               outside=dict(n=n2, expectancy=m2), recommended=False, p_value=None)
    if n1 < min_inside or n2 < min_inside or not s1 or not s2:
        res["verdict"] = "NO EVIDENCE: only %d trades inside event windows (need %d); no filter applied" % (n1, min_inside)
        return res
    t = (m1 - m2) / math.sqrt(s1 * s1 / n1 + s2 * s2 / n2)
    p = 2 * (1 - _norm_cdf(abs(t)))
    res["p_value"] = p
    res["t"] = t
    if m1 < m2 and p < 0.05 and m1 < 0:
        res["recommended"] = True
        res["verdict"] = "FILTER SUPPORTED by this history: trades near releases averaged %.2fR vs %.2fR elsewhere (p=%.3f); past data only" % (m1, m2, p)
    else:
        res["verdict"] = "NO EVIDENCE a filter helps: inside %.2fR (n=%d) vs outside %.2fR (n=%d), p=%.3f" % (m1, n1, m2, n2, p)
    return res


def summary(now=None, events=None, bars=None, spread=None):
    """What the news page shows: upcoming and recent high-impact releases, and measured past reactions when bars are given."""
    now = time.time() if now is None else now
    ev = load_store() if events is None else events
    up, rc = window(ev, now, 72, 24, high_only=True)
    out = dict(checked=iso(now), have_events=len(ev), upcoming=up, recent=rc,
               sources=sorted({"%s (%s)" % (e["source"], e["reliability"]) for e in ev}),
               note="Release times come from the sources shown; derived and manual dates should be checked. Reactions below are measured "
                    "from past prices and are not proof that a release causes a move.")
    if bars and ev:
        out["study"] = event_study(bars, [e for e in ev if e["ts"] < now], spread=spread)
    return out


# ------------------------------------------------------------------------------------ command line

def _show(ev, now):
    up, rc = window(ev, now, 24 * 14, 24 * 7)
    print("%d events stored. Sources: %s" % (len(ev), ", ".join(sorted({e["source"] + " [" + e["reliability"] + "]" for e in ev})) or "none"))
    for title, rows in (("UPCOMING (14 days)", up), ("RECENT (7 days)", rc)):
        print(title)
        for e in rows:
            print("  %s  %-22s %-6s [%s, %s]" % (e["utc"], e["name"], e["importance"], e["source"], e["reliability"]))
        if not rows:
            print("  none")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Economic calendar (read-only except for its own store).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("fetch-fomc", help="read the Fed's public calendar page")
    sub.add_parser("update", help="official FOMC dates plus derived NFP dates for the last and next year")
    n = sub.add_parser("add-nfp", help="add derived NFP dates")
    n.add_argument("--from-year", type=int, default=dt.date.today().year - 1)
    n.add_argument("--to-year", type=int, default=dt.date.today().year + 1)
    c = sub.add_parser("import-csv")
    c.add_argument("path")
    s = sub.add_parser("study", help="measure past reactions of a market to the stored events")
    s.add_argument("--symbol", default="GC=F")
    s.add_argument("--interval", default="60m", choices=["15m", "60m", "1d"])
    a = ap.parse_args(argv)
    now = time.time()
    ev = load_store()
    if a.cmd == "update":
        new = fetch_fomc()
        yr = dt.date.today().year
        ev = merge(merge(ev, new), nfp_dates(yr - 1, yr + 1))
        save_store(ev)
        print("FOMC: %d official events from federalreserve.gov. NFP: derived dates added and marked derived (verify at bls.gov). "
              "%d events stored. Add CPI and PCE with import-csv." % (len(new), len(ev)))
    elif a.cmd == "fetch-fomc":
        new = fetch_fomc()
        ev = merge(ev, new)
        save_store(ev)
        print("parsed %d FOMC events from federalreserve.gov (official); %d events stored" % (len(new), len(ev)))
    elif a.cmd == "add-nfp":
        ev = merge(ev, nfp_dates(a.from_year, a.to_year))
        save_store(ev)
        print("added derived NFP dates (marked derived: verify at bls.gov); %d events stored" % len(ev))
    elif a.cmd == "import-csv":
        with open(a.path, encoding="utf-8", errors="replace") as f:
            new, rej = parse_csv(f.read())
        ev = merge(ev, new)
        save_store(ev)
        print("imported %d events, rejected %d row(s); %d events stored" % (len(new), len(rej), len(ev)))
        for line, why in rej[:20]:
            print("  line %d: %s" % (line, why))
    elif a.cmd == "study":
        import gold
        import marketdata
        bars, _ = gold.fetch_bars(a.symbol, marketdata.DEFAULT_RANGE[a.interval], a.interval)
        study = event_study(bars[:-1], [e for e in ev if e["ts"] < now])
        print("OBSERVED reactions of %s (%s bars) to stored events. Past data only; not a claim about cause." % (a.symbol, a.interval))
        if not study:
            print("  no stored events fall inside the price history, or there is too little data")
        for typ, row in study.items():
            print("  %-13s n=%d%s" % (typ, row["n"], "" if row["enough"] else "  (too few to rely on)"))
            for k, v in row["by_k"].items():
                print("    after %d bar(s): mean absolute move %.2f ATR vs %.2f on any bar (x%.2f)" % (
                    k, v["mean_abs_move_atr"], v["control_mean_abs_move_atr"], v["excess"] or 0))
    else:
        _show(ev, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
