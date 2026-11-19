#!/usr/bin/env python3
"""Local web view for the Trader skill pack. Standard library only.

    python webui/server.py            # then open http://127.0.0.1:8765

Every button runs one of the commands listed in COMMANDS below, as an argument
list (no shell), from the repo root, so `trader/` files land where the skills
expect them. Only the commands in this file can run. Nothing here can place an
order: trader-broker's approve / decline / result / fill are not exposed, only
`show` and `log`.
"""
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets  # noqa: E402
import backtester  # noqa: E402
import command_centre  # noqa: E402
import gold_watch  # noqa: E402
import journal  # noqa: E402
import ml  # noqa: E402
import orders  # noqa: E402
import paper  # noqa: E402
import pending  # noqa: E402
import portfolio  # noqa: E402
import strategy_policy  # noqa: E402
import research  # noqa: E402
import strategies  # noqa: E402  (same folder; none of these modules can place an order)

ROOT = Path(__file__).resolve().parent.parent
WATCHERS = {"gold": gold_watch.Watcher(), "btc": gold_watch.Watcher(assets.BTC)}   # one per asset, never place orders
WATCH_PATH = re.compile(r"^/api/(gold|btc)/watch$")
WEB = Path(__file__).resolve().parent
TRADER_DIR = ROOT / "trader"
HOST, PORT = "127.0.0.1", int(os.environ.get("TRADER_WEB_PORT", "8765"))
TIMEOUT = 90          # seconds per command
MAX_OUT = 200_000     # characters kept per stream

SAFE_TEXT = re.compile(r"^[A-Za-z0-9 ._,'&@:/=+-]{1,200}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def P(name, label, kind="text", default="", flag=None, choices=None,
      hint="", positional=False, multi=False):
    """One input. flag=None + positional=True -> a bare argument."""
    return dict(name=name, label=label, kind=kind, default=default, flag=flag,
                choices=choices, hint=hint, positional=positional, multi=multi)


# id, group, title, what it does, script + fixed args, params
COMMANDS = [
    # ---- gold
    dict(id="intel", group="Gold", title="Gold intelligence",
         help="Market overview, strategy assessments with NO TRADE gates, portfolio risk monitor, performance evidence and the trading "
              "journal. A strategy is only allowed to say LONG or SHORT after it passed out-of-sample validation. Read-only.",
         script="webui/intelligence.py", fixed=["--json"], view="intel",
         params=[P("symbol", "Market", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F", "BTC-USD"]),
                 P("interval", "Chart", "select", "60m", "--interval", choices=["60m", "15m"]),
                 P("risk", "Risk per trade (%)", "number", "1", "--risk-pct")]),
    dict(id="scorecard", group="Gold", title="Recommendation scorecard",
         help="Every trade plan the console recommends, for Gold and Bitcoin, is recorded when shown and graded later against what price actually did, "
              "whether or not you took it: would it have won, lost, or never triggered. Shows the right/wrong record with honest bands.",
         script="webui/scorecard.py", fixed=["--json"], view="scorecard", params=[]),
    dict(id="profit", group="Gold", title="Gold profitability intelligence",
         help="One dashboard in eight sections (strategy health, loss diagnostics, signal quality, opportunity ranking, execution quality, profitability comparison, capital protection, "
              "optimisation history) built from the saved results of every earlier test. It runs no new experiment, and every figure is labelled historical backtest or live record.",
         script="webui/profitability.py", fixed=["--json"], view="profit", params=[]),
    dict(id="goldrebuild", group="Gold lab", title="Gold rebuild results",
         help="The pre-registered backtest of the two kept Gold strategy families (trend continuation, structural rejection) against the archived original engine on identical dates and costs. "
              "Shows the saved result; tick re-run for fresh data.",
         script="webui/gold_core.py", fixed=["--json"], view="goldrebuild",
         params=[P("rerun", "Recompute with the latest data (about 10 seconds). Note: a re-run adds a new entry to the experiment log.", "check", flag="--rerun")]),
    dict(id="sessions", group="Gold lab", title="Gold sessions",
         help="Which gold session is active now (Asian, London, New York, overlap, post-news, transition, closed), the daylight-saving-correct schedule for Tokyo, London, New York and Karachi, "
              "the session ranges so far, and descriptive per-session statistics. No spread or volume is shown because this data has none.",
         script="webui/gold_sessions.py", fixed=["--json"], view="sessions", params=[]),
    dict(id="experiments3", group="Gold lab", title="Session, news and machine-learning filter experiments",
         help="The pre-registered Phase 10 to 12 tests: session and news filters, a purged machine-learning trade filter, an event-driven order-lifecycle backtester checked against the grader, "
              "and a replay of the risk limits. Historical backtest only. Shows the saved result; tick re-run for fresh data.",
         script="webui/experiments3.py", fixed=["--json"], view="experiments3",
         params=[P("rerun", "Recompute with the latest data (about 60 seconds)", "check", flag="--rerun")]),
    dict(id="experiments2", group="Gold lab", title="Stops, grading and strict mode experiments",
         help="The pre-registered Phase 6 to 9 tests: stop and exit variants, whether the A/B/C score predicts results, the evidence grade of each strategy, what de-duplicating repeated "
              "ideas changes, and strict mode versus baseline mode. Shows the saved result; tick re-run for fresh data. Strict mode can be switched on or off on the page.",
         script="webui/experiments2.py", fixed=["--json"], view="experiments2",
         params=[P("rerun", "Recompute with the latest data (about 40 seconds)", "check", flag="--rerun")]),
    dict(id="experiments", group="Gold lab", title="Regime, trend and breakout experiments",
         help="The pre-registered Phase 3 to 5 tests: a nine-class regime classifier with ablation, a higher-timeframe trend filter, a confirmed-reversal filter, and breakout entry modes "
              "and false-breakout filters, each judged once on a held-out test period. Shows the saved result. Run 'Backtest every recommendation' first if you want fresh data, then tick re-run.",
         script="webui/experiments.py", fixed=["--json"], view="experiments",
         params=[P("rerun", "Recompute with the latest data (about 30 seconds)", "check", flag="--rerun")]),
    dict(id="lossattr", group="Gold lab", title="Loss attribution report",
         help="What happened to every recommendation and why the losing ones lost. A cause is shown only when recorded evidence supports it, otherwise it is marked unknown. "
              "Separates recommendations from simulated fills and open positions, counts repeated ideas once, and audits the simulator's own arithmetic.",
         script="webui/loss_attribution.py", fixed=["--json"], view="lossattr", params=[]),
    dict(id="recbacktest", group="Gold lab", title="Backtest every recommendation",
         help="Replays history hour by hour: at each point the Gold and Bitcoin analysis is rebuilt from only the bars that existed then, every plan the page would have ranked "
              "in its top three is recorded, and each is graded win, loss or never triggered with conservative fills and costs. Takes about 45 seconds. Nothing is tuned on the result.",
         script="webui/recommendation_backtest.py", fixed=["--json"], view="scorecard", params=[]),
    dict(id="pending", group="Gold", title="Pending Order Intelligence",
         help="BUY STOP, SELL STOP, BUY LIMIT and SELL LIMIT, each with a full plan, its own out-of-sample evidence, broker-rule checks and "
              "account-wide risk. An order is only recommended after its strategy passed validation. Paper orders are a simulation; nothing is sent to a broker.",
         script="webui/pending.py", fixed=["--json"], view="pending",
         params=[P("symbol", "Market", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F"]),
                 P("interval", "Chart", "select", "60m", "--interval", choices=["60m", "15m"])]),
    dict(id="research-pending", group="Gold lab", title="Pending order research",
         help="Walk-forward test of BUY STOP, SELL STOP, BUY LIMIT and SELL LIMIT with an untouched holdout, fill rates, expiries and cost stress. "
              "Uses your saved broker profile for costs and the minimum stop distance. Takes about 10 seconds.",
         script="webui/research.py", fixed=["run", "--family", "pending", "--json"], view="research",
         params=[P("symbol", "Market", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F"]),
                 P("interval", "Chart", "select", "60m", "--interval", choices=["60m", "15m"]),
                 P("spread", "Spread (price units, blank = profile)", "number", "", "--spread"),
                 P("slippage", "Slippage per side (price units)", "number", "", "--slippage")]),
    dict(id="research", group="Gold lab", title="Gold research run",
         help="Walk-forward test of the four strategies with a final untouched holdout, cost stress, news-filter test and a calibrated "
              "model, saved as a reproducible record. Costs are placeholders unless you enter your broker's. Takes about 10 seconds.",
         script="webui/research.py", fixed=["run", "--json"], view="research",
         params=[P("symbol", "Market", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F", "BTC-USD", "XAUUSD"]),
                 P("interval", "Chart", "select", "60m", "--interval", choices=["60m", "15m", "1d", "5m"]),
                 P("source", "Data", "select", "yahoo", "--source", choices=["yahoo", "dukascopy", "oanda"]),
                 P("spread", "Spread (price units, blank = placeholder)", "number", "", "--spread"),
                 P("slippage", "Slippage per side (price units)", "number", "", "--slippage"),
                 P("quick", "Quick (smaller model)", "check", flag="--quick")]),
    dict(id="macro-show", group="Gold", title="Economic events",
         help="The stored calendar with where each date came from and how reliable it is. FOMC dates are official, NFP dates are derived "
              "from the BLS rule, anything else comes from your own CSV.",
         script="webui/macro.py", fixed=["show"], params=[]),
    dict(id="macro-update", group="Gold lab", title="Update economic events",
         help="One request to federalreserve.gov for FOMC dates, plus derived NFP dates marked as derived. BLS blocks automated downloads, "
              "so CPI and PCE need your own CSV (macro.py import-csv).",
         script="webui/macro.py", fixed=["update"], params=[]),
    dict(id="macro-study", group="Gold lab", title="Event reactions",
         help="How gold actually behaved after past releases, against how it behaves on any bar. Observed behaviour only, not cause.",
         script="webui/macro.py", fixed=["study"],
         params=[P("symbol", "Market", "select", "GC=F", "--symbol", choices=["GC=F"]),
                 P("interval", "Chart", "select", "60m", "--interval", choices=["60m", "15m", "1d"])]),
    dict(id="gold", group="Gold", title="Gold assessment",
         help="Trend, volatility, levels and a risk-based position size from delayed Yahoo daily bars. "
              "Read-only; statistics describe the past and do not predict price.",
         script="webui/gold.py", fixed=["--json"], view="gold",
         params=[P("symbol", "Contract", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F"]),
                 P("horizon", "Timeframes", "select", "all", "--horizon",
                   choices=["all", "intraday", "short", "long", "none"]),
                 P("account", "Account size ($)", "number", "2700", "--account"),
                 P("risk", "Risk per trade (%)", "number", "1", "--risk-pct"),
                 P("stop", "Stop distance ($/oz, blank = ATR based)", "number", "", "--stop"),
                 P("atr", "ATR multiple for the stop", "number", "2", "--atr-mult")]),
    dict(id="gold-backtest", group="Gold lab", title="Gold backtest",
         help="Replays the short-term setups day by day on past gold prices (no look-ahead) and shows win rate, "
              "average R, stability and a no-zone baseline. Takes a few seconds.",
         script="webui/gold_backtest.py", fixed=["--json"], view="backtest",
         params=[P("symbol", "Contract", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F"]),
                 P("years", "History", "select", "10y", "--years", choices=["10y", "5y", "2y"]),
                 P("window", "Order stays working (bars)", "number", "10", "--window"),
                 P("hold", "Max bars held", "number", "20", "--hold"),
                 P("cost", "Cost ($/oz round trip)", "number", "0.5", "--cost")]),
    dict(id="data-check", group="Gold lab", title="Data quality check",
         help="Fetches one series from Yahoo and reports duplicates, missing bars, bad candles, staleness, whether the "
              "latest bar is still forming, and a provenance hash. Read-only; nothing is interpolated.",
         script="webui/marketdata.py", fixed=["check"],
         params=[P("symbol", "Symbol", "select", "GC=F", "--symbol", choices=["GC=F", "MGC=F", "BTC-USD"]),
                 P("interval", "Interval", "select", "15m", "--interval", choices=["15m", "60m", "1d", "1wk"])]),
    # ---- bitcoin: the same analysis as gold, on BTC-USD (24/7, so no market-hours gaps)
    dict(id="btc", group="Bitcoin", title="Bitcoin assessment",
         help="The same trend, volatility, levels, multi-timeframe confluence and ranked trade plans as the gold page, "
              "on BTC-USD (delayed Yahoo bars). Read-only; statistics describe the past and do not predict price.",
         script="webui/gold.py", fixed=["--json", "--symbol", "BTC-USD"], view="gold",
         params=[P("horizon", "Timeframes", "select", "all", "--horizon",
                   choices=["all", "intraday", "short", "long", "none"]),
                 P("account", "Account size ($)", "number", "2700", "--account"),
                 P("risk", "Risk per trade (%)", "number", "1", "--risk-pct"),
                 P("stop", "Stop distance ($/BTC, blank = ATR based)", "number", "", "--stop"),
                 P("atr", "ATR multiple for the stop", "number", "2", "--atr-mult")]),
    dict(id="btc-backtest", group="Bitcoin", title="Bitcoin backtest",
         help="Replays the daily setups on past BTC-USD prices (no look-ahead) and shows win rate, average R, "
              "stability and a no-zone baseline. Takes a few seconds.",
         script="webui/gold_backtest.py", fixed=["--json", "--symbol", "BTC-USD"], view="backtest",
         params=[P("years", "History", "select", "10y", "--years", choices=["10y", "5y", "2y"]),
                 P("window", "Order stays working (bars)", "number", "10", "--window"),
                 P("hold", "Max bars held", "number", "20", "--hold"),
                 P("cost", "Cost ($/BTC round trip)", "number", "25", "--cost")]),
    dict(id="btc-news", group="Bitcoin", title="Bitcoin news impact",
         help="Scans bitcoin, ETF-flow, regulation, Fed, dollar and stock headlines, scores which way each pushes BTC and "
              "why, and checks the dollar, 10-year yield, Nasdaq and gold to see if markets agree. Keyword rules, not a forecast.",
         script="webui/gold_news.py", fixed=["--json", "--asset", "btc"], view="goldnews",
         params=[P("hours", "Hours back", "number", "36", "--hours")]),
    # ---- copycat
    dict(id="copycat-find", group="Copycat", title="Find an investor",
         help="Look up a filer's CIK by name (Buffett, Burry, Ackman...).",
         script="trader-copycat/edgar13f.py", fixed=["find"],
         params=[P("query", "Name", default="buffett", positional=True, multi=True),
                 P("sec", "Search all EDGAR filers", "check", flag="--sec")]),
    dict(id="copycat-filings", group="Copycat", title="13F filings",
         help="List a filer's 13F-HR filings.",
         script="trader-copycat/edgar13f.py", fixed=["filings"],
         params=[P("cik", "CIK", default="1067983", positional=True),
                 P("limit", "Limit", "number", "8", "--limit")]),
    dict(id="copycat-holdings", group="Copycat", title="Holdings",
         help="The positions in the newest 13F (or a chosen quarter).",
         script="trader-copycat/edgar13f.py", fixed=["holdings"],
         params=[P("cik", "CIK", default="1067983", positional=True),
                 P("period", "Quarter end (YYYY-MM-DD, optional)", flag="--period"),
                 P("top", "Top N", "number", "10", "--top"),
                 P("combine", "Combine share classes", "check", "1", "--combine")]),
    dict(id="copycat-diff", group="Copycat", title="What changed",
         help="Bought, added, trimmed and sold since the quarter before.",
         script="trader-copycat/edgar13f.py", fixed=["diff"],
         params=[P("cik", "CIK", default="1067983", positional=True),
                 P("top", "Top N", "number", "", "--top"),
                 P("combine", "Combine share classes", "check", "1", "--combine")]),
    dict(id="copycat-copy", group="Copycat", title="Build targets",
         help="Top positions as copyable weights. Dry run prints; untick it to write trader/targets.json.",
         script="trader-copycat/edgar13f.py", fixed=["copy"],
         params=[P("cik", "CIK", default="1067983", positional=True),
                 P("top", "Top N", "number", "5", "--top"),
                 P("dry", "Dry run (do not write)", "check", "1", "--dry-run")]),
    # ---- watch
    dict(id="news", group="Watch", title="News monitor",
         help="Headlines and SEC 8-Ks for your tickers.",
         script="trader-news/news.py", fixed=["--json"], view="news",
         params=[P("tickers", "Tickers", default="AAPL KO", flag="--tickers", multi=True),
                 P("hours", "Hours back", "number", "24", "--hours"),
                 P("nosec", "Headlines only (skip SEC)", "check", flag="--no-sec"),
                 P("all", "Show seen items too", "check", flag="--all"),
                 P("dry", "Dry run (do not update state)", "check", "1", "--dry-run")]),
    dict(id="gold-news", group="Watch", title="Gold news impact",
         help="Scans gold, Fed, dollar, yield and geopolitics headlines, scores which way each pushes gold and "
              "why, and checks the dollar, 10-year yield and stocks to see if markets agree. Keyword rules, not a forecast.",
         script="webui/gold_news.py", fixed=["--json"], view="goldnews",
         params=[P("hours", "Hours back", "number", "36", "--hours")]),
    dict(id="pelosi-index", group="Watch", title="Congress trades: index",
         help="Periodic Transaction Reports filed this year.",
         script="trader-pelosi/ptr.py", fixed=["index"],
         params=[P("member", "Member (last name)", default="pelosi", flag="--member"),
                 P("year", "Year", "number", "", "--year"),
                 P("limit", "Limit", "number", "20", "--limit")]),
    dict(id="pelosi-new", group="Watch", title="Congress trades: new",
         help="Filings not seen at the last check.",
         script="trader-pelosi/ptr.py", fixed=["new"],
         params=[P("member", "Member (last name)", default="pelosi", flag="--member"),
                 P("dry", "Dry run (do not update state)", "check", "1", "--dry-run")]),
    dict(id="alerts-clock", group="Watch", title="NYSE clock",
         help="Is the market open? Uses the real NYSE calendar.",
         script="trader-alerts/alerts.py", fixed=["clock"], params=[]),
    dict(id="alerts-quote", group="Watch", title="Quotes",
         help="Latest delayed prices.",
         script="trader-alerts/alerts.py", fixed=["quote"],
         params=[P("symbols", "Symbols", default="AAPL KO", positional=True, multi=True)]),
    dict(id="alerts-list", group="Watch", title="List alert rules",
         help="Reads trader/alerts.json.",
         script="trader-alerts/alerts.py", fixed=["list"], params=[]),
    dict(id="alerts-check", group="Watch", title="Check alerts",
         help="Checks every rule against live prices.",
         script="trader-alerts/alerts.py", fixed=["check"],
         params=[P("nowrite", "Do not write state", "check", "1", "--no-write")]),
    # ---- research
    dict(id="analyst", group="Research", title="Earnings analyst",
         help="Reported numbers from SEC XBRL: revenue, EPS, margins, growth.",
         script="trader-analyst/facts.py", fixed=[],
         params=[P("tickers", "Tickers", default="AAPL", positional=True, multi=True),
                 P("quarters", "Quarters", "number", "", "--quarters"),
                 P("nofil", "Skip filing links", "check", flag="--no-filings")]),
    dict(id="screener", group="Research", title="Stock screener",
         help="Rank companies against your rules. Offline.",
         script="trader-screener/screen.py", fixed=[],
         params=[P("metrics", "Metrics file", "select", "trader-analyst/metrics.example.json",
                   positional=True,
                   choices=["trader/metrics.json", "trader-analyst/metrics.example.json"]),
                 P("rules", "Rules file", "select", "trader-screener/rules.example.json", "--rules",
                   choices=["trader/rules.json", "trader-screener/rules.example.json"]),
                 P("top", "Top N", "number", "", "--top")]),
    # ---- trade
    dict(id="budget", group="Trade", title="Budget sizing",
         help="Size every position to your budget. Preview only unless 'Write orders' is ticked.",
         script="trader-budget/size.py", fixed=[],
         params=[P("targets", "Targets", "select", "trader-budget/targets.example.json", "--targets",
                   choices=["trader/targets.json", "trader-budget/targets.example.json"]),
                 P("config", "Config", "select", "trader-budget/config.example.json", "--config",
                   choices=["trader/config.json", "trader-budget/config.example.json"]),
                 P("budget", "Budget ($)", "number", "5000", "--budget"),
                 P("top", "Top N", "number", "", "--top"),
                 P("write", "Write trader/orders.json", "check", flag="--write")]),
    dict(id="risk", group="Trade", title="Risk check",
         help="PASS or BLOCK for every proposed order in trader/orders.json.",
         script="trader-risk/check.py", fixed=[],
         params=[P("nowrite", "Do not record verdicts", "check", "1", "--no-write")]),
    dict(id="broker-show", group="Trade", title="Order tickets",
         help="Shows each ticket, READY or not. Never places anything.",
         script="trader-broker/ticket.py", fixed=["show"], params=[]),
    dict(id="broker-log", group="Trade", title="Today's decisions",
         help="The yes / no log from trader-broker.",
         script="trader-broker/ticket.py", fixed=["log"], params=[]),
    dict(id="rebalance", group="Trade", title="Rebalance drift",
         help="The drift table against your target weights.",
         script="trader-rebalance/rebalance.py", fixed=[],
         params=[P("band", "Band (points)", "number", "5", "--band")]),
    dict(id="tax-report", group="Tax", title="Gains and losses",
         help="Unrealized gains and losses by lot. Not tax advice.",
         script="trader-tax/lots.py", fixed=["report"],
         params=[P("lots", "Show every lot", "check", flag="--lots")]),
    dict(id="tax-harvest", group="Tax", title="Harvest candidates",
         help="Tax-loss harvesting candidates with wash-sale checks.",
         script="trader-tax/lots.py", fixed=["harvest"],
         params=[P("minloss", "Min loss ($)", "number", "100", "--min-loss")]),
]
BY_ID = {c["id"]: c for c in COMMANDS}


class BadInput(ValueError):
    pass


def build_args(cmd, values):
    """Turn form values into an argument list, rejecting anything off-script."""
    flagged, positional = [], []
    for p in cmd["params"]:
        raw = values.get(p["name"], p["default"])
        if p["kind"] == "check":
            if raw in (True, "1", 1, "true"):
                flagged.append(p["flag"])
            continue
        raw = "" if raw is None else str(raw).strip()
        if not raw:
            continue
        if p["kind"] == "number":
            if not re.fullmatch(r"\d{1,9}(\.\d{1,4})?", raw):
                raise BadInput(f"{p['label']}: expected a number")
            parts = [raw]
        elif p["kind"] == "select":
            if raw not in p["choices"]:
                raise BadInput(f"{p['label']}: not an allowed choice")
            parts = [raw]
        else:
            if not SAFE_TEXT.match(raw) or raw.startswith("-"):
                raise BadInput(f"{p['label']}: letters, digits and . , ' & @ : / = + - only, "
                               "and it cannot start with a dash")
            parts = raw.split() if p["multi"] else [raw]
            if any(x.startswith("-") for x in parts):
                raise BadInput(f"{p['label']}: items cannot start with a dash")
        if p["positional"]:
            positional.extend(parts)
        else:
            flagged.append(p["flag"])
            flagged.extend(parts)
    # options first, then positionals (argparse accepts either order; "--" guards them)
    return flagged + (["--"] + positional if positional else [])


def run_command(cmd_id, values):
    cmd = BY_ID.get(cmd_id)
    if not cmd:
        raise BadInput("unknown command")
    extra = build_args(cmd, values)
    argv = [sys.executable, "-u", str(ROOT / cmd["script"]), *cmd["fixed"], *extra]
    shown = "python " + " ".join([cmd["script"], *cmd["fixed"], *extra])
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    ua = str(values.get("_sec_user_agent", "")).strip()
    if ua and re.fullmatch(r"[A-Za-z0-9 ._@+-]{3,120}", ua):
        env["SEC_USER_AGENT"] = ua
    start = time.time()
    try:
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                              timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        code, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = -1, e.stdout or b"", (e.stderr or b"") + f"\n[timed out after {TIMEOUT}s]".encode()
    dec = lambda b: b.decode("utf-8", "replace")[:MAX_OUT]
    return dict(command=shown, view=cmd.get("view", ""), code=code, stdout=dec(out), stderr=dec(err),
                seconds=round(time.time() - start, 2))


def watch_action(body, asset="gold"):
    """Control one asset's alert watcher. Settings are validated; nothing here places orders."""
    w = WATCHERS[asset]
    action = str(body.get("action", ""))
    if action == "settings":
        w.configure(body.get("settings"))
    elif action == "start":
        if "settings" in body:
            w.configure(body.get("settings"))
        w.start()
    elif action == "stop":
        w.stop()
    elif action == "check":
        w.check_once()
    elif action == "test":
        w.test()
    elif action == "clear":
        w.clear()
    else:
        raise BadInput("unknown action")
    return w.status()


SID = re.compile(r"^[a-z_]{3,30}-[A-Za-z0-9]{2,6}-\d{9,11}-(long|short)-[0-9a-f]{6}$")


def _f(v, name, optional=False):
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e12:
        raise BadInput("%s must be a number" % name)
    return float(v)


def portfolio_action(body):
    """Store the positions, pending orders and limits you typed in. Validated; read-only arithmetic only."""
    if body.get("action") == "get":
        return portfolio.load()
    try:
        return portfolio.save(body.get("state"))
    except ValueError as e:
        raise BadInput(str(e))


def journal_action(body):
    """Append to the local trading journal. Nothing here touches a broker."""
    action = str(body.get("action", ""))
    try:
        if action == "get":
            ent = journal.load()
            return dict(entries=journal.recent(ent, 40), summary=journal.analyse(ent))
        if action == "add_signal":
            s = body.get("signal") or {}
            if not SID.match(str(s.get("id", ""))) or s.get("strategy") not in strategies.STRATEGIES or s.get("side") not in ("long", "short"):
                raise BadInput("not a valid signal")
            sig = dict(id=s["id"], strategy=s["strategy"], side=s["side"], decided_t=int(_f(s.get("decided_t"), "decided_t")), price=_f(s.get("price"), "price"),
                       stop=_f(s.get("stop"), "stop"), targets=[_f(t, "target") for t in (s.get("targets") or [])[:5]],
                       note=str(s.get("note", ""))[:300], invalidation=str(s.get("invalidation", ""))[:300], regime=str(s.get("regime", ""))[:30])
            for k in ("note", "invalidation"):
                if not journal.TEXT.match(sig[k]):
                    raise BadInput("%s has unsupported characters" % k)
            sym, iv = str(body.get("symbol", "")), str(body.get("interval", ""))
            if sym not in ("GC=F", "MGC=F", "BTC-USD") or iv not in ("60m", "15m"):
                raise BadInput("unknown market or interval")
            val = research.load_validation().get(research.key(sym, iv)) or {}
            srec = (val.get("strategies") or {}).get(sig["strategy"]) or {}
            model = ml.latest_accepted(sym, iv)
            reasons = [str(r)[:200] for r in (body.get("reasons") or [])[:10]]
            ctx = dict(symbol=sym, interval=iv, code_version=backtester.code_version(), strategy_status=srec.get("status", "NOT RUN"),
                       model_id=model["id"] if model else None, data_ref=dict((val.get("data") or {}), checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())) if val else None)
            return journal.add_signal(sig, str(body.get("decision", "")), reasons, ctx)
        sid = str(body.get("id", ""))
        if not SID.match(sid):
            raise BadInput("not a valid signal id")
        if action == "fill":
            return journal.record_fill(sid, _f(body.get("price"), "price"), _f(body.get("units"), "units"))
        if action == "close":
            tags = [t for t in str(body.get("tags", "")).split(",") if t.strip()]
            return journal.close_trade(sid, _f(body.get("price"), "price"), notes=str(body.get("notes", ""))[:500], tags=[t.strip() for t in tags][:8],
                                       stop_moved=bool(body.get("stop_moved")))
    except (ValueError, KeyError) as e:
        raise BadInput(str(e)[:200])
    raise BadInput("unknown action")


def pending_settings_action(body):
    """Save the broker profile and risk limits you typed in. Validated; these are numbers for arithmetic, not credentials."""
    if body.get("action") == "get":
        return dict(settings=orders.load_settings(), saved=orders.settings_saved())
    try:
        return dict(settings=orders.save_settings(body.get("settings")), saved=True)
    except ValueError as e:
        raise BadInput(str(e)[:300])


def paper_action(body):
    """Paper orders only. A plan is rebuilt on the server from fresh data; nothing here reaches a broker."""
    action = str(body.get("action", ""))
    try:
        if action == "get":
            return pending.paper_view()
        if action == "update":
            sym, iv = str(body.get("symbol", "GC=F")), str(body.get("interval", "60m"))
            if sym not in ("GC=F", "MGC=F") or iv not in ("60m", "15m"):
                raise BadInput("unknown market or interval")
            out = pending.refresh_paper(sym, iv, events=macro_events())
            return dict(pending.paper_view(), changed=out["changed"])
        if action == "cancel":
            allo = paper.load()
            sink = []
            paper.cancel(allo, str(body.get("id", "")), str(body.get("reason") or "cancelled by you"), sink=sink)
            paper.save(allo)
            paper._audit(sink)
            return pending.paper_view()
        if action == "create":
            sym, iv, typ = str(body.get("symbol", "")), str(body.get("interval", "")), str(body.get("order_type", ""))
            if sym not in ("GC=F", "MGC=F") or iv not in ("60m", "15m"):
                raise BadInput("unknown market or interval")
            made = pending.create_paper(sym, iv, typ, bool(body.get("acknowledge_unvalidated")), bool(body.get("oco")))
            return dict(pending.paper_view(), created=[dict(id=o["id"], order_state=o["order_state"], reasons=o["reasons"]) for o in made])
    except BadInput:
        raise
    except (ValueError, SystemExit) as e:
        raise BadInput(str(e)[:300])
    raise BadInput("unknown action")


def macro_events():
    import macro
    return macro.load_store()


def list_files():
    if not TRADER_DIR.is_dir():
        return []
    return sorted(p.name for p in TRADER_DIR.iterdir() if p.is_file() and SAFE_FILE.match(p.name))


def read_file(name):
    if not SAFE_FILE.match(name):
        raise BadInput("bad file name")
    path = (TRADER_DIR / name).resolve()
    if path.parent != TRADER_DIR.resolve() or not path.is_file():
        raise BadInput("no such file")
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_OUT]


class Handler(BaseHTTPRequestHandler):
    server_version = "TraderWeb"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _host_ok(self):
        # blocks DNS-rebinding: only our own address may be used as Host
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj))

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/commands":
            return self._json(200, {"commands": COMMANDS, "root": str(ROOT),
                                    "python": sys.version.split()[0]})
        m = WATCH_PATH.match(path)
        if m:
            return self._json(200, WATCHERS[m.group(1)].status())
        if path == "/api/portfolio":
            return self._json(200, portfolio.load())
        if path == "/api/pending/settings":
            return self._json(200, dict(settings=orders.load_settings(), saved=orders.settings_saved()))
        if path == "/api/paper_orders":
            return self._json(200, pending.paper_view())
        if path == "/api/policy":
            return self._json(200, strategy_policy.load())
        if path == "/api/gold/command-centre":
            try:
                return self._json(200, command_centre.build(light="light=1" in query.split("&")))
            except (Exception, SystemExit) as e:                      # an outage must show as an error, never as a made-up price
                return self._json(502, {"error": "analysis unavailable: %s" % str(e)[:200]})
        if path == "/api/journal":
            ent = journal.load()
            return self._json(200, dict(entries=journal.recent(ent, 40), summary=journal.analyse(ent)))
        if path == "/api/files":
            return self._json(200, {"files": list_files()})
        if path == "/api/file":
            name = dict(x.split("=", 1) for x in query.split("&") if "=" in x).get("name", "")
            try:
                return self._json(200, {"name": name, "text": read_file(name)})
            except BadInput as e:
                return self._json(400, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        wm = WATCH_PATH.match(self.path)
        if self.path not in ("/api/run", "/api/portfolio", "/api/journal", "/api/pending/settings", "/api/paper_orders", "/api/policy", "/api/gold/command-centre/quote") and not wm:
            return self._json(404, {"error": "not found"})
        # JSON content type forces a CORS preflight, which we never grant,
        # so other websites cannot trigger runs from the user's browser.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"error": "json only"})
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._json(403, {"error": "bad origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 20_000)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            if wm:
                return self._json(200, watch_action(body, wm.group(1)))
            if self.path == "/api/portfolio":
                return self._json(200, portfolio_action(body))
            if self.path == "/api/journal":
                return self._json(200, journal_action(body))
            if self.path == "/api/pending/settings":
                return self._json(200, pending_settings_action(body))
            if self.path == "/api/paper_orders":
                return self._json(200, paper_action(body))
            if self.path == "/api/gold/command-centre/quote":
                try:
                    return self._json(200, command_centre.save_quote(body.get("bid"), body.get("ask")))
                except ValueError as e:
                    raise BadInput(str(e))
            if self.path == "/api/policy":
                try:
                    return self._json(200, strategy_policy.save(body.get("strict")))
                except ValueError as e:
                    raise BadInput(str(e))
            result = run_command(str(body.get("id", "")), body.get("values") or {})
        except BadInput as e:
            return self._json(400, {"error": str(e)})
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request"})
        self._json(200, result)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Trader web view: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    print(f"Running commands from {ROOT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
