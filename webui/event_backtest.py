"""Phase 12: event-driven order-lifecycle backtester. Standard library only. Read only: nothing here places or changes an order.

Bar-close events from every symbol and timeframe are processed in global time order. Each order is a small state machine:

  CREATED -> PENDING -> (TRIGGERED, breakout only) -> FILLED -> PARTIALLY CLOSED (target 1 reached, stop at the entry)
          -> one final status: CLOSED WIN, CLOSED LOSS, TIMED OUT, EXPIRED, INVALIDATED, or OPEN (data ended first)

The fill rules are the conservative ones of the scorecard grader (webui/scorecard.py, engine 2). This is a SECOND implementation written as an
event loop, so that `reconcile` can compare it against the grader and list every disagreement. Labelled: historical backtest, never a forecast.
"""
import heapq
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scorecard as sc  # noqa: E402

FINAL_STATUS = {"WIN": "CLOSED WIN", "LOSS": "CLOSED LOSS", "TIMED OUT": "TIMED OUT", "NOT TRIGGERED": "EXPIRED", "INVALIDATED": "INVALIDATED", "OPEN": "OPEN"}
BY_STATE = {v: k for k, v in FINAL_STATUS.items()}


class Order:
    """One recommendation's lifecycle. `on_bar` takes one closed bar (t, high, low, close, open) and returns True once the order is finished."""

    def __init__(self, rec):
        self.rec = rec
        self.step = sc.STEP[rec["tf"]]
        self.sgn = 1 if rec["side"] == "long" else -1
        self.stop, self.entry, self.tg, self.tick = rec["stop"], rec["entry"], rec["targets"], rec["tick"]
        self.risk = abs(self.entry - self.stop)
        self.status = "CREATED"
        self.history = [(rec["created_ts"], "CREATED")]
        self.waited, self.trig, self.idx = 0, None, 0
        self.filled, self.fill, self.fill_t = False, None, None
        self.k, self.rem, self.real, self.stop_cur = 0, 1.0, 0.0, self.stop
        self.first_limit_bar, self.bars_in, self.last_close = False, 0, None
        self.state, self.note, self.exit_t, self.closed = "WAITING", "", None, False
        self.done = False

    def _go(self, t, status):
        if status != self.status:
            self.status = status
            self.history.append((t, status))

    def _end(self, t, state, note, exit_time=None):
        self.state, self.note, self.done = state, note, True
        if exit_time is not None:
            self.exit_t = exit_time
        self._go(t, FINAL_STATUS.get(state, state))

    def _beyond_stop(self, c):
        return c < self.stop if self.sgn == 1 else c > self.stop

    def on_bar(self, b):
        t, hi, lo, cl, op = b
        rec, sgn = self.rec, self.sgn
        self.last_close = cl
        idx, self.idx = self.idx, self.idx + 1
        if self.status == "CREATED":
            self._go(t, "PENDING")
        if not self.filled:
            if rec["kind"] == "pullback":
                self.waited += 1
                if self.waited > rec["expiry_bars"]:
                    self._end(t, "NOT TRIGGERED", "expired before the entry was reached")
                    return True
                if (lo <= self.entry - self.tick) if sgn == 1 else (hi >= self.entry + self.tick):
                    self.filled, self.fill, self.fill_t, self.first_limit_bar = True, (min(self.entry, op) if sgn == 1 else max(self.entry, op)), t, True
                    if (self.fill - self.stop) * sgn <= 0:
                        self.filled = False
                        self._end(t, "INVALIDATED", "the bar opened beyond the stop", t + self.step)
                        return True
                    self._go(t, "FILLED")
                elif self._beyond_stop(cl):
                    self._end(t, "INVALIDATED", "closed beyond the stop before the entry", t + self.step)
                    return True
                else:
                    return False
            else:
                if self.trig is None:
                    self.waited += 1
                    if self.waited > rec["expiry_bars"]:
                        self._end(t, "NOT TRIGGERED", "no close beyond the entry before expiry")
                        return True
                    if (cl > self.entry) if sgn == 1 else (cl < self.entry):
                        self.trig = idx
                        self._go(t, "TRIGGERED")
                    elif self._beyond_stop(cl):
                        self._end(t, "INVALIDATED", "closed beyond the stop before the breakout", t + self.step)
                        return True
                    return False
                self.fill, self.fill_t, self.filled = op, t, True
                if (self.fill - self.stop) * sgn <= 0:
                    self.filled = False
                    self._end(t, "NOT TRIGGERED", "next bar opened beyond the stop")
                    return True
                if (self.tg[0] - self.fill) * sgn <= 0:
                    self.filled = False
                    self._end(t, "NOT TRIGGERED", "next bar opened beyond the first target")
                    return True
                self._go(t, "FILLED")
        # the position is live on this bar
        self.bars_in += 1
        hit_stop = (lo <= self.stop_cur) if sgn == 1 else (hi >= self.stop_cur)
        if hit_stop:
            px = min(self.stop_cur, op) if sgn == 1 else max(self.stop_cur, op)
            self.real += self.rem * (px - self.fill) * sgn / self.risk
            self.rem, self.closed, self.exit_t = 0.0, True, t + self.step
            self.note = "stopped out" if self.k == 0 else "stopped at the entry after target 1"
            self._finish_filled(t)
            return True
        if self.first_limit_bar:
            self.first_limit_bar = False
            return False
        while self.k < 3 and self.rem > 1e-9 and ((hi >= self.tg[self.k]) if sgn == 1 else (lo <= self.tg[self.k])):
            self.real += sc.SPLIT[self.k] * (self.tg[self.k] - self.fill) * sgn / self.risk
            self.rem -= sc.SPLIT[self.k]
            self.k += 1
            if self.k == 1:
                self.stop_cur = self.fill
                self._go(t, "PARTIALLY CLOSED")
        if self.k >= 1 and self.rem > 1e-9 and ((lo <= self.stop_cur) if sgn == 1 else (hi >= self.stop_cur)):
            self.rem, self.closed, self.exit_t = 0.0, True, t + self.step
            self.note = "stopped at the entry after target %d" % self.k
            self._finish_filled(t)
            return True
        if self.k == 3 or self.rem <= 1e-9:
            self.closed, self.exit_t, self.note = True, t + self.step, "all three targets reached"
            self._finish_filled(t)
            return True
        if self.bars_in >= rec["hold_bars"]:
            self.real += self.rem * (cl - self.fill) * sgn / self.risk
            self.rem, self.closed, self.exit_t = 0.0, True, t + self.step
            self.state = "WIN" if self.k >= 1 else "TIMED OUT"
            self.note = "holding limit reached"
            self.done = True
            self._go(t, FINAL_STATUS[self.state])
            return True
        return False

    def _finish_filled(self, t):
        self.state = "WIN" if self.k >= 1 else "LOSS"
        self.done = True
        self._go(t, FINAL_STATUS[self.state])

    def finish(self, now):
        """Called when the data ended and the order is still alive."""
        if self.done:
            return
        if self.filled:
            self.real += self.rem * ((self.last_close - self.fill) * self.sgn / self.risk if self.last_close is not None else 0.0)
            self.state = "OPEN"
            self.status = "OPEN"
            self.history.append((now, "OPEN"))
        elif now - self.rec["created_ts"] > (self.rec["expiry_bars"] + 1) * self.step:
            self.state = "NOT TRIGGERED"
            self.status = "EXPIRED"
            self.history.append((now, "EXPIRED"))

    def result(self):
        r = dict(state=self.state, status=self.status, fill_price=self.fill if self.filled else None, fill_time=sc._iso(self.fill_t) if self.filled else None,
                 exit_time=sc._iso(self.exit_t) if self.exit_t else None, r_gross=None, r_net=None, best_target=self.k)
        if self.filled and self.state in ("WIN", "LOSS", "TIMED OUT", "OPEN"):
            r["r_gross"] = self.real
            r["r_net"] = self.real - self.rec["cost"] / self.risk
        return r


def run(recs, bars_by_symbol, now, keep_history=0):
    """Process every record's bars in global time order. Returns (results by record id, status transition counts, up to `keep_history` order histories)."""
    import bisect
    orders = {}
    streams = {}
    heap = []
    for r in recs:
        bars = (bars_by_symbol.get(r["symbol"]) or {}).get(r["tf"])
        if not bars:
            continue
        key = (r["symbol"], r["tf"])
        if key not in streams:
            seq = [b for b in bars if b[0] + sc.STEP[r["tf"]] <= now]
            streams[key] = (seq, [b[0] for b in seq])
        seq, times = streams[key]
        i0 = bisect.bisect_left(times, r["created_ts"])
        o = Order(r)
        orders[r["id"]] = (o, key, i0)
        if i0 < len(seq):
            heapq.heappush(heap, (seq[i0][0] + o.step, r["id"], i0))
    while heap:
        t_close, rid, i = heapq.heappop(heap)
        o, key, _ = orders[rid]
        seq = streams[key][0]
        if o.on_bar(seq[i]):
            continue
        if i + 1 < len(seq):
            heapq.heappush(heap, (seq[i + 1][0] + o.step, rid, i + 1))
    transitions, hist = {}, []
    out = {}
    for rid, (o, key, _) in orders.items():
        o.finish(now)
        out[rid] = o.result()
        for _, st in o.history:
            transitions[st] = transitions.get(st, 0) + 1
        if len(hist) < keep_history and o.state in ("WIN", "LOSS") and o.k >= 1:
            hist.append(dict(id=rid, history=[dict(time=sc._iso(t), status=s) for t, s in o.history]))
    return out, transitions, hist


def reconcile(recs, bars_by_symbol, now, tol=1e-9, keep_history=3):
    """Compare this engine with the scorecard grader on every record. Lists every disagreement."""
    ev, transitions, hist = run(recs, bars_by_symbol, now, keep_history)
    n = same = 0
    bad = []
    for r in recs:
        if r["id"] not in ev:
            continue
        g = sc.grade(r, bars_by_symbol[r["symbol"]][r["tf"]], now)
        e = ev[r["id"]]
        n += 1
        ok = g["state"] == e["state"] and (g["r_net"] is None) == (e["r_net"] is None) and (g["r_net"] is None or abs(g["r_net"] - e["r_net"]) < tol) and g["fill_time"] == e["fill_time"]
        if ok:
            same += 1
        elif len(bad) < 25:
            bad.append(dict(id=r["id"], grader=dict(state=g["state"], r_net=g["r_net"], fill_time=g["fill_time"]), events=dict(state=e["state"], r_net=e["r_net"], fill_time=e["fill_time"])))
    return dict(compared=n, identical=same, agreement=(same / n if n else None), disagreements=bad, transitions=transitions, example_histories=hist,
                passed=bool(n and same / n >= 0.999))


# ------------------------------------------------------------------ risk-gate replay (descriptive) --------------------------------------------

RISK_PER_TRADE_PCT, MAX_OPEN, MAX_AGG_PCT, DAILY_LOCK_PCT = 0.25, 3, 1.0, 1.5


def gate_replay(trades):
    """trades: dicts with fill_time, exit_time (ISO), r_net, symbol, side. Chronological by fill time. Each trade risks RISK_PER_TRADE_PCT (1R)."""
    import loss_attribution as la
    rows = sorted(trades, key=lambda x: la._ts(x["fill_time"]))
    open_, day_loss, taken, skipped = [], {}, [], dict(max_positions=0, aggregate_risk=0, same_market_side=0, daily_lockout=0)
    realised = []                                                    # (exit ts, r) of every closed taken trade

    def settle(upto):
        keep = []
        for ex_ts, tr in open_:
            if ex_ts <= upto:
                d = int(ex_ts // 86400)
                day_loss[d] = day_loss.get(d, 0.0) + tr["r_net"]
                realised.append((ex_ts, tr["r_net"]))
            else:
                keep.append((ex_ts, tr))
        open_[:] = keep

    for tr in rows:
        t0 = la._ts(tr["fill_time"])
        settle(t0)
        d = int(t0 // 86400)
        lock = day_loss.get(d, 0.0) * RISK_PER_TRADE_PCT <= -DAILY_LOCK_PCT
        if lock:
            skipped["daily_lockout"] += 1
        elif len(open_) >= MAX_OPEN:
            skipped["max_positions"] += 1
        elif (len(open_) + 1) * RISK_PER_TRADE_PCT > MAX_AGG_PCT + 1e-9:
            skipped["aggregate_risk"] += 1
        elif any(o["symbol"] == tr["symbol"] and o["side"] == tr["side"] for _, o in open_):
            skipped["same_market_side"] += 1
        else:
            open_.append((la._ts(tr["exit_time"]), tr))
            taken.append(tr)
    settle(float("inf"))
    eq = peak = dd = 0.0
    for _, r in sorted(realised, key=lambda x: x[0]):
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    tot = sum(t["r_net"] for t in taken)
    allr = sum(t["r_net"] for t in rows)
    return dict(candidates=len(rows), taken=len(taken), skipped=skipped, total_r=tot, total_pct=tot * RISK_PER_TRADE_PCT, max_drawdown_r=dd, max_drawdown_pct=dd * RISK_PER_TRADE_PCT,
                ungated_total_r=allr, mean_r_taken=(tot / len(taken) if taken else None),
                settings=dict(risk_per_trade_pct=RISK_PER_TRADE_PCT, max_open=MAX_OPEN, max_aggregate_pct=MAX_AGG_PCT, daily_lockout_pct=DAILY_LOCK_PCT),
                label="historical backtest on recorded outcomes, not a forward result")
