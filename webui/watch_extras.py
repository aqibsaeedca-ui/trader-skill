"""Extra watcher alerts built from the intelligence dashboard. Standard library only. Never places or changes an order.

evaluate() looks at one dashboard snapshot and returns the NEW alerts. Each kind fires once per cause (a signal ID, a
bar, an event, a day) or after a cooldown, and everything shares an hourly cap. The state dict is plain JSON so the
watcher can save it and a restart does not repeat old alerts.

Kinds
  setup_confirmed    a validated strategy has a confirmed trigger and every gate passed
  setup_invalid      a setup we alerted on has since traded through its stop
  regime_change      the market regime changed on a newly closed bar, in a way that matters
  high_impact_event  a scheduled high-impact release is close
  abnormal_spread    the latest measured spread is far above its recent median (only with real bid/ask data)
  stale_data         the market should be open but the latest bar is old
  exposure           net exposure, open risk or position count is over your limits
  daily_loss         the daily loss limit is reached
  unprotected        an open position or pending order has no stop
  conflicting_orders pending orders that fight each other or an open position
"""
import time

MEANINGFUL = ("strong_bull", "strong_bear", "vol_expansion", "post_news", "sweep_reversal")
DEFAULTS = dict(event_lead_min=30, max_alerts_per_hour=12, extras_cooldown_min=60, spread_multiple=3.0)


def new_state():
    return dict(fired={}, live={}, regime=None, sent=[], suppressed=0, pending={})


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class _Gate:
    """Dedupe, cooldown and the hourly cap, in one place."""

    def __init__(self, state, settings, now):
        self.s, self.now = state, now
        self.cool = settings["extras_cooldown_min"] * 60
        self.cap = settings["max_alerts_per_hour"]
        self.out = []
        state["sent"] = [t for t in state.get("sent", []) if now - t < 3600]
        for k in [k for k, t in state["fired"].items() if now - t > 7 * 86400]:
            del state["fired"][k]                                   # forget old causes so the file stays small

    def fire(self, key, kind, title, body, price, once=False, **extra):
        last = self.s["fired"].get(key)
        if last is not None and (once or self.now - last < self.cool):
            return False
        if len(self.s["sent"]) >= self.cap:
            self.s["suppressed"] = self.s.get("suppressed", 0) + 1
            return False
        self.s["fired"][key] = self.now
        self.s["sent"].append(self.now)
        self.out.append(dict(ts=_iso(self.now), tf="intel", kind=kind, title=title, body=body, price=price, **extra))
        return True


def evaluate(intel, settings, state, now):
    cfg = dict(DEFAULTS, **{k: settings[k] for k in DEFAULTS if k in settings})
    cfg.setdefault("extras_cooldown_min", 60)
    g = _Gate(state, cfg, now)
    o, mon = intel["overview"], intel["risk_monitor"]
    price = o["price"]

    # 1. confirmed validated setups, and earlier ones that failed
    for a in intel["assessments"]:
        c = a["candidate"]
        if a["decision"] in ("LONG", "SHORT") and c:
            if g.fire("setup:" + c["id"], "setup_confirmed", "%s: %s setup confirmed (validated)" % (a["title"], c["side"]),
                      "%s on the %s chart. Entry about %.2f, stop %.2f, first target %.2f, reward to risk %.2f after costs. Evidence: %s. Read the full plan before acting; nothing is placed for you."
                      % (c["side"].upper(), intel["interval"], c["entry"], c["stop"], c["targets"][0], c["rr_net"], a["evidence"]["status"]), price, once=True,
                      strategy=a["strategy"], signal_id=c["id"]):
                state["live"][c["id"]] = dict(side=c["side"], stop=c["stop"], strategy=a["title"], at=now)
    for sid, s in list(state["live"].items()):
        beyond = price <= s["stop"] if s["side"] == "long" else price >= s["stop"]
        if beyond:
            g.fire("invalid:" + sid, "setup_invalid", "%s setup invalidated" % s["strategy"],
                   "Price %.2f has traded through the %s setup's stop at %.2f. The idea no longer holds; cancel any order you placed for it." % (price, s["side"], s["stop"]),
                   price, once=True, signal_id=sid)
            del state["live"][sid]
        elif now - s["at"] > 48 * 3600:
            del state["live"][sid]                                   # expired quietly

    # 2. a regime change on a newly closed bar
    bar = o["last_closed_bar"]
    prev = state.get("regime")
    if prev and prev["bar"] != bar and prev["label"] != o["regime"] and (o["regime"] in MEANINGFUL or prev["label"] in MEANINGFUL):
        g.fire("regime:" + bar, "regime_change", "Regime changed: %s to %s" % (prev["label"].replace("_", " "), o["regime"].replace("_", " ")),
               "The %s chart moved from %s to %s on the bar closed %s (trend %s, volatility %s). Strategy assumptions differ between regimes."
               % (intel["interval"], prev["label"], o["regime"], bar, o["trend"], o["volatility"]), price, once=True)
    if not prev or prev["bar"] != bar:
        state["regime"] = dict(bar=bar, label=o["regime"])

    # 3. scheduled high-impact releases
    lead = cfg["event_lead_min"] * 60
    for e in o["events"]["upcoming"]:
        if 0 <= e["ts"] - now <= lead:
            g.fire("event:%d" % e["ts"], "high_impact_event", "%s in %d min" % (e["name"], max(1, (e["ts"] - now) // 60)),
                   "%s is scheduled for %s (%s, %s). Spreads and volatility usually widen around releases; check open orders and stops." % (e["name"], e["utc"], e["source"], e["reliability"]),
                   price, once=True)

    # 4. spreads and stale data
    dq = o["data_quality"]["series"][0]
    if dq["stale"] and dq["market_open"]:
        g.fire("stale", "stale_data", "Market data looks stale", "The latest %s bar is %d minutes old while the market should be open. Do not rely on these levels until data resumes."
               % (dq["interval"], (dq["age_sec"] or 0) // 60), price)
    sp = intel.get("spread_series")
    if sp and len(sp) >= 20:
        med = sorted(sp)[len(sp) // 2]
        if med > 0 and sp[-1] > cfg["spread_multiple"] * med:
            g.fire("spread", "abnormal_spread", "Abnormal spread", "The latest spread %.2f is %.1f times its recent median %.2f. Fills will cost more than usual." % (sp[-1], sp[-1] / med, med), price)

    # 5. portfolio protection and limits (from the positions you entered)
    for text in mon["alerts"]:
        low = text.lower()
        if "no stop" in low or "has no stop" in low:
            g.fire("unprot:" + text, "unprotected", "Unprotected position or order", text + ". Add a stop yourself; nothing is changed automatically.", price)
        elif "daily loss limit" in low:
            g.fire("dayloss:" + _day(now), "daily_loss", "Daily loss limit reached", text + ". Your own rule says stop trading for today.", price, once=True)
        elif "pending" in low or "hedge" in low or "same time" in low or "both sides" in low:
            g.fire("conflict:" + text, "conflicting_orders", "Conflicting orders", text + ". Review whether you want both.", price)
        else:
            g.fire("exposure:" + text, "exposure", "Exposure over your limits", text, price)
    if mon["lockout"]:
        g.fire("dayloss:" + _day(now), "daily_loss", "Daily loss limit reached", "Today's result is at or below your daily loss limit. Your own rule says stop trading for today.", price, once=True)
    return g.out
