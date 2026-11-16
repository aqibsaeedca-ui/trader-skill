#!/usr/bin/env python3
"""Market data quality checks, provenance records and a small on-disk store. Standard library only.

Bars are tuples (t, high, low, close, open) with t in UTC epoch seconds, the same shape gold.py uses.

Principles:
  * Missing data is reported, never interpolated.
  * Every dataset can carry a provenance record (provider, symbol, interval, fetch time, row count, hash).
  * A provider that cannot supply something (bid/ask, spread, ticks) says so; nothing is invented.
  * Nothing here talks to a broker or holds credentials.

    python webui/marketdata.py check --symbol GC=F --interval 15m
    python webui/marketdata.py check-csv export.csv --interval 5m --utc-offset 2 --point-size 0.01
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600,
                "4h": 14400, "1d": 86400, "1wk": 604800}
DEFAULT_RANGE = {"15m": "30d", "60m": "6mo", "1d": "2y", "1wk": "5y"}
CALENDARS = ("cme_gold", "24x7")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.path.join(ROOT, "trader", "marketdata")


class DataUnavailable(Exception):
    """A provider cannot supply what was asked. The message says what is missing and why."""


def utc_iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ calendars

def market_closed(ts, calendar):
    """True when the market is expected to be closed at this UTC second.
    cme_gold: weekend from Friday 21:00 UTC to Sunday 23:00 UTC, and a daily break 21:00-23:00 UTC.
    The windows are wide on purpose so US daylight saving never makes an open hour look closed."""
    if calendar == "24x7":
        return False
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    hm = d.hour + d.minute / 60.0
    wd = d.weekday()                      # Monday = 0
    if wd == 5:
        return True
    if wd == 4 and hm >= 21:
        return True
    if wd == 6 and hm < 23:
        return True
    return 21 <= hm < 23


def _slot_closed(ts, step, calendar):
    if step < 86400:
        return market_closed(ts, calendar)
    if step == 86400:
        return calendar != "24x7" and dt.datetime.fromtimestamp(ts, dt.timezone.utc).weekday() >= 5
    return False


# ------------------------------------------------------------------ checks

def dedupe_bars(bars):
    """Sort by time and keep the LAST of any duplicate timestamps. Returns (bars, removed_timestamps).
    The removal is reported so it is never silent."""
    last = {}
    removed = []
    for b in bars:
        if b[0] in last:
            removed.append(b[0])
        last[b[0]] = b
    return [last[t] for t in sorted(last)], removed


def completed(bars, step, now=None):
    """Drop the final bar if its period has not finished yet. Returns (bars, dropped_bool).
    Signals should use only closed bars, or they can change while the bar develops."""
    now = time.time() if now is None else now
    if bars and bars[-1][0] + step > now:
        return list(bars[:-1]), True
    return list(bars), False


def validate_bars(bars, interval, calendar="cme_gold", now=None, max_listed_gaps=20):
    """Quality report for one series. Does not change the data."""
    if interval not in INTERVAL_SEC:
        raise ValueError("unknown interval %r" % interval)
    step = INTERVAL_SEC[interval]
    now = time.time() if now is None else now
    issues = []
    n = len(bars)
    rep = dict(interval=interval, calendar=calendar, bars=n, first=None, last=None, duplicates=[], out_of_order=0,
               bad_ohlc=0, zero_range=0, overlaps=0, gaps=[], gap_count=0, unexpected_missing=0, expected_missing=0,
               completeness_pct=None, forming=False, age_sec=None, market_open=None, stale=False,
               status="fail", issues=issues)
    if n == 0:
        issues.append("no bars")
        return rep
    rep["first"], rep["last"] = utc_iso(bars[0][0]), utc_iso(bars[-1][0])
    seen = set()
    for i, (t, h, l, c, o) in enumerate(bars):
        if t in seen:
            rep["duplicates"].append(t)
        seen.add(t)
        if i and t < bars[i - 1][0]:
            rep["out_of_order"] += 1
        vals = (h, l, c, o)
        if any(v is None or (isinstance(v, float) and math.isnan(v)) or v <= 0 for v in vals) \
                or h < l or not (l - 1e-9 <= c <= h + 1e-9) or not (l - 1e-9 <= o <= h + 1e-9):
            rep["bad_ohlc"] += 1
        elif h == l:
            rep["zero_range"] += 1
    ordered = sorted(set(b[0] for b in bars))
    rep["holiday_candidates"] = 0
    rep["last_bar_off_grid"] = False
    for a, b in zip(ordered, ordered[1:]):
        g = b - a
        if g < step:
            if b == ordered[-1]:
                rep["last_bar_off_grid"] = True       # Yahoo stamps the live bar with the quote time
            else:
                rep["overlaps"] += 1
            continue
        if g == step:
            continue
        slots = int(round(g / float(step))) - 1
        if slots <= 0:
            continue
        expected = sum(1 for k in range(1, slots + 1) if _slot_closed(a + k * step, step, calendar))
        rep["expected_missing"] += expected
        if step == 86400 and calendar != "24x7" and slots - expected == 1:
            rep["holiday_candidates"] += 1            # one missing weekday: most likely an exchange holiday
            continue
        rep["unexpected_missing"] += slots - expected
        if slots - expected > 0:
            rep["gap_count"] += 1
            if len(rep["gaps"]) < max_listed_gaps:
                rep["gaps"].append(dict(start=utc_iso(a), end=utc_iso(b), missing=slots - expected))
    total = len(ordered) + rep["unexpected_missing"]
    rep["completeness_pct"] = round(100.0 * len(ordered) / total, 2) if total else None
    last_t = bars[-1][0]
    rep["forming"] = last_t + step > now
    rep["age_sec"] = int(now - last_t)
    rep["market_open"] = not _slot_closed(now, step, calendar)
    allowed = 3 * step if step < 86400 else 5 * 86400 if step == 86400 else 14 * 86400
    rep["stale"] = bool(rep["market_open"] and rep["age_sec"] > allowed)
    if rep["duplicates"]:
        issues.append("%d duplicate timestamp(s)" % len(rep["duplicates"]))
    if rep["out_of_order"]:
        issues.append("%d bar(s) out of order" % rep["out_of_order"])
    if rep["bad_ohlc"]:
        issues.append("%d bar(s) with invalid OHLC" % rep["bad_ohlc"])
    if rep["overlaps"]:
        issues.append("%d bar(s) closer together than the interval" % rep["overlaps"])
    if rep["unexpected_missing"]:
        issues.append("%d missing bar(s) in %d gap(s) outside the normal weekend and daily closures "
                      "(some may be exchange holidays or shortened sessions)" % (rep["unexpected_missing"], rep["gap_count"]))
    if rep["stale"]:
        issues.append("latest bar is %d min old while the market should be open" % (rep["age_sec"] // 60))
    if rep["holiday_candidates"]:
        issues.append("%d single-weekday gap(s) treated as likely exchange holidays (not checked against a holiday calendar)"
                      % rep["holiday_candidates"])
    if rep["last_bar_off_grid"]:
        issues.append("latest bar is stamped with the quote time, not a bar start")
    if rep["forming"]:
        issues.append("latest bar is still forming (excluded from closed-bar signals)")
    hard = rep["out_of_order"] or rep["bad_ohlc"] or rep["stale"]
    soft = rep["duplicates"] or rep["unexpected_missing"] or rep["overlaps"]
    rep["status"] = "fail" if hard else "warn" if soft else "ok"
    return rep


# ------------------------------------------------------------------ provenance

def bars_hash(bars):
    body = json.dumps([[b[0], b[1], b[2], b[3], b[4]] for b in bars], separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def provenance(provider, symbol, interval, bars, fetched_at=None, extra=None):
    """Who supplied this data, when, and exactly which rows (hash)."""
    fetched_at = time.time() if fetched_at is None else fetched_at
    out = dict(provider=provider, symbol=symbol, interval=interval, fetched_at=utc_iso(fetched_at), rows=len(bars),
               first=utc_iso(bars[0][0]) if bars else None, last=utc_iso(bars[-1][0]) if bars else None,
               sha256=bars_hash(bars))
    out.update(extra or {})
    return out


# ------------------------------------------------------------------ providers

class Provider:
    name = "base"
    bid_ask = False
    description = ""

    def fetch(self, symbol, interval, rng):
        raise DataUnavailable("not implemented")


class YahooProvider(Provider):
    name = "yahoo"
    bid_ask = False

    def describe(self, symbol):
        s = ["Yahoo Finance chart API (unofficial, delayed). Last-trade OHLC only: no bid/ask, no spread history, no volume."]
        if symbol.upper().endswith("=F"):
            s.append("%s is the continuous front-month futures contract, not spot XAUUSD: roll gaps are in the prices "
                     "and quotes differ from an MT5 or OANDA XAUUSD feed." % symbol.upper())
        if symbol.upper() == "BTC-USD":
            s.append("BTC-USD is an aggregated spot price, not one exchange's executable quote.")
        return s

    def fetch(self, symbol, interval, rng=None):
        import gold
        rng = rng or DEFAULT_RANGE.get(interval)
        if not rng:
            raise DataUnavailable("Yahoo has no usable %s history in this tool" % interval)
        bars, meta = gold.fetch_bars(symbol, rng, interval)
        return bars, dict(provider=self.name, delayed=True, bid_ask=False, notes=self.describe(symbol),
                          currency=meta.get("currency"), exchange=meta.get("exchangeName"))


class UnconfiguredProvider(Provider):
    """Placeholder for a broker feed that needs the user's own terminal or account. It never guesses."""

    def __init__(self, name, how):
        self.name, self.how = name, how

    def fetch(self, symbol, interval, rng=None):
        raise DataUnavailable("%s is not configured: %s" % (self.name, self.how))


PROVIDERS = {
    "yahoo": YahooProvider(),
    "mt5": UnconfiguredProvider("MetaTrader 5", "export history to CSV and use the CSV importer, or run the MT5 terminal "
                                "and a read-only adapter. Credentials are never stored here."),
    "oanda": UnconfiguredProvider("OANDA", "needs a practice-account token supplied through an environment variable; "
                                  "not implemented yet."),
}

_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


def _parse_time(text, utc_offset_hours):
    text = text.strip()
    if re.fullmatch(r"\d{9,11}", text):
        return int(text)                                  # epoch seconds are already UTC
    for f in _FORMATS:
        try:
            naive = dt.datetime.strptime(text, f)
        except ValueError:
            continue
        # a broker server clock is converted with the offset the user states, never a guess
        return int(naive.replace(tzinfo=dt.timezone.utc).timestamp() - utc_offset_hours * 3600)
    return None


def parse_csv(text, utc_offset_hours, point_size=None):
    """Parse an MT5 history export (tab separated, <DATE> <TIME> <OPEN> ...) or a simple CSV with
    time,open,high,low,close[,spread]. Returns (bars, extras, rejected).
    utc_offset_hours is the broker SERVER clock's offset from UTC and is required.
    extras = {"spread_points": {t: points}, "spread": {t: price units}} (spread only when present).
    rejected = [(line_number, reason)], so no row disappears silently."""
    if utc_offset_hours is None:
        raise DataUnavailable("utc_offset_hours is required: state the broker server clock offset from UTC")
    lines = text.lstrip("﻿").splitlines()
    if not lines:
        raise DataUnavailable("empty file")
    delim = "\t" if "\t" in lines[0] else ","
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    head = [re.sub(r"[<>\s]", "", h).lower() for h in rows[0]]
    idx = {name: head.index(name) for name in head}

    def col(*names):
        for nme in names:
            if nme in idx:
                return idx[nme]
        return None
    ci = dict(date=col("date"), time=col("time", "timestamp", "datetime"), o=col("open", "o"), h=col("high", "h"),
              l=col("low", "l"), c=col("close", "c"), sp=col("spread"))
    if None in (ci["time"], ci["o"], ci["h"], ci["l"], ci["c"]):
        raise DataUnavailable("could not find time, open, high, low, close columns in the header: %s" % rows[0])
    bars, rejected, sp_pts = {}, [], {}
    for n, r in enumerate(rows[1:], start=2):
        if not r or all(not x.strip() for x in r):
            continue
        try:
            tstr = r[ci["time"]] if ci["date"] is None else r[ci["date"]] + " " + r[ci["time"]]
            t = _parse_time(tstr, utc_offset_hours)
            if t is None:
                rejected.append((n, "unreadable time %r" % tstr))
                continue
            o, h, l, c = (float(r[ci[k]]) for k in ("o", "h", "l", "c"))
            if ci["sp"] is not None and r[ci["sp"]].strip() != "":
                sp_pts[t] = float(r[ci["sp"]])
        except (ValueError, IndexError) as e:
            rejected.append((n, "bad row: %s" % e))
            continue
        bars[t] = (t, h, l, c, o)
    out = [bars[t] for t in sorted(bars)]
    extras = {"spread_points": sp_pts}
    if sp_pts and point_size:
        extras["spread"] = {t: v * point_size for t, v in sp_pts.items()}
    return out, extras, rejected


# ------------------------------------------------------------------ on-disk store

class Store:
    """Append-and-reconcile store: bars are merged by timestamp. A bar that a provider later restates is
    recorded in `revisions` (old and new values), never overwritten silently."""

    def __init__(self, root=None):
        self.root = root or STORE_DIR

    def _path(self, provider, symbol, interval):
        safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
        return os.path.join(self.root, safe(provider), "%s_%s.json" % (safe(symbol), safe(interval)))

    def load(self, provider, symbol, interval):
        try:
            with open(self._path(provider, symbol, interval), encoding="utf-8") as f:
                d = json.load(f)
            return [tuple(b) for b in d["bars"]], d.get("meta", {}), d.get("revisions", [])
        except (OSError, ValueError, KeyError):
            return [], {}, []

    def save(self, provider, symbol, interval, bars, prov=None, now=None):
        old, _, revs = self.load(provider, symbol, interval)
        merged = {b[0]: b for b in old}
        step = INTERVAL_SEC.get(interval, 0)
        for b in bars:
            prev = merged.get(b[0])
            if prev is not None and tuple(prev) != tuple(b):
                still_forming = old and b[0] == old[-1][0]
                if not still_forming:                       # a closed bar changed: record it
                    revs.append(dict(t=utc_iso(b[0]), old=list(prev), new=list(b)))
            merged[b[0]] = tuple(b)
        out = [merged[t] for t in sorted(merged)]
        meta = dict(prov or provenance(provider, symbol, interval, out, fetched_at=now))
        meta.update(rows=len(out), first=utc_iso(out[0][0]) if out else None,
                    last=utc_iso(out[-1][0]) if out else None, sha256=bars_hash(out), restated_bars=len(revs))
        path = self._path(provider, symbol, interval)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(meta=meta, bars=[list(b) for b in out], revisions=revs[-200:]), f)
        os.replace(tmp, path)
        return meta


# ------------------------------------------------------------------ report for the Gold page

def calendar_for(symbol):
    import assets
    return assets.profile(symbol)["calendar"]


def quality_report(symbol, series, now=None):
    """series: list of (label, bars, interval). Used by the Gold page. Yahoo is the provider."""
    now = time.time() if now is None else now
    prov = PROVIDERS["yahoo"]
    cal = calendar_for(symbol)
    rows = []
    for label, bars, interval in series:
        v = validate_bars(bars, interval, cal, now)
        rows.append(dict(label=label, interval=interval, bars=v["bars"], first=v["first"], last=v["last"],
                         age_sec=v["age_sec"], completeness_pct=v["completeness_pct"], forming=v["forming"],
                         stale=v["stale"], market_open=v["market_open"], status=v["status"], issues=v["issues"],
                         gaps=v["gaps"][:3], sha256=bars_hash(bars)[:12]))
    worst = "fail" if any(r["status"] == "fail" for r in rows) else "warn" if any(r["status"] == "warn" for r in rows) else "ok"
    return dict(provider="Yahoo Finance (delayed)", bid_ask=False, spread_available=False,
                notes=prov.describe(symbol), calendar=cal, checked=utc_iso(now), status=worst, series=rows)


# ------------------------------------------------------------------ command line

def _print_report(rep, prov=None):
    print("%s  %s bars  %s -> %s" % (rep["interval"], rep["bars"], rep["first"], rep["last"]))
    print("  status %s | completeness %s%% | age %s min | market open %s | latest bar forming %s" % (
        rep["status"].upper(), rep["completeness_pct"], None if rep["age_sec"] is None else rep["age_sec"] // 60,
        rep["market_open"], rep["forming"]))
    for i in rep["issues"]:
        print("  - " + i)
    for g in rep["gaps"][:10]:
        print("  gap %s -> %s (%d missing)" % (g["start"], g["end"], g["missing"]))
    if prov:
        print("  provenance: %s rows, sha256 %s" % (prov["rows"], prov["sha256"][:16]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Market data quality check (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c1 = sub.add_parser("check", help="fetch from Yahoo and validate")
    c1.add_argument("--symbol", default="GC=F")
    c1.add_argument("--interval", default="15m", choices=["15m", "60m", "1d", "1wk"])
    c1.add_argument("--save", action="store_true", help="also merge the bars into trader/marketdata/")
    c1.add_argument("--json", action="store_true")
    c2 = sub.add_parser("check-csv", help="validate an MT5 or generic CSV export")
    c2.add_argument("path")
    c2.add_argument("--interval", required=True, choices=sorted(INTERVAL_SEC))
    c2.add_argument("--utc-offset", type=float, required=True, help="broker server clock offset from UTC, in hours")
    c2.add_argument("--point-size", type=float, default=None)
    c2.add_argument("--calendar", default="cme_gold", choices=CALENDARS)
    c2.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            import assets
            bars, meta = PROVIDERS["yahoo"].fetch(a.symbol, a.interval)
            bars, removed = dedupe_bars(bars)
            rep = validate_bars(bars, a.interval, assets.profile(a.symbol)["calendar"])
            prov = provenance("yahoo", a.symbol, a.interval, bars, extra=dict(notes=meta["notes"]))
            if a.save:
                Store().save("yahoo", a.symbol, a.interval, bars, prov)
            if a.json:
                print(json.dumps(dict(report=rep, provenance=prov, duplicates_removed=len(removed))))
            else:
                print("%s via Yahoo" % a.symbol)
                for n in meta["notes"]:
                    print("  note: " + n)
                _print_report(rep, prov)
                if removed:
                    print("  removed %d duplicate bar(s)" % len(removed))
        else:
            with open(a.path, encoding="utf-8", errors="replace") as f:
                bars, extras, rejected = parse_csv(f.read(), a.utc_offset, a.point_size)
            rep = validate_bars(bars, a.interval, a.calendar)
            prov = provenance("csv", os.path.basename(a.path), a.interval, bars,
                              extra=dict(utc_offset_hours=a.utc_offset, rejected_rows=len(rejected),
                                         spread_points_rows=len(extras["spread_points"])))
            if a.json:
                print(json.dumps(dict(report=rep, provenance=prov, rejected=rejected[:50])))
            else:
                _print_report(rep, prov)
                print("  rejected rows: %d | spread rows: %d" % (len(rejected), len(extras["spread_points"])))
    except DataUnavailable as e:
        raise SystemExit("error: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
