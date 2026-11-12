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

ROOT = Path(__file__).resolve().parent.parent
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
         script="trader-news/news.py", fixed=[],
         params=[P("tickers", "Tickers", default="AAPL KO", flag="--tickers", multi=True),
                 P("hours", "Hours back", "number", "24", "--hours"),
                 P("nosec", "Headlines only (skip SEC)", "check", flag="--no-sec"),
                 P("all", "Show seen items too", "check", flag="--all"),
                 P("dry", "Dry run (do not update state)", "check", "1", "--dry-run")]),
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
    return dict(command=shown, code=code, stdout=dec(out), stderr=dec(err),
                seconds=round(time.time() - start, 2))


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
        if self.path != "/api/run":
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
