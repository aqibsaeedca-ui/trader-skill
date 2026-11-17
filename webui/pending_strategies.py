"""BUY STOP, SELL STOP, BUY LIMIT and SELL LIMIT as four independent strategies. Standard library only.

Each follows the Strategy interface: it is fed one CLOSED bar at a time and returns an order decided at that bar's close,
alive for `expires` bars. The backtester fills it with its conservative pending-order model (a limit must be traded
through, a stop fills at the worse of the open and the trigger plus slippage, an unfilled order expires, an order whose
structural stop is closed through is cancelled).

Entry levels come only from confirmed structure (swings, previous day and week, sessions) that has not been traded
through. Buffers are in ATRs and are calibrated by walk-forward on a small grid, never fixed at a dollar amount. The stop
is the structural invalidation level plus a small ATR margin; it is never widened to make a position size fit, and a
setup whose structural stop is too far or too near is rejected instead.

The same classes produce the live recommendations, so what is shown is exactly what was tested.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategies  # noqa: E402

DEFAULT_MIN_STOP = 0.30        # broker minimum stop distance in price units (placeholder until a broker profile is set)


def regime_gate(order_type, snap, htf):
    """Which order types make sense in the current regime. Returns (ok, reason). Uses completed-bar information only."""
    if snap["label"] == "post_news":
        return False, "news-driven high-volatility regime: entries are blocked until it settles"
    buy = order_type.startswith("BUY")
    base, big = snap["trend"], (htf or {}).get("trend", "unknown")
    down = lambda t: t.endswith("bear")
    up = lambda t: t.endswith("bull")
    if buy and (base == "strong_bear" or big == "strong_bear" or (down(base) and down(big))):
        return False, "established downtrend: buys are suppressed (no validated counter-trend logic)"
    if not buy and (base == "strong_bull" or big == "strong_bull" or (up(base) and up(big))):
        return False, "established uptrend: sells are suppressed (no validated counter-trend logic)"
    if order_type.endswith("STOP") and base == "sideways" and not (snap["vol"] == "expansion" or (snap["vol_ratio"] or 0) >= 1.2):
        return False, "sideways market: a breakout order needs demonstrated volatility expansion"
    return True, ""


class PendingBase(strategies.Strategy):
    family = "pending"
    order_type = ""
    side = "long"
    kind = "stop"
    defaults = dict(expiry_bars=8, stop_atr=0.30, min_net_rr=1.0, max_dist_atr=2.0, min_risk_atr=0.6, max_risk_atr=3.5, min_touches=1,
                    touch_atr=0.25, max_cost_r=0.20, min_stop=DEFAULT_MIN_STOP, buffer_atr=0.15, limit_buf_atr=0.05)

    def reset(self):
        self.live = {}                       # level key -> bar index until which an order for it is already working
        self.noted = {}                      # (level key, reason) -> bar index, so a refusal is recorded once, not every bar

    # -- helpers
    def refuse(self, i, key, reason):
        if self.noted.get((key, reason), -10 ** 9) < i - 50:
            self.noted[(key, reason)] = i
            self.reject(i, "%s: %s" % (self.order_type, reason))

    def touches(self, bars, i, level, side):
        p = self.p
        win = bars[max(0, i - 60):i + 1]
        atr = self.atr_now
        t = 0
        for b in win:
            if side == "high" and b[1] >= level - p["touch_atr"] * atr and b[1] <= level + 0.5 * atr:
                t += 1
            if side == "low" and b[2] <= level + p["touch_atr"] * atr and b[2] >= level - 0.5 * atr:
                t += 1
        return t

    def structural_targets(self, levels, entry, risk, sg):
        """Next structure beyond the entry, then measured distances only to fill up to three. Returns (targets, measured_count)."""
        away = sorted((l["price"] for l in levels if (l["price"] - entry) * sg >= 0.8 * risk), key=lambda x: abs(x - entry))
        out = []
        for t in away:
            if not out or abs(t - out[-1]) >= 0.3 * risk:
                out.append(t)
            if len(out) == 3:
                break
        measured = 0
        for m in (1.5, 2.5, 4.0):
            if len(out) >= 3:
                break
            t = entry + sg * m * risk
            if all(abs(t - x) >= 0.3 * risk for x in out):
                out.append(t)
                measured += 1
        out.sort(key=lambda x: abs(x - entry))
        return out, measured

    def on_bar(self, A, i, htf=None):
        atr = A.atr[i]
        if atr is None or i < 60:
            return []
        self.atr_now = atr
        p, bars = self.p, A.bars
        price = bars[i][3]
        half = self.spread / 2.0
        ask, bid = price + half, price - half
        sg = 1 if self.side == "long" else -1
        sup, res = A.nearest(price)
        # the level this order is built on: for stops the resistance/support to be broken, for limits the one expected to hold
        if self.kind == "stop" and sg == 1:
            pool = [l for l in res if l["price"] >= ask]                      # resistance to be broken, above the Ask
        elif self.kind == "stop":
            pool = [l for l in sup if l["price"] <= bid]                      # support to be broken, below the Bid
        elif sg == 1:
            pool = [l for l in sup if bid - l["price"] >= p["min_stop"]]      # support expected to hold, far enough below
        else:
            pool = [l for l in res if l["price"] - ask >= p["min_stop"]]      # resistance expected to hold, far enough above
        if not pool:
            return []
        lv = pool[0]
        key = "%s@%.2f" % (lv["source"], lv["price"])
        if self.live.get(key, -1) >= i:
            return []
        market = ask if sg == 1 else bid
        if abs(lv["price"] - market) > p["max_dist_atr"] * atr:
            return []                                                    # too far away to matter yet
        side_kind = "high" if (self.kind == "stop") == (sg == 1) else "low"
        if self.touches(bars, i, lv["price"], side_kind) < p["min_touches"]:
            return []
        ok, why = regime_gate(self.order_type, A.snaps[i], htf)
        if not ok:
            self.refuse(i, key, why)
            return []
        if self.kind == "stop":
            buf = max(p["buffer_atr"] * atr, 2.0 * self.spread)
            entry = lv["price"] + sg * buf
            entry = max(entry, ask + p["min_stop"]) if sg == 1 else min(entry, bid - p["min_stop"])
            struct = [s["price"] for s in (A.swing_lows if sg == 1 else A.swing_highs) if (entry - s["price"]) * sg > 0][-1:]
            win = bars[max(0, i - 12):i + 1]
            fallback = min(b[2] for b in win) if sg == 1 else max(b[1] for b in win)
            anchor = struct[0] if struct else fallback
            stop = anchor - sg * p["stop_atr"] * atr
            beyond = [l for l in (res if sg == 1 else sup) if (l["price"] - lv["price"]) * sg > 0]
            others = [l for l in (res if sg == 1 else sup) if abs(l["price"] - entry) <= 0.3 * atr and l["key"] != lv["key"]]
        else:
            entry = lv["price"] + sg * p["limit_buf_atr"] * atr                  # a buy limit sits just ABOVE support, a sell limit just BELOW resistance
            entry = min(entry, ask - p["min_stop"]) if sg == 1 else max(entry, bid + p["min_stop"])
            stop = lv["price"] - sg * p["stop_atr"] * atr
            beyond = [l for l in (res if sg == 1 else sup)]
            others = []
        risk = (entry - stop) * sg
        if risk < p["min_risk_atr"] * atr or risk > p["max_risk_atr"] * atr:
            self.refuse(i, key, "the structural stop is %.1f ATR from the entry (allowed %.1f to %.1f); it will not be moved to fit" %
                        (risk / atr, p["min_risk_atr"], p["max_risk_atr"]))
            return []
        targets, measured = self.structural_targets(beyond, entry, risk, sg)
        reward = abs(targets[0] - entry) if targets else 0.0
        cpu = self.cost_per_unit
        net_rr = (reward - cpu) / (risk + cpu) if risk + cpu > 0 else 0.0
        if net_rr < p["min_net_rr"]:
            self.refuse(i, key, "reward to risk after costs is %.2f, below the %.2f minimum" % (net_rr, p["min_net_rr"]))
            return []
        if self.kind == "stop":
            word = "above" if sg == 1 else "below"
            note = "%s %s %.2f (%s) tested %d times; order %s it by a volatility buffer of %.2f." % (
                "Resistance" if sg == 1 else "Support", "at", lv["price"], lv["source"], self.touches(bars, i, lv["price"], side_kind), word, abs(entry - lv["price"]))
            inval = "Cancel if a bar closes %s %.2f (the structural stop) before the order fills, or after %d bars. A trigger that is then reversed back through the level is a liquidity sweep, not a confirmed %s." % (
                "below" if sg == 1 else "above", stop, p["expiry_bars"], "breakout" if sg == 1 else "breakdown")
        else:
            note = "%s %.2f (%s) is the pullback zone; the order rests %.2f %s it." % (
                "Support" if sg == 1 else "Resistance", lv["price"], lv["source"], abs(entry - lv["price"]), "above" if sg == 1 else "below")
            inval = "Cancel if a bar closes %s %.2f (the zone has failed) before the order fills, or after %d bars." % (
                "below" if sg == 1 else "above", stop, p["expiry_bars"])
        s = self.signal(A, i, self.side, entry, stop, targets, kind=self.kind, expires=p["expiry_bars"], note=note, invalidation=inval,
                        order_type=self.order_type, level=lv["price"], level_source=lv["source"], invalid_price=stop, measured_targets=measured,
                        liquidity_risk=bool(others), net_rr=net_rr, buffer=abs(entry - lv["price"]), bid=bid, ask=ask)
        if not s:
            return []
        self.live[key] = i + p["expiry_bars"]
        return [s]


class BuyStop(PendingBase):
    name, title, order_type, side, kind = "buy_stop", "BUY STOP (breakout continuation)", "BUY STOP", "long", "stop"
    grid = dict(buffer_atr=(0.10, 0.25), expiry_bars=(4, 8), stop_atr=(0.2, 0.5))


class SellStop(PendingBase):
    name, title, order_type, side, kind = "sell_stop", "SELL STOP (breakdown continuation)", "SELL STOP", "short", "stop"
    grid = dict(buffer_atr=(0.10, 0.25), expiry_bars=(4, 8), stop_atr=(0.2, 0.5))


class BuyLimit(PendingBase):
    name, title, order_type, side, kind = "buy_limit", "BUY LIMIT (pullback entry)", "BUY LIMIT", "long", "limit"
    grid = dict(limit_buf_atr=(0.0, 0.15), expiry_bars=(6, 12), stop_atr=(0.3, 0.6))


class SellLimit(PendingBase):
    name, title, order_type, side, kind = "sell_limit", "SELL LIMIT (pullback entry)", "SELL LIMIT", "short", "limit"
    grid = dict(limit_buf_atr=(0.0, 0.15), expiry_bars=(6, 12), stop_atr=(0.3, 0.6))


PENDING = {c.name: c for c in (BuyStop, SellStop, BuyLimit, SellLimit)}
BY_ORDER_TYPE = {c.order_type: c.name for c in PENDING.values()}
