"""Market regimes and market structure, built so nothing repaints. Standard library only.

Every concept is defined by an explicit rule below and is only reported once its confirmation condition has
been satisfied on a CLOSED bar. An event records the bar it was confirmed on (`i`); later bars never change
it. tests/test_webui_regimes.py proves it: running on bars[:n] gives the same snapshot and events as running on
all the bars and looking at bar n-1.

Definitions
-----------
swing high / low   bar j whose high (low) is >= the k bars before and > the k bars after. Confirmed at bar j+k.
HH, HL, LH, LL     a newly confirmed swing compared with the previous confirmed swing of the same kind.
structure bias     bullish: last confirmed high is HH and last confirmed low is HL. bearish: LH and LL. else range.
BOS                a bar CLOSES beyond the last confirmed swing in the direction of the current bias (or range).
CHoCH              a bar CLOSES beyond the last confirmed swing AGAINST the current bias.
liquidity level    a confirmed swing not yet traded through, the previous day/week high and low, and the
                   high and low of the last completed session.
liquidity sweep    price trades beyond a level by at least sweep_min_atr ATR and a bar CLOSES back on the
                   original side within sweep_reclaim_bars bars. Confirmed on that reclaim close, never earlier.
                   A close beyond the LAST confirmed swing is a BOS, not a sweep (the breakout rules own that
                   case). The multi-bar reclaim applies to older swings and to day, week and session levels.
                   Every confirmed sweep is an event; only a sweep of the previous day or week high or low that
                   went at least major_depth_atr ATR beyond the level ("major") sets the regime label, otherwise
                   the label would describe a third or more of all bars.
fair-value gap     three bars where bar i's low is above bar i-2's high (bullish) or bar i's high is below bar
                   i-2's low (bearish), larger than fvg_min_atr ATR. Confirmed on bar i. Tracked until filled.
order block        on a displacement bar (range >= ob_disp_atr ATR, body >= 60% of range) that also makes a BOS,
                   the last opposite-colour bar in the six bars before it. Confirmed on the displacement bar.
range anomaly      true-range z-score against the prior 20 bars >= anomaly_z. Real volume is used when given;
                   otherwise it is a range proxy and is labelled as such.
"""
import bisect
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicators as ind  # noqa: E402

DEFAULT_CFG = dict(
    k=3, atr_n=14, adx_n=14, er_n=20, slope_n=20, vol_fast=5, vol_slow=30, range_n=20,
    adx_strong=25.0, adx_weak=18.0, er_strong=0.30, er_sideways=0.22, slope_strong=0.05,
    vol_expansion=1.5, vol_compression=0.7,
    sweep_min_atr=0.10, sweep_reclaim_bars=3, sweep_recent=3, major_depth_atr=0.25,
    fvg_min_atr=0.20, ob_disp_atr=1.5, ob_lookback=6, anomaly_z=2.5,
    max_swings=40, news_window_bars=3)

SESSIONS = (("asia", 0, 7), ("london", 7, 13), ("ny", 13, 21), ("off", 21, 24))   # UTC hours


def session_of(t):
    h = (t % 86400) // 3600
    for name, a, b in SESSIONS:
        if a <= h < b:
            return name
    return "off"


def day_key(t):
    return t // 86400


def week_key(t):
    return (t // 86400 + 3) // 7                      # weeks start on Monday (1970-01-01 was a Thursday)


def interval_seconds(bars):
    d = sorted(bars[i + 1][0] - bars[i][0] for i in range(len(bars) - 1) if bars[i + 1][0] > bars[i][0])
    return d[len(d) // 2] if d else 3600


def post_news_flags(bars, event_times, window_bars, step):
    """True for bars that start within window_bars bars after a scheduled release. Release times are known in
    advance, so this uses no future information."""
    ev = sorted(event_times or [])
    out = []
    for b in bars:
        t = b[0]
        j = bisect.bisect_right(ev, t + step) - 1          # last event at or before this bar's end
        out.append(j >= 0 and t < ev[j] + window_bars * step)
    return out


def classify(f, cfg, post_news=False, recent_sweep=False):
    """Primary regime label plus its parts. f needs adx, pdi, mdi, er, slope_atr, vol_ratio."""
    adx, er, sl = f["adx"], f["er"], f["slope_atr"]
    if adx is None or er is None or sl is None:
        return dict(trend="unknown", vol="unknown", label="unknown")
    if adx >= cfg["adx_strong"] and er >= cfg["er_strong"] and abs(sl) >= cfg["slope_strong"]:
        trend = "strong_bull" if (f["pdi"] > f["mdi"] and sl > 0) else "strong_bear" if (f["mdi"] > f["pdi"] and sl < 0) else "weak_bull" if sl > 0 else "weak_bear"
    elif adx >= cfg["adx_weak"] and er >= cfg["er_sideways"]:
        trend = "weak_bull" if sl > 0 else "weak_bear"
    else:
        trend = "sideways"
    vr = f["vol_ratio"]
    vol = "unknown" if vr is None else "expansion" if vr >= cfg["vol_expansion"] else "compression" if vr <= cfg["vol_compression"] else "normal"
    if post_news:
        label = "post_news"
    elif recent_sweep and not trend.startswith("strong"):
        label = "sweep_reversal"
    elif vol == "expansion" and not trend.startswith("strong"):
        label = "vol_expansion"
    elif trend.startswith("strong"):
        label = trend
    elif vol == "compression" and trend == "sideways":
        label = "vol_compression"
    elif trend.startswith("weak"):
        label = "weak_trend"
    else:
        label = "sideways"
    return dict(trend=trend, vol=vol, label=label)


class Analysis:
    """Steps through bars once, in order. After step(i) the live state describes the market at the CLOSE of bar i
    and nothing later. Use run(callback) to be handed that live state at every bar."""

    def __init__(self, bars, cfg=None, volumes=None, event_times=None, interval_sec=None):
        self.bars = list(bars)
        self.cfg = dict(DEFAULT_CFG, **(cfg or {}))
        c = self.cfg
        self.step_sec = interval_sec or interval_seconds(self.bars)
        self.intraday = self.step_sec <= 7200
        self.volumes = volumes
        self.atr = ind.atr(self.bars, c["atr_n"])
        self.atr_fast = ind.atr(self.bars, c["vol_fast"])
        self.atr_slow = ind.atr(self.bars, c["vol_slow"])
        self.adx, self.pdi, self.mdi = ind.adx(self.bars, c["adx_n"])
        self.er = ind.efficiency_ratio(self.bars, c["er_n"])
        self.slope = ind.slope(self.bars, c["slope_n"])
        self.rz = ind.range_zscore(self.bars, c["range_n"])
        self.rv = ind.realised_vol(self.bars, c["range_n"])
        self.vz = self._volume_z() if volumes else None
        self.news = post_news_flags(self.bars, event_times, c["news_window_bars"], self.step_sec) if event_times else [False] * len(self.bars)
        # live state
        self.events, self.snaps = [], []
        self.swing_highs, self.swing_lows = [], []
        self.bias = "range"
        self.last_bos = self.last_choch = None
        self.recent_sweep = None
        self.fvgs, self.obs = [], []
        self.fixed_levels = {}              # prev day/week and session levels
        self.pending = {}                   # level key -> {extreme, start}
        self.dead = set()                   # level keys already swept or accepted
        self._day = self._week = self._sess = None
        self._run = {}
        self.i = -1

    # ---------------------------------------------------------------- helpers
    def _volume_z(self):
        n = self.cfg["range_n"]
        out = [None] * len(self.bars)
        for i in range(n, len(self.bars)):
            w = self.volumes[i - n:i]
            sd = statistics.pstdev(w)
            out[i] = (self.volumes[i] - sum(w) / n) / sd if sd else 0.0
        return out

    def _emit(self, typ, i, **kw):
        e = dict(type=typ, i=i, t=self.bars[i][0], **kw)
        self.events.append(e)
        return e

    def levels(self):
        """Active liquidity levels: dicts with key, price, side ('high' or 'low'), source."""
        out = []
        for s in self.swing_highs[-self.cfg["max_swings"]:]:
            if s["key"] not in self.dead:
                out.append(dict(key=s["key"], price=s["price"], side="high", source="swing high"))
        for s in self.swing_lows[-self.cfg["max_swings"]:]:
            if s["key"] not in self.dead:
                out.append(dict(key=s["key"], price=s["price"], side="low", source="swing low"))
        for key, lv in self.fixed_levels.items():
            if key not in self.dead:
                out.append(dict(key=key, price=lv["price"], side=lv["side"], source=lv["source"]))
        return out

    def nearest(self, price):
        """(support levels below, resistance levels above), nearest first."""
        lv = self.levels()
        sup = sorted([l for l in lv if l["price"] <= price], key=lambda l: -l["price"])
        res = sorted([l for l in lv if l["price"] > price], key=lambda l: l["price"])
        return sup, res

    # ---------------------------------------------------------------- one bar
    def step(self, i):
        if i != self.i + 1:
            raise ValueError("bars must be stepped in order")
        self.i = i
        bars, c = self.bars, self.cfg
        t, h, l, cl, o = bars[i][0], bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        atr = self.atr[i]
        self._calendar_levels(i)
        self._confirm_swings(i)
        bos = self._bos_choch(i, cl)
        self._sweeps(i, h, l, cl, atr)
        self._fvgs(i, h, l, atr)
        self._order_blocks(i, h, l, cl, o, atr, bos)
        if self.rz[i] is not None and self.rz[i] >= c["anomaly_z"]:
            self._emit("anomaly", i, z=self.rz[i], basis="range proxy (no volume in this data)")
        if self.vz and self.vz[i] is not None and self.vz[i] >= c["anomaly_z"]:
            self._emit("anomaly", i, z=self.vz[i], basis="volume")
        self.snaps.append(self._snapshot(i))
        return self.snaps[-1]

    def run(self, callback=None):
        for i in range(self.i + 1, len(self.bars)):
            self.step(i)
            if callback:
                callback(i, self)
        return self

    # ---------------------------------------------------------------- parts
    def _confirm_swings(self, i):
        k = self.cfg["k"]
        j = i - k
        if j < k:
            return
        bars = self.bars
        hj, lj = bars[j][1], bars[j][2]
        if all(hj >= bars[x][1] for x in range(j - k, j)) and all(hj > bars[x][1] for x in range(j + 1, j + k + 1)):
            prev = self.swing_highs[-1]["price"] if self.swing_highs else None
            lab = "first" if prev is None else "HH" if hj > prev else "LH" if hj < prev else "EQH"
            s = dict(key="sh%d" % j, price=hj, j=j, conf=i, label=lab)
            self.swing_highs.append(s)
            self._emit("swing", i, kind="high", price=hj, swing_i=j, label=lab)
        if all(lj <= bars[x][2] for x in range(j - k, j)) and all(lj < bars[x][2] for x in range(j + 1, j + k + 1)):
            prev = self.swing_lows[-1]["price"] if self.swing_lows else None
            lab = "first" if prev is None else "HL" if lj > prev else "LL" if lj < prev else "EQL"
            s = dict(key="sl%d" % j, price=lj, j=j, conf=i, label=lab)
            self.swing_lows.append(s)
            self._emit("swing", i, kind="low", price=lj, swing_i=j, label=lab)
        hl = self.swing_highs[-1]["label"] if self.swing_highs else None
        ll = self.swing_lows[-1]["label"] if self.swing_lows else None
        self.bias = "bullish" if (hl == "HH" and ll == "HL") else "bearish" if (hl == "LH" and ll == "LL") else "range"

    def _bos_choch(self, i, close):
        """Close beyond the last confirmed swing. BOS if with the prior bias, CHoCH if against it."""
        found = None
        if self.swing_highs and self.swing_highs[-1]["key"] not in self.dead and close > self.swing_highs[-1]["price"]:
            s = self.swing_highs[-1]
            typ = "choch" if self.bias == "bearish" else "bos"
            found = self._emit(typ, i, dir="bull", level=s["price"], swing_i=s["j"], prior_bias=self.bias)
            self.dead.add(s["key"])
        if self.swing_lows and self.swing_lows[-1]["key"] not in self.dead and close < self.swing_lows[-1]["price"]:
            s = self.swing_lows[-1]
            typ = "choch" if self.bias == "bullish" else "bos"
            found = self._emit(typ, i, dir="bear", level=s["price"], swing_i=s["j"], prior_bias=self.bias)
            self.dead.add(s["key"])
        if found:
            if found["type"] == "bos":
                self.last_bos = found
            else:
                self.last_choch = found
        return found

    def _sweeps(self, i, h, l, cl, atr):
        if atr is None:
            return
        c = self.cfg
        margin = c["sweep_min_atr"] * atr
        for lv in self.levels():
            key, p, side = lv["key"], lv["price"], lv["side"]
            if key in self.dead:
                continue
            pend = self.pending.get(key)
            if side == "high":
                if pend:
                    pend["extreme"] = max(pend["extreme"], h)
                    if cl < p:
                        self._confirm_sweep(i, lv, pend, "bear")
                    elif i - pend["start"] >= c["sweep_reclaim_bars"]:
                        self.pending.pop(key)
                        self.dead.add(key)                        # price accepted above: not a sweep
                elif h > p + margin:
                    pend = {"extreme": h, "start": i}
                    self.pending[key] = pend
                    if cl < p:
                        self._confirm_sweep(i, lv, pend, "bear")
            else:
                if pend:
                    pend["extreme"] = min(pend["extreme"], l)
                    if cl > p:
                        self._confirm_sweep(i, lv, pend, "bull")
                    elif i - pend["start"] >= c["sweep_reclaim_bars"]:
                        self.pending.pop(key)
                        self.dead.add(key)
                elif l < p - margin:
                    pend = {"extreme": l, "start": i}
                    self.pending[key] = pend
                    if cl > p:
                        self._confirm_sweep(i, lv, pend, "bull")

    def _confirm_sweep(self, i, lv, pend, reversal):
        self.pending.pop(lv["key"], None)
        self.dead.add(lv["key"])
        atr = self.atr[i]
        depth = abs(pend["extreme"] - lv["price"]) / atr if atr else 0.0
        keys = ("prev_week_",) if not self.intraday else ("prev_day_", "prev_week_")
        major = lv["key"].startswith(keys) and depth >= self.cfg["major_depth_atr"]
        ev = self._emit("sweep", i, level=lv["price"], source=lv["source"], swept="highs" if lv["side"] == "high" else "lows",
                        reversal=reversal, extreme=pend["extreme"], started=pend["start"], depth_atr=depth, major=major)
        if major:                                   # only a deep sweep of a prior day or week extreme sets the regime label
            self.recent_sweep = ev

    def _fvgs(self, i, h, l, atr):
        for g in self.fvgs:                                        # update older gaps first
            if g["status"] == "filled":
                continue
            if g["dir"] == "bull":
                if l <= g["lo"]:
                    g["status"] = "filled"
                    self._emit("fvg_filled", i, dir="bull", lo=g["lo"], hi=g["hi"], created=g["i"])
                elif l < g["hi"]:
                    g["status"] = "partial"
            else:
                if h >= g["hi"]:
                    g["status"] = "filled"
                    self._emit("fvg_filled", i, dir="bear", lo=g["lo"], hi=g["hi"], created=g["i"])
                elif h > g["lo"]:
                    g["status"] = "partial"
        self.fvgs = [g for g in self.fvgs if g["status"] != "filled" and i - g["i"] <= 300]
        if i < 2 or atr is None:
            return
        b0 = self.bars[i - 2]
        mn = self.cfg["fvg_min_atr"] * atr
        if l - b0[1] >= mn and l > b0[1]:
            g = dict(dir="bull", lo=b0[1], hi=l, i=i, status="open")
            self.fvgs.append(g)
            self._emit("fvg", i, dir="bull", lo=g["lo"], hi=g["hi"])
        elif b0[2] - h >= mn and h < b0[2]:
            g = dict(dir="bear", lo=h, hi=b0[2], i=i, status="open")
            self.fvgs.append(g)
            self._emit("fvg", i, dir="bear", lo=g["lo"], hi=g["hi"])

    def _order_blocks(self, i, h, l, cl, o, atr, bos):
        for ob in self.obs:                                        # invalidate first
            if ob["status"] == "active" and ((ob["dir"] == "bull" and cl < ob["lo"]) or (ob["dir"] == "bear" and cl > ob["hi"])):
                ob["status"] = "invalid"
        self.obs = [ob for ob in self.obs if ob["status"] == "active" and i - ob["i"] <= 300]
        if atr is None or not bos:
            return
        rng = h - l
        if rng < self.cfg["ob_disp_atr"] * atr or rng == 0 or abs(cl - o) < 0.6 * rng:
            return
        want_bear_candle = bos["dir"] == "bull"
        for j in range(i - 1, max(-1, i - 1 - self.cfg["ob_lookback"]), -1):
            bj = self.bars[j]
            if (bj[3] < bj[4]) == want_bear_candle and bj[3] != bj[4]:
                ob = dict(dir=bos["dir"], lo=bj[2], hi=bj[1], i=i, src=j, status="active")
                self.obs.append(ob)
                self._emit("ob", i, dir=bos["dir"], lo=ob["lo"], hi=ob["hi"], candle_i=j)
                break

    def _calendar_levels(self, i):
        t, h, l = self.bars[i][0], self.bars[i][1], self.bars[i][2]
        for name, key_fn, kind in (("day", day_key, "_day"), ("week", week_key, "_week")):
            k = key_fn(t)
            cur = getattr(self, kind)
            if cur is None or cur["k"] != k:
                if cur is not None:                                   # previous period just completed
                    for side, price in (("high", cur["h"]), ("low", cur["l"])):
                        key = "prev_%s_%s" % (name, side)
                        self.fixed_levels[key] = dict(price=price, side=side, source="previous %s %s" % (name, side))
                        self.dead.discard(key)
                        self.pending.pop(key, None)
                setattr(self, kind, {"k": k, "h": h, "l": l})
            else:
                cur["h"], cur["l"] = max(cur["h"], h), min(cur["l"], l)
        if not self.intraday:
            return
        sess = (day_key(t), session_of(t))
        if self._sess is None or self._sess["k"] != sess:
            if self._sess is not None:
                nm = self._sess["k"][1]
                for side, price in (("high", self._sess["h"]), ("low", self._sess["l"])):
                    key = "sess_%s_%s" % (nm, side)
                    self.fixed_levels[key] = dict(price=price, side=side, source="last %s session %s" % (nm, side))
                    self.dead.discard(key)
                    self.pending.pop(key, None)
            self._sess = {"k": sess, "h": h, "l": l}
        else:
            self._sess["h"], self._sess["l"] = max(self._sess["h"], h), min(self._sess["l"], l)

    def _snapshot(self, i):
        c = self.cfg
        fa, fs = self.atr_fast[i], self.atr_slow[i]
        atr = self.atr[i]
        sl = self.slope[i]
        f = dict(adx=self.adx[i], pdi=self.pdi[i], mdi=self.mdi[i], er=self.er[i],
                 slope_atr=(sl / atr if (sl is not None and atr) else None),
                 vol_ratio=(fa / fs if (fa and fs) else None))
        sweep_recent = bool(self.recent_sweep and i - self.recent_sweep["i"] <= c["sweep_recent"])
        reg = classify(f, c, self.news[i], sweep_recent)
        sh = self.swing_highs[-1]["price"] if self.swing_highs else None
        sl_ = self.swing_lows[-1]["price"] if self.swing_lows else None
        return dict(i=i, t=self.bars[i][0], close=self.bars[i][3], atr=atr, adx=f["adx"], pdi=f["pdi"], mdi=f["mdi"], er=f["er"],
                    slope_atr=f["slope_atr"], vol_ratio=f["vol_ratio"], trend=reg["trend"], vol=reg["vol"], label=reg["label"],
                    post_news=self.news[i], bias=self.bias, swing_high=sh, swing_low=sl_,
                    last_bos=self.last_bos["i"] if self.last_bos else None, last_choch=self.last_choch["i"] if self.last_choch else None,
                    sweep=(self.recent_sweep["reversal"] if sweep_recent else None), session=session_of(self.bars[i][0]))


def analyse(bars, cfg=None, volumes=None, event_times=None, interval_sec=None):
    return Analysis(bars, cfg, volumes, event_times, interval_sec).run()


def regime_diagnostics(bars, horizon=10, cfg=None):
    """Does each regime label separate what happens next? Per label: bars, mean |forward move| in ATRs and the
    mean forward move in the direction of the label (persistence). Research output; makes no claim by itself."""
    a = analyse(bars, cfg)
    rows = {}
    for s in a.snaps:
        i = s["i"]
        if i + horizon >= len(bars) or not s["atr"]:
            continue
        fwd = (bars[i + horizon][3] - s["close"]) / s["atr"]
        r = rows.setdefault(s["label"], dict(n=0, abs=0.0, dirsum=0.0, dirn=0))
        r["n"] += 1
        r["abs"] += abs(fwd)
        if s["trend"].endswith("bull") or s["trend"].endswith("bear"):
            r["dirsum"] += fwd if s["trend"].endswith("bull") else -fwd
            r["dirn"] += 1
    return {k: dict(n=v["n"], mean_abs_move_atr=v["abs"] / v["n"],
                    mean_directional_move_atr=(v["dirsum"] / v["dirn"] if v["dirn"] else None)) for k, v in rows.items()}
