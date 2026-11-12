#!/usr/bin/env python3
"""Gold alert watcher: tells you when price is about to reach a key level or a setup
trigger on the 15-minute, 1-hour or 4-hour chart. Standard library only.

It only reads delayed Yahoo bars and raises notifications. It never places orders.

Rules (per timeframe you enable, measured in that chart's own ATR):
  * KEY LEVEL   price comes within `near_atr` ATR of a key level (a price area where two or
                more timeframes line up, or three-plus levels stack on a 1h+ chart), or is inside it
  * SETUP       price comes within `near_atr` ATR of a setup's entry zone or breakout trigger,
                or reaches it (a breakout still needs the candle close; the alert says so)
Each rule fires once, then re-arms only after price moves more than max(1 ATR, 2x near) away,
and never more often than the per-timeframe cooldown.

Alerts go to: the page feed, desktop notifications while the page is open (if you allow
them), a Windows balloon notification from the server (optional) and trader/gold_alerts.json.
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold  # noqa: E402
import gold_intraday as gi  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER = os.path.join(ROOT, "trader")
SETTINGS_FILE = os.path.join(TRADER, "gold_watch.json")
LOG_FILE = os.path.join(TRADER, "gold_alerts.json")

DEFAULTS = dict(symbol="GC=F", tfs=["15m", "1h", "4h"], near_atr=0.5, poll_min=5, toast=True)
COOLDOWN_MIN = {"15m": 30, "1h": 90, "4h": 240}
POLLS = (2, 5, 10, 15)
FRIENDLY = {
    "LONG on a pullback": "Buy a dip into support",
    "LONG on a breakout": "Buy a break above resistance",
    "SHORT on a rally into resistance": "Sell a rally into resistance",
    "SHORT on a breakdown": "Sell a break below support",
}


def clean_settings(raw):
    """Validate untrusted settings; unknown or bad values fall back to defaults."""
    s = dict(DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("symbol") in ("GC=F", "MGC=F"):
        s["symbol"] = raw["symbol"]
    tfs = [t for t in (raw.get("tfs") or []) if t in gi.INTRADAY]
    if tfs:
        s["tfs"] = [t for t in gi.INTRADAY if t in tfs]
    try:
        n = float(raw.get("near_atr", s["near_atr"]))
        if 0.1 <= n <= 3:
            s["near_atr"] = round(n, 2)
    except (TypeError, ValueError):
        pass
    try:
        p = int(raw.get("poll_min", s["poll_min"]))
        if p in POLLS:
            s["poll_min"] = p
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("toast"), bool):
        s["toast"] = raw["toast"]
    return s


def money(x):
    return "$%s" % format(round(x), ",")


def span(lo, hi):
    return money(lo) if hi - lo < 1 else "%s-%s" % (money(lo), money(hi))


def toast(title, text):
    """Windows balloon notification via PowerShell. Fire and forget; harmless if it fails."""
    if os.name != "nt":
        return
    clean = lambda s: re.sub(r"[^A-Za-z0-9 .,:;$%/()+\-]", " ", s)[:180]
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
        "$n.ShowBalloonTip(10000, '%s', '%s', 'Info'); Start-Sleep -Seconds 11; $n.Dispose()"
        % (clean(title).replace("'", ""), clean(text).replace("'", ""))
    )
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                          "-EncodedCommand", enc], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def snapshot(res, tfs):
    """What the page shows under 'right now': nearest key levels per timeframe."""
    price = res["price"]
    sup, rs = gi.split_levels(res, only_key=True)
    rows = []
    for tf in tfs:
        atr = res["setup"][tf]["atr"]
        def near(lv):
            if not lv:
                return None
            d = gi.distance(price, lv["lo"], lv["hi"])
            return dict(lv, dist=d, dist_atr=d / atr)
        rows.append(dict(tf=tf, atr=atr, bias=res["setup"][tf]["bias"],
                         above=near(rs[0] if rs else None), below=near(sup[0] if sup else None)))
    return dict(price=price, asof=res["asof"], rows=rows, support=sup, resistance=rs)


def evaluate(res, settings, state, now):
    """Return the new alerts for this poll. `state` carries armed/last-fired per rule."""
    price = res["price"]
    near_atr = settings["near_atr"]
    levels = gi.key_levels(res)
    alerts = []

    def fire(key, tf, dist, atr, title, body, extra):
        st = state.setdefault(key, {"armed": True, "last": 0.0})
        if dist > max(1.0 * atr, 2 * near_atr * atr):
            st["armed"] = True
        if dist <= near_atr * atr and st["armed"] and now - st["last"] >= COOLDOWN_MIN[tf] * 60:
            st["armed"], st["last"] = False, now
            alerts.append(dict(ts=datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                               tf=tf, title=title, body=body, price=price, **extra))

    for tf in settings["tfs"]:
        c = res["setup"][tf]
        atr = c["atr"]
        for l in levels:
            d = gi.distance(price, l["lo"], l["hi"])
            side = "above" if l["mid"] > price else "below"
            where = ("inside" if d == 0 else "%.1f ATR %s" % (d / atr, side))
            title = "[%s] Price %s key level %s" % (tf, "is inside" if d == 0 else "near", span(l["lo"], l["hi"]))
            body = "%s is %s the %s level (%s agree). %s." % (
                money(price), where if d else "inside", span(l["lo"], l["hi"]), "+".join(l["tfs"]),
                "; ".join(l["signals"][:3]))
            fire(("lvl", tf, round(l["mid"])), tf, d, atr, title, body,
                 dict(kind="level", level=l["mid"], tfs=l["tfs"]))
        for s in c["setups"]:
            z = s["zone"]
            name = FRIENDLY.get(s["name"], s["name"])
            risk = abs(s["level"] - s["stop"])
            r1 = abs(s["targets"][0] - s["level"]) / risk if s["targets"] and risk else None
            tail = "Stop %s, first target %s%s." % (
                money(s["stop"]), money(s["targets"][0]) if s["targets"] else "n/a",
                " (%.1fR)" % r1 if r1 else "")
            tr = {"with": "with the trend", "counter": "against the trend", "range": "no clear trend"}[s["trend"]]
            if s["kind"] == "pullback":
                long_ = s["side"] == "long"
                if (long_ and price < z["lo"]) or (not long_ and price > z["hi"]):
                    continue                                   # already through the zone
                d = gi.distance(price, z["lo"], z["hi"])
                state_txt = "price is inside the entry zone" if d == 0 else "price is %.1f ATR from the entry zone" % (d / atr)
                title = "[%s] %s: %s" % (tf, name, "zone reached" if d == 0 else "approaching")
                body = "%s: %s (%s). %s" % (state_txt[0].upper() + state_txt[1:], span(z["lo"], z["hi"]), tr, tail)
            else:
                trig = s["level"]
                long_ = s["side"] == "long"
                crossed = price >= trig if long_ else price <= trig
                d = 0.0 if crossed else abs(trig - price)
                title = "[%s] %s: %s" % (tf, name, "trigger level touched" if crossed else "approaching trigger")
                body = "Trigger %s: needs a %s close %s it to confirm. Price %s%s (%s). %s" % (
                    money(trig), tf, "above" if long_ else "below", money(price),
                    "" if crossed else ", %.1f ATR away" % (d / atr), tr, tail)
            fire(("set", tf, s["name"], round(s["level"])), tf, d, atr, title, body,
                 dict(kind="setup", level=s["level"], setup=s["name"]))
    return alerts


class Watcher:
    def __init__(self):
        self.lock = threading.Lock()
        self.settings = self._load_settings()
        self.alerts = deque(self._load_log(), maxlen=200)
        self.state = {}
        self.running = False
        self.thread = None
        self.stop_evt = threading.Event()
        self.last_check = None
        self.last_error = ""
        self.snap = None
        self.next_check = None
        self.seq = max([a.get("id", 0) for a in self.alerts] + [0])

    # ---- persistence (best effort)
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return clean_settings(json.load(f))
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def _load_log(self):
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return [a for a in data if isinstance(a, dict)][-200:]
        except (OSError, ValueError):
            return []

    def _save(self, path, obj):
        try:
            os.makedirs(TRADER, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass

    # ---- control
    def configure(self, raw):
        with self.lock:
            self.settings = clean_settings(raw)
            self.state = {}                       # new rules, fresh start
        self._save(SETTINGS_FILE, self.settings)

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.stop_evt.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True, name="gold-watch")
            self.thread.start()

    def stop(self):
        with self.lock:
            self.running = False
            self.next_check = None
        self.stop_evt.set()

    def _loop(self):
        while not self.stop_evt.is_set():
            self.check_once()
            wait = self.settings["poll_min"] * 60
            with self.lock:
                self.next_check = time.time() + wait
            if self.stop_evt.wait(wait):
                break

    def check_once(self):
        with self.lock:
            settings = dict(self.settings)
        try:
            res = gi.analyse(settings["symbol"])
            now = time.time()
            with self.lock:
                new = evaluate(res, settings, self.state, now)
                self.snap = snapshot(res, settings["tfs"])
                self.last_check, self.last_error = now, ""
                for a in new:
                    self.seq += 1
                    a["id"] = self.seq
                    self.alerts.append(a)
                log = list(self.alerts)
            if new:
                self._save(LOG_FILE, log)
                if settings["toast"]:
                    head = new[0]
                    more = " (+%d more)" % (len(new) - 1) if len(new) > 1 else ""
                    toast("Gold alert " + head["title"][:60], head["body"][:140] + more)
            return new
        except (Exception, SystemExit) as e:          # SystemExit comes from the data fetcher
            with self.lock:
                self.last_error = str(e)[:300]
                self.last_check = time.time()
            return []

    def test(self):
        with self.lock:
            self.seq += 1
            a = dict(id=self.seq, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), tf="test",
                     title="Test alert", body="If you can read this, notifications work. No real signal.",
                     price=None, kind="test")
            self.alerts.append(a)
            settings = dict(self.settings)
            log = list(self.alerts)
        self._save(LOG_FILE, log)
        if settings["toast"]:
            toast("Gold alert test", a["body"])
        return a

    def clear(self):
        with self.lock:
            self.alerts.clear()
        self._save(LOG_FILE, [])

    def status(self):
        with self.lock:
            return dict(running=self.running, settings=dict(self.settings), last_check=self.last_check,
                        next_check=self.next_check, error=self.last_error, snapshot=self.snap,
                        alerts=list(self.alerts)[::-1][:60], now=time.time())


if __name__ == "__main__":
    w = Watcher()
    w.configure({"tfs": ["15m", "1h", "4h"], "near_atr": 3})
    out = w.check_once()
    st = w.status()
    print("price", st["snapshot"]["price"] if st["snapshot"] else None, "error:", st["error"] or "none")
    for a in out[:12]:
        print(" ", a["title"], "|", a["body"])
