"""Gold Session Intelligence Engine. Standard library only (zoneinfo needs the `tzdata` package on Windows). Read only: it never places an order.

All internal timestamps are UTC seconds. Session windows are defined in LOCAL time in an IANA zone and converted to UTC for each calendar day, so
the UK and US daylight-saving changes (which happen on different dates) are handled independently and no fixed UTC offset is used anywhere.

Sessions (local windows, configurable in WINDOWS):
  ASIA      Asia/Tokyo        09:00 to 15:00   (the Tokyo day session; Tokyo has no daylight saving)
  LONDON    Europe/London     08:00 to 16:30
  NEW YORK  America/New_York  08:00 to 17:00   (08:00 is a window start, NOT the formal COMEX open; 08:20 and 08:30 are separate references below)
  OVERLAP   the intersection of LONDON and NEW YORK on that day
  POST NEWS up to POST_NEWS_MIN minutes after a high-impact scheduled event (overrides the session label)
  TRANSITION  open market but outside the windows above (thinner liquidity)
  CLOSED    the daily 17:00 to 18:00 New York maintenance break, and the weekend from Friday 17:00 to Sunday 18:00 New York

Event times (London AM fix 10:30, PM fix 15:00; New York 08:20 and 08:30 references; 09:30 equity open) are RISK WINDOWS and explanatory
variables, never signals. Spread and volume are None when the data has none: they are never invented.
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UTC = dt.timezone.utc
WINDOWS = {
    "asia": ("Asia/Tokyo", (9, 0), (15, 0)),
    "london": ("Europe/London", (8, 0), (16, 30)),
    "new_york": ("America/New_York", (8, 0), (17, 0)),
}
LABELS = {"asia": "Asian", "london": "London", "new_york": "New York", "overlap": "London-New York overlap", "post_news": "Post-news", "transition": "Transition / low liquidity", "closed": "Market closed"}
REFERENCES = (  # (name, zone, (h, m)): risk windows and explanatory references, not signals
    ("London Gold Price AM auction", "Europe/London", (10, 30)),
    ("London Gold Price PM auction", "Europe/London", (15, 0)),
    ("New York 08:20 historical COMEX liquidity reference", "America/New_York", (8, 20)),
    ("New York 08:30 US data release reference", "America/New_York", (8, 30)),
    ("New York 09:30 equity-market open reference", "America/New_York", (9, 30)),
)
BREAK = ("America/New_York", (17, 0), (18, 0))      # CME daily maintenance break; the weekend closure also starts Friday 17:00 here
POST_NEWS_MIN, PRE_NEWS_MIN = 90, 30
DISPLAY_ZONES = ("Asia/Tokyo", "Europe/London", "America/New_York", "Asia/Karachi")


def _utc(zone, day, hm):
    """UTC timestamp of the local wall-clock time hm on the local calendar date `day` in `zone`."""
    local = dt.datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=ZoneInfo(zone))
    return local.astimezone(UTC).timestamp()


def local_dates(ts, zone):
    return dt.datetime.fromtimestamp(ts, UTC).astimezone(ZoneInfo(zone)).date()


def windows_for_day(day, cfg=None):
    """Session windows that start on UTC calendar date `day` (a datetime.date), each as (name, start_ts, end_ts). Local dates are
    taken from the zone itself so a window never shifts a day because of an offset."""
    cfg = cfg or WINDOWS
    out = {}
    for name, (zone, a, b) in cfg.items():
        d = day                                    # the local calendar date equals the UTC date for these zones at these hours
        s, e = _utc(zone, d, a), _utc(zone, d, b)
        out[name] = (s, e)
    lo, hi = out["london"], out["new_york"]
    s, e = max(lo[0], hi[0]), min(lo[1], hi[1])
    out["overlap"] = (s, e) if e > s else None
    return out


def _closed(ts):
    """True during the daily maintenance break or the weekend closure (both anchored to New York local time)."""
    ny = dt.datetime.fromtimestamp(ts, UTC).astimezone(ZoneInfo(BREAK[0]))
    wd, hm = ny.weekday(), (ny.hour, ny.minute)
    if wd == 5:                                           # Saturday
        return True
    if wd == 4 and hm >= BREAK[1]:                        # Friday after 17:00
        return True
    if wd == 6 and hm < BREAK[2]:                         # Sunday before 18:00
        return True
    return wd not in (4, 5, 6) and BREAK[1] <= hm < BREAK[2]


def classify(ts, events=None, cfg=None):
    """Session label and flags for one UTC timestamp. `events` are dicts with ts and importance. Returns a dict."""
    flags = dict(news_block=False, minutes_since_event=None, minutes_to_event=None)
    for e in events or ():
        if e.get("importance") != "high":
            continue
        d = (ts - e["ts"]) / 60.0
        if -PRE_NEWS_MIN <= d <= POST_NEWS_MIN:
            flags["news_block"] = True
        if 0 <= d and (flags["minutes_since_event"] is None or d < flags["minutes_since_event"]):
            flags["minutes_since_event"] = d
        if d < 0 and (flags["minutes_to_event"] is None or -d < flags["minutes_to_event"]):
            flags["minutes_to_event"] = -d
    if _closed(ts):
        return dict(session="closed", label=LABELS["closed"], **flags)
    if flags["minutes_since_event"] is not None and flags["minutes_since_event"] <= POST_NEWS_MIN:
        return dict(session="post_news", label=LABELS["post_news"], **flags)
    day = dt.datetime.fromtimestamp(ts, UTC).date()
    inside = []
    for d in (day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)):     # a window may start on the neighbouring UTC date
        w = windows_for_day(d, cfg)
        for name, span in w.items():
            if span and span[0] <= ts < span[1]:
                inside.append(name)
    for name in ("overlap", "london", "new_york", "asia"):
        if name in inside:
            return dict(session=name, label=LABELS[name], **flags)
    return dict(session="transition", label=LABELS["transition"], **flags)


def session_of(ts, events=None):
    return classify(ts, events)["session"]


def day_schedule(day, cfg=None):
    """Everything scheduled on the UTC date `day`, in UTC ISO strings, with the local time beside it, so the daylight-saving effect is visible."""
    iso = lambda t: dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%MZ")   # noqa: E731
    out = dict(date=day.isoformat(), sessions=[], references=[])
    for name, span in windows_for_day(day, cfg).items():
        if span:
            out["sessions"].append(dict(session=name, start=iso(span[0]), end=iso(span[1])))
    for label, zone, hm in REFERENCES:
        t = _utc(zone, day, hm)
        out["references"].append(dict(name=label, utc=iso(t), local="%02d:%02d %s" % (hm[0], hm[1], zone)))
    return out


def offsets_on(ts):
    """The UTC offset of each display zone at this instant (shows daylight saving, never hardcoded)."""
    t = dt.datetime.fromtimestamp(ts, UTC)
    return {z: t.astimezone(ZoneInfo(z)).utcoffset().total_seconds() / 3600.0 for z in DISPLAY_ZONES}


# ------------------------------------------------------------------ session statistics from bars -------------------------------------------------

def _atr(bars, n=14):
    trs, prev = [], None
    for t, h, l, c, o in bars:
        trs.append(h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev)))
        prev = c
    return sum(trs[-n:]) / len(trs[-n:]) if trs else None


def instances(bars, events=None):
    """Group consecutive bars of the same session type on the same session instance. Returns a list of dicts. Bars are (t, high, low, close, open).
    A trade-session instance never mixes two UTC days."""
    inst, cur = [], None
    for b in bars:
        c = classify(b[0], events)
        key = (c["session"], dt.datetime.fromtimestamp(b[0], UTC).date())
        if cur is None or cur["key"] != key:
            cur = dict(key=key, session=c["session"], bars=[])
            inst.append(cur)
        cur["bars"].append(b)
    return inst


def stats(bars, events=None, volumes=None):
    """Per-session descriptive statistics. Honest by construction: spread and volume are None when absent; character is INSUFFICIENT EVIDENCE under 30 instances."""
    by = {}
    prev_range = {}
    for it in instances(bars, events):
        bs = it["bars"]
        if len(bs) < 2:
            continue
        hi, lo = max(b[1] for b in bs), min(b[2] for b in bs)
        first_open, last_close = bs[0][4], bs[-1][3]
        path = sum(abs(b[3] - b[4]) for b in bs) or 1e-12
        moves = [b[3] - b[4] for b in bs]
        ac = None
        if len(moves) >= 4:
            m = sum(moves) / len(moves)
            den = sum((x - m) ** 2 for x in moves)
            ac = (sum((moves[i] - m) * (moves[i - 1] - m) for i in range(1, len(moves))) / den) if den > 0 else None
        s = by.setdefault(it["session"], dict(instances=0, ranges=[], efficiency=[], autocorr=[], displacement=[], breaks_prior=0, reversals=0, comparable=0))
        s["instances"] += 1
        s["ranges"].append(hi - lo)
        s["efficiency"].append(abs(last_close - first_open) / path)
        s["displacement"].append((last_close - first_open) / (hi - lo) if hi > lo else 0.0)
        if ac is not None:
            s["autocorr"].append(ac)
        pr = prev_range.get(it["session"])
        if pr:
            s["comparable"] += 1
            if hi > pr[0] or lo < pr[1]:
                s["breaks_prior"] += 1
                if (hi > pr[0] and last_close < pr[0]) or (lo < pr[1] and last_close > pr[1]):
                    s["reversals"] += 1          # broke the previous session's range and closed back inside it
        prev_range[it["session"]] = (hi, lo)
    out = {}
    avg = lambda v: (sum(v) / len(v)) if v else None   # noqa: E731
    for name, s in by.items():
        n = s["instances"]
        ae, ac = avg(s["efficiency"]), avg(s["autocorr"])
        if n < 30:
            character = "INSUFFICIENT EVIDENCE"
        elif ac is not None and ac > 0.05 and ae is not None and ae > 0.3:
            character = "momentum-leaning (descriptive)"
        elif ac is not None and ac < -0.05:
            character = "mean-reversion-leaning (descriptive)"
        else:
            character = "no clear character (descriptive)"
        out[name] = dict(label=LABELS.get(name, name), instances=n, avg_range=avg(s["ranges"]), avg_efficiency=ae, avg_autocorr=ac, avg_displacement_over_range=avg(s["displacement"]),
                         breakout_rate=(s["breaks_prior"] / s["comparable"] if s["comparable"] else None), reversal_rate_after_break=(s["reversals"] / s["breaks_prior"] if s["breaks_prior"] else None),
                         character=character, spread=None, volume=None if not volumes else "present",
                         note="Descriptive only: a session's character is not a trading signal. Spread and volume are not in this data.")
    return out


def tracker(bars, now_ts, events=None):
    """Live-style state at `now_ts` from CLOSED bars only: current session, its high and low so far, the previous instances' ranges, ATR, time remaining."""
    closed = [b for b in bars if b[0] <= now_ts]
    cls = classify(now_ts, events)
    its = [it for it in instances(closed, events)]
    cur = next((it for it in reversed(its) if it["session"] == cls["session"]), None)
    prev = {}
    for it in its[:-1] if its and cur is its[-1] else its:
        bs = it["bars"]
        prev[it["session"]] = dict(high=max(b[1] for b in bs), low=min(b[2] for b in bs), date=it["key"][1].isoformat())
    out = dict(now=dt.datetime.fromtimestamp(now_ts, UTC).strftime("%Y-%m-%dT%H:%MZ"), classification=cls, offsets_hours=offsets_on(now_ts), previous_ranges=prev,
               atr=_atr(closed[-60:]), spread=None, volume=None)
    if cur and cur["bars"]:
        bs = cur["bars"]
        out["current"] = dict(high=max(b[1] for b in bs), low=min(b[2] for b in bs), open=bs[0][4], bars=len(bs))
    day = dt.datetime.fromtimestamp(now_ts, UTC).date()
    w = windows_for_day(day).get(cls["session"])
    out["minutes_to_session_end"] = round((w[1] - now_ts) / 60.0) if w and w[0] <= now_ts < w[1] else None
    return out


def build(bars_1h, bars_15m, events, now_ts):
    day = dt.datetime.fromtimestamp(now_ts, UTC).date()
    return dict(now=tracker(bars_15m or bars_1h, now_ts, events), schedule_today=day_schedule(day), schedule_in_summer=day_schedule(dt.date(day.year, 7, 15)),
                schedule_in_winter=day_schedule(dt.date(day.year, 1, 15)), stats_1h=stats(bars_1h, events), bars_1h=len(bars_1h), bars_15m=len(bars_15m or []),
                definitions={k: dict(zone=v[0], start="%02d:%02d" % v[1], end="%02d:%02d" % v[2]) for k, v in WINDOWS.items()},
                caution=("Descriptive statistics from delayed GC=F futures bars (last-trade, no spread, no volume). The windows are configurable assumptions, not the formal COMEX hours. "
                         "A session's apparent character is not evidence of a profitable strategy; that needs the pre-registered tests."))


def main(argv=None):
    import argparse
    import gold_intraday as gi
    import macro
    ap = argparse.ArgumentParser(description="Gold session intelligence: current session, daylight-saving-correct schedule, per-session descriptive statistics.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--symbol", default="GC=F")
    a = ap.parse_args(argv)
    h1 = gi.get_bars(a.symbol, "60m", "6mo", 300)
    try:
        m15 = gi.get_bars(a.symbol, "15m", "30d", 60)
    except (Exception, SystemExit):
        m15 = []
    events = macro.load_store()
    now = (m15 or h1)[-1][0] + (900 if m15 else 3600)
    doc = build(h1, m15, events, now)
    if a.json:
        print(json.dumps(doc))
    else:
        c = doc["now"]["classification"]
        print("now:", doc["now"]["now"], "session:", c["label"], "news_block:", c["news_block"])
        for k, v in doc["stats_1h"].items():
            print("%-12s n=%-4s range=%s character=%s" % (k, v["instances"], None if v["avg_range"] is None else round(v["avg_range"], 2), v["character"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
