#!/usr/bin/env python3
"""Gold news impact: which headlines push gold up or down, why, and whether markets agree.

Standard library only, read-only. Headlines come from public RSS feeds (Yahoo Finance and Google
News). Each headline is matched against a small, readable rulebook of gold drivers (Fed and rates,
dollar, Treasury yields, inflation, jobs, geopolitics, demand, risk appetite). The rulebook is
keyword based: it reads wording, not meaning, so treat it as a sorting aid, not a forecast.
The cross-market table is measured from past daily prices (dollar, 10-year yield, stocks vs gold).

    python webui/gold_news.py --json --hours 36
"""
import argparse
import concurrent.futures as cf
import email.utils
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold  # noqa: E402

UA = "Mozilla/5.0 (compatible; trader-skill-gold/1.0; local research tool)"
YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
GOOGLE = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
QUERIES = ["gold price", "gold Fed rate cut", "dollar Treasury yields gold", "gold central bank buying ETF",
           "gold safe haven geopolitical", "inflation CPI Fed gold", "gold futures"]
SYMBOL_FEEDS = ["GC=F", "GLD"]
GOLD_WORDS = re.compile(r"\b(gold|bullion|xau|xauusd|gc=f|precious metals?|comex)\b", re.I)
HALF_LIFE_H = 12.0


def R(*pats):
    return [re.compile(p, re.I) for p in pats]


# weight: how strongly this kind of news usually matters for gold (0 to 1). bull/bear: wording that
# pushes gold up or down. A headline that matches both sides of one driver counts as mixed (zero).
DRIVERS = [
    dict(id="fed", name="Fed & interest rates", weight=1.0,
         why="Gold pays no interest. Expected rate cuts make holding it cheaper (bullish); hikes or 'higher for longer' do the opposite.",
         bull=R(r"rate[- ]cuts?\b", r"\bcuts? (interest )?rates?", r"dovish", r"\beasing\b", r"lower interest rates",
                r"fed (pivot|pause)", r"cut bets", r"rate[- ]cut (hopes|bets|expectations)"),
         bear=R(r"rate hikes?", r"hik(e|es|ing) (interest )?rates?", r"hawkish", r"higher for longer", r"\btightening\b",
                r"(surge|rise|jump|climb)s? in real (interest )?(rates|yields)", r"real (interest )?(rates|yields) (rise|jump|surge|climb)",
                r"(pressure|weigh\w*|dent\w*|hurt\w*|hit\w*) (on )?gold", r"gold (under pressure|pressured)",
                r"fewer (rate )?cuts", r"delay(s|ed)? (rate )?cuts?", r"cut (bets|hopes) (fade|fall|dim|wane)", r"no (more )?rate cuts?")),
    dict(id="usd", name="US dollar", weight=0.9,
         why="Gold is priced in dollars. A weaker dollar makes it cheaper for everyone else (bullish); a stronger dollar weighs on it.",
         bull=R(r"dollar (falls?|drops?|slides?|weakens?|slumps?|retreats?|softens?|declines?|dips?|sinks?|lower|slips?)",
                r"(weak(er)?|falling|slumping) (us )?dollar", r"dollar index (falls?|dips?|down|slips?)", r"greenback (falls?|slips?|weak)"),
         bear=R(r"dollar (rises?|climbs?|jumps?|strengthens?|firms?|gains?|rallies|rally|surges?|higher|advances?)",
                r"(strong(er)?|rising|surging) (us )?dollar", r"dollar index (rises?|jumps?|up)", r"greenback (gains?|firms?|rises?)")),
    dict(id="yields", name="Treasury yields", weight=0.9,
         why="Higher yields make interest-paying bonds more attractive than gold (bearish); falling yields help gold.",
         bull=R(r"yields? (fall|fell|drop|slip|dip|decline|slide|tumble|retreat|lower|ease|sink|slump)"),
         bear=R(r"yields? (rise|rose|jump|climb|surge|spike|higher|advance|hit|reach)", r"(rising|higher|surging) (treasury )?yields?")),
    dict(id="inflation", name="Inflation", weight=0.6,
         why="Hot inflation can lift gold as a hedge, but it can also make the Fed more hawkish. The net effect is mixed, so it counts less.",
         bull=R(r"inflation (rises?|rose|hotter|accelerat\w+|sticky|jumps?|surges?|higher|heats?)", r"hot(ter)? (cpi|pce|inflation)"),
         bear=R(r"inflation (cools?|eases?|slows?|falls?|softer|lower|declines?)", r"cool(er)? (cpi|inflation)")),
    dict(id="jobs", name="Jobs & growth data", weight=0.7,
         why="Strong jobs data keeps the Fed from cutting (bearish for gold); weak data raises cut bets (bullish).",
         bull=R(r"weak(er)? (jobs|payrolls|labou?r|hiring)", r"(jobs|payrolls) (miss|disappoint|slump|fall|weak|cool)",
                r"unemployment (rises?|jumps?|climbs?)", r"jobless claims (rise|jump|climb)", r"recession"),
         bear=R(r"strong(er)? (jobs|payrolls|labou?r|hiring|economy)", r"(jobs|payrolls) (beat|top|surge|jump|strong|rise)",
                r"unemployment (falls?|drops?)", r"jobless claims (fall|drop|decline)")),
    dict(id="geo", name="Geopolitics & safe haven", weight=0.8,
         why="In crises money looks for safety and gold is a classic safe haven (bullish). De-escalation removes that bid.",
         bull=R(r"\bwar\b", r"missile", r"invasion", r"\bescalat\w+", r"geopolitic\w*", r"\bsanctions?\b", r"safe[- ]haven",
                r"\btensions?\b", r"\bconflict\b", r"trade war", r"\btariffs?\b"),
         bear=R(r"cease[- ]?fire", r"peace (deal|talks)", r"de-?escalat\w+", r"\btruce\b")),
    dict(id="demand", name="Demand & central banks", weight=0.8,
         why="Central-bank and ETF buying is real demand that supports price; heavy selling or outflows remove it.",
         bull=R(r"central banks? (buy|buying|purchas\w+|add|accumulat\w+)", r"(etf|gld|fund)s? inflows?",
                r"record (gold )?(demand|purchases)", r"\bpboc\b.*(buy|add|gold)", r"china.*(buys?|adds?).*gold"),
         bear=R(r"(etf|gld|fund)s? outflows?", r"central banks? (sell|selling|dump)", r"demand (falls?|weak\w*|slump\w*|slows?)",
                r"jewel+ery demand (falls?|slump\w*|weak\w*)")),
    dict(id="risk", name="Risk appetite", weight=0.5,
         why="Panic selling in stocks can send money to gold; a euphoric stock rally can pull money away. A weak, indirect link.",
         bull=R(r"stocks? (tumble|plunge|sell-?off|slump|sink|crash)", r"market (rout|turmoil|panic|sell-?off)", r"wall street (falls?|tumbles?|slumps?)"),
         bear=R(r"stocks? (rally|surge|soar|jump)", r"record high.*(s&p|dow|nasdaq|stocks)", r"risk[- ]on")),
    dict(id="move", name="Gold price action", weight=0.4, needs_gold=True,
         why="These headlines report a move that already happened, not its cause. They show direction, so they count the least.",
         bull=R(r"\b(rises?|climbs?|jumps?|rall(y|ies)|surges?|soars?|gains?|advances?|extends gains|record high|hits record|higher|up)\b"),
         bear=R(r"\b(falls?|drops?|slides?|slumps?|tumbles?|sinks?|declines?|loses|extends losses|lower|down|retreats?|pares)\b")),
]

LINKS = [  # (symbol, name, kind, how it usually relates, meaning)
    ("DX-Y.NYB", "US dollar index", "pct"),
    ("^TNX", "10-year Treasury yield", "diff"),
    ("^GSPC", "S&P 500", "pct"),
]


# ---- Bitcoin rulebook. Same mechanics, different drivers: crypto trades as a risk asset, so stocks
# and growth data point the other way from gold, and crypto-only news (ETF flows, rules, hacks) matters.
_G = {d["id"]: d for d in DRIVERS}
BTC_WORDS = re.compile(r"\b(bitcoin|btc|btc-usd|crypto(currenc(y|ies))?|spot bitcoin|digital assets?)\b", re.I)
BTC_DRIVERS = [
    dict(_G["fed"], why="Rate cuts and easy money lift risk assets, crypto included (bullish); hikes and 'higher for longer' drain liquidity (bearish).",
         bear=[p for p in _G["fed"]["bear"] if "gold" not in p.pattern] +
         R(r"(pressure|weigh\w*|hit\w*|hurt\w*|dent\w*) (on )?(bitcoin|btc|crypto)", r"(bitcoin|btc|crypto) (under pressure|pressured)")),
    dict(_G["usd"], weight=0.8, why="A weaker dollar usually helps bitcoin and a strong one weighs on it, though the link is looser than for gold."),
    dict(_G["yields"], weight=0.8, why="Higher yields make safe interest more attractive than a volatile asset (bearish); falling yields help."),
    dict(id="flows", name="ETF flows & institutions", weight=1.0,
         why="Spot ETF and corporate buying is real demand (bullish); outflows and large holders selling remove it (bearish).",
         bull=R(r"etfs? inflows?", r"record inflows?", r"institutional (buy\w*|adoption|demand|investors? (buy|add))", r"(strategy|microstrategy|saylor).*(buy|bought|purchas\w+|acquir\w+|adds?)",
                r"(strategic )?bitcoin reserve", r"treasur(y|ies) (buy\w*|adds?).*bitcoin", r"whales? (buy\w*|accumulat\w+)"),
         bear=R(r"etfs? outflows?", r"record outflows?", r"institutional (selling|outflows?)", r"whales? (sell\w*|dump\w*)",
                r"(sells?|sold|selling|dump\w*) (\d[\d,.]* )?(bitcoin|btc)", r"miners? (sell\w*|dump\w*|capitulat\w+)")),
    dict(id="rules", name="Regulation & policy", weight=0.9,
         why="Clear, friendly rules and approvals widen who can buy (bullish); bans, lawsuits and crackdowns narrow it (bearish).",
         bull=R(r"(sec|regulators?|congress|senate).*(approv\w+|clarity|friendly|framework|passes|advances)", r"pro-crypto", r"legal tender",
                r"crypto bill (passes|advances)", r"etf approv\w+", r"approv\w+ (spot )?(bitcoin|crypto) etf"),
         bear=R(r"\bban(s|ned|ning)?\b", r"crackdown", r"\blawsuit\b", r"\bsues?\b", r"investigat\w+", r"enforcement", r"restrict\w+",
                r"reject\w*", r"\bfines?\b.*(crypto|exchange)")),
    dict(id="security", name="Hacks & exchange trouble", weight=0.8,
         why="Hacks, thefts and exchange failures damage trust and force selling (bearish).",
         bull=[],
         bear=R(r"\bhack(ed|s|ers?)?\b", r"\bexploit(ed|s)?\b", r"\bstolen\b", r"\bbreach\w*", r"exchange (collapse\w*|halts?|insolven\w+|bankrupt\w*)", r"rug pull")),
    dict(id="risk", name="Stocks & risk appetite", weight=0.8,
         why="Bitcoin trades like a risk asset: stock rallies and risk-on moods help it (bullish); sell-offs and recession fear hurt it (bearish).",
         bull=R(r"stocks? (rally|surge|soar|jump)", r"(nasdaq|s&p).*(record|rally|surge)", r"risk[- ]on", r"record high.*(s&p|dow|nasdaq|stocks)"),
         bear=R(r"stocks? (tumble|plunge|sell-?off|slump|sink|crash)", r"market (rout|turmoil|panic|sell-?off)", r"risk[- ]off", r"\brecession\b",
                r"wall street (falls?|tumbles?|slumps?)")),
    dict(id="inflation", name="Inflation", weight=0.6,
         why="Hot inflation tends to make the Fed more hawkish (bearish for risk assets); cooler inflation raises cut hopes (bullish).",
         bull=_G["inflation"]["bear"], bear=_G["inflation"]["bull"]),
    dict(_G["jobs"], weight=0.5, why="Strong jobs data keeps the Fed from cutting (bearish); weak data raises cut bets (bullish)."),
    dict(id="geo", name="Geopolitics", weight=0.5,
         why="Crises usually hit crypto first as money runs to cash (bearish); de-escalation lets risk appetite return. Not a reliable safe haven short-term.",
         bull=R(r"cease[- ]?fire", r"peace (deal|talks)", r"de-?escalat\w+", r"\btruce\b"),
         bear=R(r"\bwar\b", r"missile", r"invasion", r"\bescalat\w+", r"geopolitic\w*", r"\bsanctions?\b", r"\btensions?\b", r"trade war", r"\btariffs?\b")),
    dict(id="move", name="Bitcoin price action", weight=0.4, needs_gold=True,
         why="These headlines report a move that already happened, not its cause. They show direction, so they count the least.",
         bull=_G["move"]["bull"], bear=_G["move"]["bear"]),
]
BTC_JUNK = re.compile(r"\b(price prediction|forecast|could (hit|reach|soar)|meme ?coins?|airdrop|giveaway|presale|casino|sponsored|"
                      r"how to buy|dogecoin|shiba|xrp|solana|cardano|pepe|ripple|ethereum|ether)\b", re.I)
BTC_LINKS = [("DX-Y.NYB", "US dollar index", "pct"), ("^TNX", "10-year Treasury yield", "diff"),
             ("^IXIC", "Nasdaq", "pct"), ("GC=F", "Gold", "pct")]


def asset_cfg(asset_id):
    if asset_id == "btc":
        return dict(id="btc", name="Bitcoin", short="bitcoin", symbol="BTC-USD", words=BTC_WORDS, feeds=["BTC-USD", "IBIT"],
                    queries=["bitcoin price", "bitcoin ETF flows", "crypto regulation SEC", "bitcoin Fed rate",
                             "dollar Treasury yields bitcoin", "bitcoin whales institutional buying", "crypto market sell-off"],
                    drivers=BTC_DRIVERS, junk=BTC_JUNK, links=BTC_LINKS, move_thr=0.4, cmd="btc")
    return dict(id="gold", name="Gold", short="gold", symbol="GC=F", words=GOLD_WORDS, feeds=SYMBOL_FEEDS, queries=QUERIES,
                drivers=DRIVERS, junk=JUNK, links=LINKS, move_thr=0.1, cmd="gold")


def get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_time(s):
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return None


def parse_rss(data, via):
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        t = parse_time(it.findtext("pubDate"))
        if not title or t is None:
            continue
        src_el = it.find("source")
        source = (src_el.text if src_el is not None and src_el.text else "").strip()
        if not source and " - " in title:
            source = title.rsplit(" - ", 1)[1]
        if " - " in title and source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        out.append(dict(title=title, link=(it.findtext("link") or "").strip(), time=t,
                        source=source or via))
    return out


def fetch_news(errors, A):
    jobs = [(YAHOO % urllib.parse.quote(s, safe="="), "Yahoo Finance") for s in A["feeds"]]
    jobs += [(GOOGLE % urllib.parse.quote_plus(q + " when:3d"), "Google News") for q in A["queries"]]
    items = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(get, u): (u, via) for u, via in jobs}
        for f in cf.as_completed(futs):
            u, via = futs[f]
            try:
                items += parse_rss(f.result(), via)
            except (urllib.error.URLError, OSError, ValueError) as e:
                errors.append("%s feed failed: %s" % (via, e))
    seen, out = set(), []
    for it in sorted(items, key=lambda i: -i["time"]):
        k = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()[:90]
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# corporate and mining-company news that merely contains the word gold
JUNK = re.compile(r"\b(project|drill\w*|permit|offering|warrants?|resource estimate|miners?|mining|stocks?|shares|"
                  r"royalty|exploration|acquisition|placement|ltd|inc|corp|plc|etf (list|launch)|price targets?|"
                  r"porphyry|assay|jewel+ers?|coins?)\b", re.I)


def classify(item, now, A):
    title = item["title"]
    direct = bool(A["words"].search(title))
    hits, score = [], 0.0
    macro_hit = False
    for d in A["drivers"]:
        if d.get("needs_gold") and (not direct or macro_hit):
            continue                       # a reported move only counts when no cause is named
        b = any(p.search(title) for p in d["bull"])
        s = any(p.search(title) for p in d["bear"])
        if not (b or s):
            continue
        sign = 0 if (b and s) else (1 if b else -1)
        if d["id"] != "move":
            macro_hit = True
        hits.append(dict(id=d["id"], name=d["name"], dir="mixed" if sign == 0 else "bull" if sign > 0 else "bear"))
        score += sign * d["weight"]
    if not hits and not direct:
        return None
    if A["junk"].search(title) and not macro_hit:
        return None
    score *= 1.25 if direct else 1.0
    score = max(-3.0, min(3.0, score))
    age_h = max(0.0, (now - item["time"]) / 3600)
    return dict(title=title, link=item["link"], source=item["source"], time=item["time"], age_h=age_h,
                direct=direct, drivers=hits, score=round(score, 2),
                direction="bullish" if score > 0.2 else "bearish" if score < -0.2 else "neutral",
                impact="high" if abs(score) >= 1.5 else "medium" if abs(score) >= 0.8 else "low",
                weight=0.5 ** (age_h / HALF_LIFE_H))


def aggregate(rows, A):
    num = sum(r["score"] * r["weight"] for r in rows)
    den = sum(max(abs(r["score"]), 0.5) * r["weight"] for r in rows)
    n = len(rows)
    raw = 100 * num / den if den else 0.0
    pressure = raw * min(1.0, n / 8.0)         # a few headlines say little: shrink toward zero
    label = ("Bullish" if pressure >= 25 else "Leaning bullish" if pressure >= 10 else
             "Bearish" if pressure <= -25 else "Leaning bearish" if pressure <= -10 else "Balanced")
    drivers = []
    for d in A["drivers"]:
        bull = sum(1 for r in rows if any(h["id"] == d["id"] and h["dir"] == "bull" for h in r["drivers"]))
        bear = sum(1 for r in rows if any(h["id"] == d["id"] and h["dir"] == "bear" for h in r["drivers"]))
        mix = sum(1 for r in rows if any(h["id"] == d["id"] and h["dir"] == "mixed" for h in r["drivers"]))
        net = sum((1 if h["dir"] == "bull" else -1 if h["dir"] == "bear" else 0) * r["weight"]
                  for r in rows for h in r["drivers"] if h["id"] == d["id"]) * d["weight"]
        drivers.append(dict(id=d["id"], name=d["name"], why=d["why"], bull=bull, bear=bear, mixed=mix,
                            net=round(net, 2), weight=d["weight"]))
    drivers.sort(key=lambda x: -(x["bull"] + x["bear"] + x["mixed"]))
    return dict(score=round(pressure, 1), label=label, n=n, bull=sum(1 for r in rows if r["direction"] == "bullish"),
                bear=sum(1 for r in rows if r["direction"] == "bearish"),
                neutral=sum(1 for r in rows if r["direction"] == "neutral")), drivers


def series(bars):
    return {b[0] // 86400: b[3] for b in bars}


def changes(vals, kind):
    return [(b - a) if kind == "diff" else (b / a - 1) * 100 for a, b in zip(vals, vals[1:])]


def corr(x, y):
    n = len(x)
    if n < 20:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else None


def relation(goldbars, symbol, name, kind):
    bars, _ = gold.fetch_bars(symbol, "2y", "1d")
    g, o = series(goldbars), series(bars)
    days = sorted(set(g) & set(o))
    gv, ov = [g[d] for d in days], [o[d] for d in days]
    gr = changes(gv, "pct")
    orr = changes(ov, kind)
    last_o, prev_o = bars[-1][3], bars[-2][3]
    day = (last_o - prev_o) if kind == "diff" else (last_o / prev_o - 1) * 100
    five = (last_o - bars[-6][3]) if kind == "diff" else (last_o / bars[-6][3] - 1) * 100
    c60 = corr(orr[-60:], gr[-60:])
    c250 = corr(orr[-250:], gr[-250:])
    mo = sum(orr) / len(orr)
    sd = math.sqrt(sum((a - mo) ** 2 for a in orr) / max(1, len(orr) - 1))
    thr = 0.5 * sd

    def cond(sel):
        v = [r for a, r in zip(orr, gr) if sel(a)]
        if len(v) < 5:
            return dict(n=len(v), mean=None, se=None)
        m = sum(v) / len(v)
        s = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
        return dict(n=len(v), mean=m, se=s / math.sqrt(len(v)))
    return dict(symbol=symbol, name=name, kind=kind, last=last_o, day=day, five=five, corr60=c60, corr250=c250,
                up=cond(lambda a: a > thr), down=cond(lambda a: a < -thr),
                unit="pts" if kind == "diff" else "%", thr=thr, n=len(orr))


def market(errors, A):
    gbars, _ = gold.fetch_bars(A["symbol"], "2y", "1d")
    last, prev = gbars[-1][3], gbars[-2][3]
    out = dict(base=dict(name=A["name"], symbol=A["symbol"], last=last, day=(last / prev - 1) * 100,
                         five=(last / gbars[-6][3] - 1) * 100), links=[])
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(relation, gbars, s, n, k): n for s, n, k in A["links"]}
        for f in cf.as_completed(futs):
            try:
                out["links"].append(f.result())
            except (SystemExit, Exception) as e:                 # one missing series should not sink the page
                errors.append("%s unavailable: %s" % (futs[f], e))
    order = [s for s, _, _ in A["links"]]
    out["links"].sort(key=lambda l: order.index(l["symbol"]))
    return out


def confirm_lines(agg, mk, A):
    """Does the market agree with the news? Uses today's moves and each link's usual direction."""
    lines = []
    g = mk["base"]["day"]
    nm, thr = A["short"], A["move_thr"]
    lean = 1 if agg["score"] >= 10 else -1 if agg["score"] <= -10 else 0
    if lean:
        agree = (g > thr and lean > 0) or (g < -thr and lean < 0)
        against = (g > thr and lean < 0) or (g < -thr and lean > 0)
        word = "bullish" if lean > 0 else "bearish"
        if agree:
            lines.append(("good", "News leans %s and %s moved %+.2f%% today: price is confirming the news." % (word, nm, g)))
        elif against:
            lines.append(("warn", "News leans %s but %s moved %+.2f%% today: price is NOT following the news. "
                          "Either it is already priced in or something else is in charge." % (word, nm, g)))
        else:
            lines.append(("flat", "News leans %s but %s barely moved today (%+.2f%%): no confirmation yet." % (word, nm, g)))
    else:
        lines.append(("flat", "The headlines do not lean either way, so there is nothing for price to confirm."))
    for l in mk["links"]:
        c = l["corr60"]
        if c is None or abs(c) < 0.2 or abs(l["day"]) < 0.0001:
            continue
        expected_up = (l["day"] > 0) == (c > 0)           # direction gold "should" take from this move
        g_dir = 1 if expected_up else -1
        moved = (g > thr and g_dir > 0) or (g < -thr and g_dir < 0)
        mv = "%+.2f%%" % l["day"] if l["unit"] == "%" else "%+.0f bp" % (l["day"] * 100)
        lines.append(("good" if moved else "flat",
                      "%s %s today (%s). Over 60 days %s has moved %s it (correlation %+.2f), which points to %s %s%s." % (
                          l["name"], "rose" if l["day"] > 0 else "fell", mv, nm, "with" if c > 0 else "against", c, nm,
                          "up" if g_dir > 0 else "down", ", and it did" if moved else ", but %s has not followed yet" % nm)))
    return lines


def build(hours, A):
    now = time.time()
    errors = []
    items = [i for i in fetch_news(errors, A) if now - i["time"] <= hours * 3600]
    rows = [r for r in (classify(i, now, A) for i in items) if r]
    rows.sort(key=lambda r: (-abs(r["score"]) * r["weight"], r["age_h"]))
    agg, drivers = aggregate(rows, A)
    try:
        mk = market(errors, A)
    except SystemExit as e:
        mk, errors = None, errors + [str(e)]
    out = dict(checked=now, hours=hours, scanned=len(items), pressure=agg, drivers=drivers, headlines=rows[:40],
               market=mk, errors=errors, asset=dict(id=A["id"], name=A["name"], short=A["short"], symbol=A["symbol"], cmd=A["cmd"]))
    out["confirm"] = confirm_lines(agg, mk, A) if mk else []
    try:                                  # scheduled releases from the stored calendar; reactions live on the Event reactions page
        import macro
        out["macro"] = macro.summary(now)
    except Exception:                     # the calendar is optional context and must never break the news page
        out["macro"] = None
    return out


def render(d):
    p = d["pressure"]
    nm = d["asset"]["short"]
    L = ["%s NEWS IMPACT  (last %dh, %d headlines scanned, %d relevant)" % (d["asset"]["name"].upper(), d["hours"], d["scanned"], p["n"]),
         "News pressure  %+.0f  %s   (%d bullish, %d bearish, %d neutral)" % (p["score"], p["label"], p["bull"], p["bear"], p["neutral"]), ""]
    L.append("DRIVERS")
    for x in d["drivers"]:
        if x["bull"] + x["bear"] + x["mixed"]:
            L.append("  %-26s bullish %d  bearish %d  mixed %d" % (x["name"], x["bull"], x["bear"], x["mixed"]))
    L.append("")
    if d["market"]:
        m = d["market"]
        L.append("MARKET   %s %+.2f%% today" % (nm, m["base"]["day"]))
        for l in d["market"]["links"]:
            L.append("  %-24s corr(60d) %s   %s moves %s on up days, %s on down days" % (
                l["name"], "n/a" if l["corr60"] is None else "%+.2f" % l["corr60"], nm,
                "n/a" if l["up"]["mean"] is None else "%+.2f%%" % l["up"]["mean"],
                "n/a" if l["down"]["mean"] is None else "%+.2f%%" % l["down"]["mean"]))
        L.append("")
        L += ["  " + t for _, t in d["confirm"]]
        L.append("")
    L.append("TOP HEADLINES")
    for r in d["headlines"][:15]:
        L.append("  [%s %+.1f] %s (%s, %.0fh ago)" % (r["direction"][:4], r["score"], r["title"], r["source"], r["age_h"]))
    for e in d["errors"]:
        L.append("note: " + e)
    L.append("")
    L.append("Keyword rules read wording, not meaning. Headlines are leads, not facts. Not financial advice.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gold or Bitcoin news impact (read-only).")
    ap.add_argument("--asset", choices=["gold", "btc"], default="gold")
    ap.add_argument("--hours", type=float, default=36)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not 1 <= a.hours <= 168:
        raise SystemExit("error: --hours must be between 1 and 168")
    d = build(a.hours, asset_cfg(a.asset))
    print(json.dumps(d) if a.json else render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
