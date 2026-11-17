"""Four independently configurable strategies. Standard library only.

Each strategy is a state machine fed one CLOSED bar at a time through on_bar(A, i, htf). It may read only
A.bars[0..i] and the structure state in A after step(i); it returns signals decided at the close of bar i. The
backtester executes them from bar i+1. tests/test_webui_strategies.py checks that signals never change when
later bars are appended.

None of these is assumed to have an edge. A strategy is only switched on after the validation gate in
backtester.verdict() passes on out-of-sample data; until then it is research output.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SIDES = ("long", "short")


def _quantile(vals, q):
    s = sorted(vals)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class Strategy:
    name = "base"
    title = "Base"
    defaults = {}
    grid = {}                                   # parameter -> values to try in walk-forward optimisation

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError("unknown parameter(s) for %s: %s" % (self.name, sorted(unknown)))
        self.p = dict(self.defaults, **params)
        self.cost_per_unit = 0.0                # round-trip cost in price units; the backtester sets it
        self.spread = 0.0                       # full spread in price units, for strategies that place orders around bid and ask
        self.interval = ""
        self.rejected = []                      # (bar index, reason) for signals that were considered and refused
        self.reset()

    def reset(self):
        pass

    def reject(self, i, reason):
        self.rejected.append(dict(i=i, strategy=self.name, reason=reason))

    def signal(self, A, i, side, price, stop, targets, kind="market", expires=1, note="", invalidation="", **meta):
        """Build one signal, or return None (and record why) if it fails the common checks."""
        risk = (price - stop) if side == "long" else (stop - price)
        atr = A.atr[i]
        if risk <= 0:
            self.reject(i, "stop on the wrong side of entry")
            return None
        good = sorted({round(t, 6) for t in targets if ((t - price) if side == "long" else (price - t)) > 0.5 * risk},
                      reverse=(side == "short"))
        if not good:
            self.reject(i, "no target beyond half an R")
            return None
        if self.cost_per_unit and self.cost_per_unit / risk > self.p.get("max_cost_r", 0.2):
            self.reject(i, "costs take %.0f%% of the risk" % (100 * self.cost_per_unit / risk))
            return None
        snap = A.snaps[i]
        t = A.bars[i][0]
        sid = "%s-%s-%d-%s" % (self.name, self.interval or "x", t, side)
        sid += "-" + hashlib.sha1(("%s%.4f%.4f" % (sid, price, stop)).encode()).hexdigest()[:6]
        return dict(id=sid, strategy=self.name, side=side, kind=kind, price=price, stop=stop, targets=good,
                    expires=expires, decided_i=i, decided_t=t, regime=snap["label"], session=snap["session"],
                    atr=atr, risk=risk, note=note, invalidation=invalidation, meta=meta)

    def on_bar(self, A, i, htf=None):
        raise NotImplementedError


# ---------------------------------------------------------------------------------------- A
class BreakoutRetest(Strategy):
    name = "breakout_retest"
    title = "Breakout and retest"
    defaults = dict(n=20, max_width_atr=5.0, min_touches=2, touch_atr=0.25, buf=0.10, fb_bars=3, retest_bars=8,
                    retest_tol=0.30, retest_depth=0.60, stop_atr=0.30, max_cost_r=0.20, max_vol_ratio=2.0)
    grid = dict(n=(12, 20), buf=(0.05, 0.15), retest_bars=(6, 10))

    def reset(self):
        self.pending = []
        self.last_breakout = -10 ** 9
        self.false_breakouts = 0

    def on_bar(self, A, i, htf=None):
        p, bars = self.p, A.bars
        atr = A.atr[i]
        if atr is None or i < p["n"] + 2:
            return []
        snap = A.snaps[i]
        _, h, l, c, o = bars[i][0], bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        out = []
        for pb in list(self.pending):                       # 1. manage breakouts already seen
            age = i - pb["i0"]
            lv = pb["level"]
            long_ = pb["side"] == "long"
            back_inside = c < lv if long_ else c > lv
            too_deep = c < lv - p["retest_depth"] * pb["atr"] if long_ else c > lv + p["retest_depth"] * pb["atr"]
            if back_inside and age <= p["fb_bars"] or too_deep:
                self.pending.remove(pb)
                self.false_breakouts += 1
                self.reject(i, "false breakout (closed back inside the range)")
                continue
            if age > p["retest_bars"]:
                self.pending.remove(pb)
                self.reject(i, "no retest within %d bars" % p["retest_bars"])
                continue
            if age < 1 or snap["label"] == "post_news":
                continue
            tol = p["retest_tol"] * pb["atr"]
            touched = (l <= lv + tol and l >= lv - p["retest_depth"] * pb["atr"]) if long_ else (h >= lv - tol and h <= lv + p["retest_depth"] * pb["atr"])
            confirm = (c > lv and c > o) if long_ else (c < lv and c < o)
            if touched and confirm:
                self.pending.remove(pb)
                ext = min(b[2] for b in bars[pb["i0"]:i + 1]) if long_ else max(b[1] for b in bars[pb["i0"]:i + 1])
                stop = (min(lv, ext) - p["stop_atr"] * atr) if long_ else (max(lv, ext) + p["stop_atr"] * atr)
                risk = abs(c - stop)
                sg = 1 if long_ else -1
                measured = lv + sg * pb["height"]
                targets = [c + sg * 1.5 * risk, c + sg * 2.5 * risk, measured if (measured - c) * sg > 2.5 * risk else c + sg * 4 * risk]
                s = self.signal(A, i, pb["side"], c, stop, targets, expires=1,
                                note="Closed beyond a %d-bar range, retested the broken level and held." % p["n"],
                                invalidation="A close back inside the range (%s %.2f)." % ("below" if long_ok(pb) else "above", lv),
                                level=lv)
                if s:
                    out.append(s)
        # 2. look for a new breakout of a validated consolidation (bar i excluded from the range)
        win = bars[i - p["n"]:i]
        hi, lo = max(b[1] for b in win), min(b[2] for b in win)
        if hi - lo <= p["max_width_atr"] * atr and i - self.last_breakout > p["n"] and \
                (snap["vol_ratio"] is None or snap["vol_ratio"] <= p["max_vol_ratio"]):
            t_hi = sum(1 for b in win if b[1] >= hi - p["touch_atr"] * atr)
            t_lo = sum(1 for b in win if b[2] <= lo + p["touch_atr"] * atr)
            if t_hi >= p["min_touches"] and t_lo >= p["min_touches"]:
                strong = h - l > 0
                if c > hi + p["buf"] * atr and strong and (c - l) >= 0.6 * (h - l):
                    self.pending.append(dict(side="long", level=hi, i0=i, atr=atr, height=hi - lo))
                    self.last_breakout = i
                elif c < lo - p["buf"] * atr and strong and (h - c) >= 0.6 * (h - l):
                    self.pending.append(dict(side="short", level=lo, i0=i, atr=atr, height=hi - lo))
                    self.last_breakout = i
        return out


def long_ok(pb):
    return pb["side"] == "long"


# ---------------------------------------------------------------------------------------- B
class TrendContinuation(Strategy):
    name = "trend_continuation"
    title = "Trend continuation"
    defaults = dict(min_leg_atr=2.0, pb_min=0.30, pb_max=0.70, stop_atr=0.25, max_cost_r=0.20, require_htf=True)
    grid = dict(pb_min=(0.25, 0.382), pb_max=(0.618, 0.786), stop_atr=(0.25, 0.5))

    def reset(self):
        self.used = set()

    def on_bar(self, A, i, htf=None):
        p, bars = self.p, A.bars
        atr = A.atr[i]
        if atr is None or not A.swing_highs or not A.swing_lows or i < 3:
            return []
        if p["require_htf"] and htf is None:
            return []
        snap = A.snaps[i]
        if snap["label"] == "post_news":
            return []
        side = None
        if htf is not None:
            if htf["trend"] in ("strong_bull", "weak_bull") and htf["bias"] != "bearish":
                side = "long"
            elif htf["trend"] in ("strong_bear", "weak_bear") and htf["bias"] != "bullish":
                side = "short"
            if side is None:
                return []
        sh, sl = A.swing_highs[-1], A.swing_lows[-1]
        h, l, c, o = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        pc_h, pc_l = bars[i - 1][1], bars[i - 1][2]
        if side in (None, "long") and sl["j"] < sh["j"] and i > sh["j"] + 1 and sh["j"] not in self.used:
            leg = sh["price"] - sl["price"]
            if leg >= p["min_leg_atr"] * atr:
                pb_low = min(b[2] for b in bars[sh["j"] + 1:i + 1])
                depth = (sh["price"] - pb_low) / leg
                if p["pb_min"] <= depth <= p["pb_max"] and pb_low > sl["price"] and c > pc_h and c > o:
                    stop = pb_low - p["stop_atr"] * atr
                    r = c - stop
                    t1 = sh["price"] if sh["price"] - c >= r else c + r
                    s = self.signal(A, i, "long", c, stop, [t1, pb_low + 1.272 * leg, pb_low + 1.618 * leg], expires=1,
                                    note="Pulled back %.0f%% of the last up-leg inside an up-trend, then closed above the prior bar high." % (100 * depth),
                                    invalidation="A close below the pullback low %.2f." % pb_low, depth=depth, leg=leg)
                    self.used.add(sh["j"])
                    return [s] if s else []
        if side in (None, "short") and sh["j"] < sl["j"] and i > sl["j"] + 1 and sl["j"] not in self.used:
            leg = sh["price"] - sl["price"]
            if leg >= p["min_leg_atr"] * atr:
                pb_high = max(b[1] for b in bars[sl["j"] + 1:i + 1])
                depth = (pb_high - sl["price"]) / leg
                if p["pb_min"] <= depth <= p["pb_max"] and pb_high < sh["price"] and c < pc_l and c < o:
                    stop = pb_high + p["stop_atr"] * atr
                    r = stop - c
                    t1 = sl["price"] if c - sl["price"] >= r else c - r
                    s = self.signal(A, i, "short", c, stop, [t1, pb_high - 1.272 * leg, pb_high - 1.618 * leg], expires=1,
                                    note="Bounced %.0f%% of the last down-leg inside a down-trend, then closed below the prior bar low." % (100 * depth),
                                    invalidation="A close above the pullback high %.2f." % pb_high, depth=depth, leg=leg)
                    self.used.add(sl["j"])
                    return [s] if s else []
        return []


# ---------------------------------------------------------------------------------------- C
class LiquiditySweepReversal(Strategy):
    name = "liquidity_sweep"
    title = "Liquidity sweep reversal"
    defaults = dict(shift_bars=5, min_depth=0.15, stop_buf=0.20, max_risk_atr=3.0, min_risk_atr=0.5, allow_countertrend=False,
                    max_cost_r=0.20)
    grid = dict(shift_bars=(3, 6), min_depth=(0.10, 0.30), stop_buf=(0.10, 0.30))

    def reset(self):
        self.used = set()
        self.expired = set()

    def on_bar(self, A, i, htf=None):
        p, bars = self.p, A.bars
        atr = A.atr[i]
        if atr is None:
            return []
        snap = A.snaps[i]
        out = []
        recent = A.events[-400:]
        sweeps = [e for e in recent if e["type"] == "sweep" and i - e["i"] <= p["shift_bars"] + 1]
        ready = []
        for sw in sweeps:
            key = (sw["i"], sw["level"])
            if key in self.used or key in self.expired:
                continue
            if i - sw["i"] > p["shift_bars"]:
                self.expired.add(key)
                self.reject(i, "sweep without a structure shift within %d bars" % p["shift_bars"])
                continue
            shift = [e for e in recent if e["type"] in ("choch", "bos") and e["dir"] == sw["reversal"] and sw["i"] <= e["i"] <= i]
            if shift and snap["label"] != "post_news":
                ready.append((sw, shift))
        # several nearby levels can be swept by the same push; trade the most extreme one, once per direction
        chosen = []
        for want in ("bear", "bull"):
            grp = [r for r in ready if r[0]["reversal"] == want]
            if not grp:
                continue
            for sw, _ in grp:
                self.used.add((sw["i"], sw["level"]))
            chosen.append(max(grp, key=lambda r: r[0]["level"]) if want == "bear" else min(grp, key=lambda r: r[0]["level"]))
        for sw, shift in chosen:
            want = sw["reversal"]
            depth = abs(sw["extreme"] - sw["level"]) / atr
            if depth < p["min_depth"]:
                self.reject(i, "sweep too shallow (%.2f ATR)" % depth)
                continue
            if htf is not None and not p["allow_countertrend"]:
                if (want == "bear" and htf["trend"] == "strong_bull") or (want == "bull" and htf["trend"] == "strong_bear"):
                    self.reject(i, "reversal against a strong higher-timeframe trend")
                    continue
            b = bars[sw["i"]]
            rng = b[1] - b[2]
            if rng > 0 and ((want == "bear" and b[3] > b[2] + 0.5 * rng) or (want == "bull" and b[3] < b[2] + 0.5 * rng)):
                self.reject(i, "weak reclaim close")
                continue
            c = bars[i][3]
            sup, res = A.nearest(c)
            if want == "bear":
                stop = sw["extreme"] + p["stop_buf"] * atr
                r = stop - c
                lv = [x["price"] for x in sup if c - x["price"] >= r]
                targets = [lv[0] if lv else c - 1.5 * r, lv[1] if len(lv) > 1 else c - 2.5 * r, c - 4 * r]
                side = "short"
            else:
                stop = sw["extreme"] - p["stop_buf"] * atr
                r = c - stop
                lv = [x["price"] for x in res if x["price"] - c >= r]
                targets = [lv[0] if lv else c + 1.5 * r, lv[1] if len(lv) > 1 else c + 2.5 * r, c + 4 * r]
                side = "long"
            if not (p["min_risk_atr"] * atr <= r <= p["max_risk_atr"] * atr):
                self.reject(i, "stop distance %.1f ATR outside %.1f to %.1f" % (r / atr, p["min_risk_atr"], p["max_risk_atr"]))
                continue
            s = self.signal(A, i, side, c, stop, targets, expires=1,
                            note="Swept %s at %.2f, closed back inside, then %s confirmed the reversal." % (sw["swept"], sw["level"], shift[-1]["type"].upper()),
                            invalidation="Price trades beyond the sweep extreme %.2f." % sw["extreme"], sweep_level=sw["level"])
            if s:
                out.append(s)
        return out


# ---------------------------------------------------------------------------------------- D
def range_bounds(bars, i, lookback, hi_q=0.9, lo_q=0.1):
    """Range edges estimated from the distribution of highs and lows over the lookback, independent of the
    indicators that classify the regime."""
    w = bars[i - lookback + 1:i + 1]
    return _quantile([b[1] for b in w], hi_q), _quantile([b[2] for b in w], lo_q)


class RangeTrading(Strategy):
    name = "range"
    title = "Range trading"
    defaults = dict(range_lookback=40, range_min_bars=10, min_width_atr=2.0, max_width_atr=8.0, touch_atr=0.20, min_touches=2,
                    edge_atr=0.15, wick_frac=0.40, stop_atr=0.60, break_atr=0.50, max_cost_r=0.20)
    grid = dict(range_lookback=(30, 50), range_min_bars=(8, 12), stop_atr=(0.5, 0.8))

    def reset(self):
        self.run = 0
        self.cool_until = -1
        self.active = None                                             # (upper, lower) of the last validated range

    def on_bar(self, A, i, htf=None):
        p, bars = self.p, A.bars
        atr = A.atr[i]
        snap = A.snaps[i]
        c = bars[i][3]
        if self.active and atr is not None and (c > self.active[0] + p["break_atr"] * atr or c < self.active[1] - p["break_atr"] * atr):
            self.active = None                                         # the range is broken: wait for a new one
            self.run = 0
            self.cool_until = i + p["range_min_bars"]
            self.reject(i, "range invalidated by a close outside it")
            return []
        self.run = self.run + 1 if snap["trend"] == "sideways" else 0
        if atr is None or i < p["range_lookback"] or self.run < p["range_min_bars"] or i < self.cool_until:
            return []
        if snap["label"] == "post_news":
            return []
        upper, lower = range_bounds(bars, i, p["range_lookback"])
        width = upper - lower
        h, l, c, o = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        if not (p["min_width_atr"] * atr <= width <= p["max_width_atr"] * atr):
            return []
        win = bars[i - p["range_lookback"] + 1:i + 1]
        if sum(1 for b in win if b[1] >= upper - p["touch_atr"] * atr) < p["min_touches"] or \
                sum(1 for b in win if b[2] <= lower + p["touch_atr"] * atr) < p["min_touches"]:
            return []
        self.active = (upper, lower)
        rng = h - l
        if rng <= 0:
            return []
        mid = (upper + lower) / 2
        if l <= lower + p["edge_atr"] * atr and c > o and (min(o, c) - l) >= p["wick_frac"] * rng and c > lower:
            stop = lower - p["stop_atr"] * atr
            s = self.signal(A, i, "long", c, stop, [mid, upper - 0.2 * atr], expires=1,
                            note="Rejected the lower edge %.2f of a range that has held %d bars." % (lower, self.run),
                            invalidation="A close %.2f ATR below the range low." % p["break_atr"], upper=upper, lower=lower)
            return [s] if s else []
        if h >= upper - p["edge_atr"] * atr and c < o and (h - max(o, c)) >= p["wick_frac"] * rng and c < upper:
            stop = upper + p["stop_atr"] * atr
            s = self.signal(A, i, "short", c, stop, [mid, lower + 0.2 * atr], expires=1,
                            note="Rejected the upper edge %.2f of a range that has held %d bars." % (upper, self.run),
                            invalidation="A close %.2f ATR above the range high." % p["break_atr"], upper=upper, lower=lower)
            return [s] if s else []
        return []


STRATEGIES = {c.name: c for c in (BreakoutRetest, TrendContinuation, LiquiditySweepReversal, RangeTrading)}


def lookup(name):
    """A strategy class by name: the four core strategies, then the pending-order family."""
    if name in STRATEGIES:
        return STRATEGIES[name]
    import pending_strategies                      # imported late: it imports this module
    if name in pending_strategies.PENDING:
        return pending_strategies.PENDING[name]
    raise ValueError("unknown strategy %r" % name)


def make(name, **params):
    return lookup(name)(**params)


def param_grid(name):
    """All combinations of the strategy's small search grid, as a list of dicts."""
    g = lookup(name).grid
    combos = [{}]
    for k, vals in g.items():
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    return combos
