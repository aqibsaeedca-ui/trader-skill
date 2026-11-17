"""Paper pending orders: lifecycle, expiry, invalidation, OCO, audit log. Bars and plans are SYNTHETIC."""
import os
import shutil
import tempfile
import unittest

from _load import load

pp = load("webui", "paper")
bt = load("webui", "backtester")

T0 = 1_700_000_000
STEP = 3600
FREE = bt.costs()
CFG = dict(max_hold=20)


def bars_from(rows):
    return [(T0 + i * STEP, r[1], r[2], r[3], r[0]) for i, r in enumerate(rows)]       # rows are (o, h, l, c)


START = (100, 100.4, 99.6, 100)


def plan(order_type="BUY STOP", entry=101.0, stop=98.0, tps=(104.0,), expires=4, level=100.5, sid="sig1", **kw):
    side = "LONG" if order_type.startswith("BUY") else "SHORT"
    p = dict(symbol="XAUUSD", direction=side, order_type=order_type, kind="stop" if "STOP" in order_type else "limit", entry=entry, stop=stop,
             tp1=tps[0] if len(tps) > 0 else None, tp2=tps[1] if len(tps) > 1 else None, tp3=tps[2] if len(tps) > 2 else None, lots=0.1, units=10.0,
             risk_currency=30.0, expires_bars=expires, signal_id="%s-%s" % (sid, order_type.replace(" ", "")), signal_ts=T0, strategy=order_type.lower().replace(" ", "_"),
             regime="strong_bull", invalidation="close below the stop", invalid_price=stop, meta=dict(level=level), atr=1.0, broker_errors=[], fits=True,
             stale=False, targets_plan=None)
    p.update(kw)
    return p


def make(rows, **kw):
    bars = bars_from(rows)
    sink = []
    o = pp.create(plan(**kw), bars[:1], STEP, now=T0 + 10, sink=sink)
    return o, bars, sink


def run(orders, bars, events=(), sink=None):
    return pp.update_all(orders, bars, FREE, STEP, CFG, events, sink if sink is not None else [])


def states(o):
    return [h["to_state"] for h in o["history"]]


class Create(unittest.TestCase):
    def test_a_clean_plan_waits_for_its_trigger(self):
        o, bars, sink = make([START])
        self.assertEqual((o["order_state"], o["position_state"]), ("AWAITING_TRIGGER", None))
        self.assertRegex(o["id"], r"^po-[0-9a-f]{10}$")
        self.assertEqual(o["targets"], [104.0])
        self.assertEqual(o["expires_at"], T0 + 4 * STEP)
        self.assertEqual(len(sink), 1)
        self.assertIn("simulation only", sink[0]["note"])

    def test_blocked_plans_are_kept_as_rejected_with_the_reasons(self):
        bars = bars_from([START])
        for kw, text in ((dict(broker_errors=["BUY STOP must be at least 0.30 above the Ask"]), "broker rule"), (dict(fits=False, size_problem="too small"), "does not fit"),
                         (dict(stale=True), "already expired")):
            o = pp.create(plan(**kw), bars, STEP, now=T0)
            self.assertEqual(o["order_state"], "REJECTED")
            self.assertIn(text, " ".join(o["reasons"]))
        o = pp.create(plan(), bars, STEP, now=T0, blocking=["aggregate risk limit"])
        self.assertEqual(o["order_state"], "REJECTED")
        self.assertIn("aggregate risk limit", o["reasons"])

    def test_duplicates_are_refused(self):
        bars = bars_from([START])
        first = pp.create(plan(), bars, STEP, now=T0)
        again = pp.create(plan(sid="other"), bars, STEP, now=T0, others=[first])
        self.assertEqual(again["order_state"], "REJECTED")
        self.assertIn("duplicate", " ".join(again["reasons"]))
        far = pp.create(plan(entry=110.0, tps=(120.0,), sid="far"), bars, STEP, now=T0, others=[first])
        self.assertEqual(far["order_state"], "AWAITING_TRIGGER")

    def test_only_the_four_ordinary_types_and_real_bars(self):
        with self.assertRaises(pp.PaperError):
            pp.create(plan("BUY STOP LIMIT"), bars_from([START]), STEP)
        with self.assertRaises(pp.PaperError):
            pp.create(plan(), [], STEP)

    def test_signal_states_and_order_states_are_kept_apart(self):
        self.assertEqual(set(pp.SIGNAL_STATES), {"CANDIDATE", "VALIDATED", "REJECTED"})
        self.assertIn("AWAITING_TRIGGER", pp.ORDER_STATES)
        self.assertNotIn("CANDIDATE", pp.ORDER_STATES)
        self.assertNotIn("VALIDATED", pp.ORDER_STATES)
        self.assertNotIn("BROKER", " ".join(pp.ORDER_STATES))


class Lifecycle(unittest.TestCase):
    def test_stop_order_triggers_fills_and_hits_its_target(self):
        o, bars, sink = make([START, (100, 101.5, 99.8, 101.2), (101.2, 104.5, 101.0, 104.2), (104, 104.5, 103.5, 104)])
        self.assertTrue(run([o], bars, sink=sink))
        self.assertEqual(states(o), ["AWAITING_TRIGGER", "TRIGGERED", "FILLED", "FILLED"])      # the last entry is the position closing
        self.assertEqual((o["order_state"], o["position_state"]), ("FILLED", "CLOSED"))
        self.assertAlmostEqual(o["fill"]["price"], 101.0)
        self.assertEqual(o["exit"]["how"], "target")
        self.assertGreater(o["r_result"], 0)
        self.assertEqual([e["to_state"] for e in sink], ["AWAITING_TRIGGER", "TRIGGERED", "FILLED", "FILLED"])      # the audit log matches the order's own history

    def test_limit_order_fills_only_when_price_trades_through(self):
        o, bars, sink = make([START, (100, 100.2, 98.6, 99.0), (99, 99.5, 98.4, 99.2)], order_type="BUY LIMIT", entry=98.5, stop=95.0, tps=(103.0,))
        run([o], bars)
        self.assertEqual(o["order_state"], "AWAITING_TRIGGER")                                  # 98.6 touched nothing; 98.4 is not through 98.5 by a tick yet
        bars2 = bars_from([START, (100, 100.2, 98.0, 99.0), (99, 104.0, 98.5, 103.5)])
        o2 = pp.create(plan("BUY LIMIT", 98.5, 95.0, (103.0,)), bars2[:1], STEP, now=T0)
        run([o2], bars2)
        self.assertEqual(o2["order_state"], "FILLED")
        self.assertEqual(o2["position_state"], "CLOSED")
        self.assertNotIn("TRIGGERED", states(o2))                                                # a limit fill is not a trigger

    def test_a_waiting_order_stays_waiting_until_it_has_had_its_full_time(self):
        o, bars, _ = make([START] + [(100, 100.4, 99.6, 100)] * 3)
        run([o], bars)
        self.assertEqual(o["order_state"], "AWAITING_TRIGGER")                                  # 3 bars of a 4-bar life

    def test_an_unfilled_order_expires_at_its_expiry_time(self):
        o, bars, sink = make([START] + [(100, 100.4, 99.6, 100)] * 5)
        run([o], bars, sink=sink)
        self.assertEqual(o["order_state"], "EXPIRED")
        self.assertEqual(o["history"][-1]["ts"], pp._iso(o["expires_at"]))
        self.assertIn("not filled within 4 bars", o["history"][-1]["note"])
        self.assertEqual(o["position_state"], None)

    def test_closing_beyond_the_structural_stop_invalidates_it(self):
        o, bars, _ = make([START, (100, 100.2, 95.0, 96.0), (96, 97, 95, 96)])
        run([o], bars)
        self.assertEqual(o["order_state"], "INVALIDATED")
        self.assertIn("structural stop", o["history"][-1]["note"])

    def test_a_filled_position_can_still_be_open(self):
        o, bars, _ = make([START, (100, 101.5, 99.8, 101.2), (101.2, 102.0, 100.5, 101.5)])
        run([o], bars)
        self.assertEqual((o["order_state"], o["position_state"], o["exit"]), ("FILLED", "OPEN", None))
        more = bars_from([START, (100, 101.5, 99.8, 101.2), (101.2, 102.0, 100.5, 101.5), (101.5, 105.0, 101.0, 104.5)])
        run([o], more)
        self.assertEqual(o["position_state"], "CLOSED")

    def test_a_losing_position_records_a_negative_result(self):
        o, bars, _ = make([START, (100, 101.5, 99.8, 101.2), (101.2, 101.5, 97.5, 98.5), (98.5, 99, 98, 98.5)])
        run([o], bars)
        self.assertEqual(o["exit"]["how"], "stop")
        self.assertLess(o["r_result"], 0)

    def test_running_again_changes_nothing_and_logs_nothing(self):
        o, bars, sink = make([START, (100, 101.5, 99.8, 101.2), (101.2, 104.5, 101.0, 104.2), (104, 104.5, 103.5, 104)])
        run([o], bars, sink=sink)
        n_hist, n_sink = len(o["history"]), len(sink)
        again = []
        self.assertFalse(run([o], bars, sink=again))
        self.assertEqual((len(o["history"]), len(again)), (n_hist, 0))
        self.assertEqual(n_sink, len(sink))

    def test_sweep_reversal_invalidates_a_waiting_breakout_order(self):
        ev = [dict(type="sweep", i=1, t=T0 + STEP, level=100.5, reversal="bear")]
        o, bars, _ = make([START, (100, 100.9, 99.5, 100.2), (100.2, 100.6, 99.7, 100.0)])
        run([o], bars, events=ev)
        self.assertEqual(o["order_state"], "INVALIDATED")
        self.assertIn("liquidity sweep", o["history"][-1]["note"])
        far = [dict(type="sweep", i=1, t=T0 + STEP, level=120.0, reversal="bear")]
        o2, bars2, _ = make([START, (100, 100.9, 99.5, 100.2), (100.2, 100.6, 99.7, 100.0)])
        run([o2], bars2, events=far)
        self.assertEqual(o2["order_state"], "AWAITING_TRIGGER")                                  # a sweep of some other level is not this setup's problem

    def test_orders_whose_start_bar_is_missing_are_left_alone(self):
        o, bars, _ = make([START, (100, 101.5, 99.8, 101.2)])
        o["start_t"] = 12345
        self.assertFalse(run([o], bars))
        self.assertEqual(o["order_state"], "AWAITING_TRIGGER")

    def test_partially_filled_is_never_produced_by_the_bar_simulator(self):
        seen = set()
        for rows in ([START, (100, 101.5, 99.8, 101.2), (101.2, 104.5, 101.0, 104.2)], [START] + [(100, 100.4, 99.6, 100)] * 5, [START, (100, 100.2, 95.0, 96.0)]):
            o, bars, _ = make(rows)
            run([o], bars)
            seen.update(states(o))
        self.assertNotIn("PARTIALLY_FILLED", seen)

    def test_short_orders_mirror(self):
        o, bars, _ = make([START, (100, 100.2, 98.5, 98.8), (98.8, 99.0, 95.5, 95.9), (96, 96.5, 95.5, 96)], order_type="SELL STOP", entry=99.0, stop=102.0, tps=(96.0,))
        run([o], bars)
        self.assertEqual((o["order_state"], o["position_state"]), ("FILLED", "CLOSED"))
        self.assertGreater(o["r_result"], 0)


class OCO(unittest.TestCase):
    def pair(self, rows):
        bars = bars_from(rows)
        sink = []
        buy = pp.create(plan("BUY STOP", 101.0, 98.0, (104.0,), expires=6, level=100.5, sid="a"), bars[:1], STEP, now=T0, oco_group="g1", sink=sink)
        sell = pp.create(plan("SELL STOP", 99.0, 102.0, (96.0,), expires=6, level=99.5, sid="b"), bars[:1], STEP, now=T0, oco_group="g1", sink=sink)
        return buy, sell, bars, sink

    def test_the_first_fill_cancels_the_other_before_it_could_fill(self):
        buy, sell, bars, sink = self.pair([START, (100, 101.5, 99.5, 101.2), (101.2, 101.4, 98.5, 99.0), (99, 99.5, 98, 98.5)])
        run([buy, sell], bars, sink=sink)
        self.assertEqual(buy["order_state"], "FILLED")
        self.assertEqual(sell["order_state"], "CANCELLED")
        self.assertIn("OCO", sell["history"][-1]["note"])
        self.assertIsNone(sell["fill"])
        self.assertNotIn("FILLED", [e["to_state"] for e in sink if e["order_id"] == sell["id"]])      # never logged as filled

    def test_a_same_bar_double_trigger_is_kept_and_flagged(self):
        buy, sell, bars, sink = self.pair([START, (100, 101.6, 98.4, 100.0), (100, 100.5, 99.5, 100)])
        run([buy, sell], bars, sink=sink)
        self.assertEqual((buy["order_state"], sell["order_state"]), ("FILLED", "FILLED"))
        self.assertIn("oco_failed", buy["flags"])
        self.assertIn("oco_failed", sell["flags"])
        self.assertTrue(any("OCO FAILED" in h["note"] for h in buy["history"]))

    def test_a_waiting_sibling_is_cancelled_when_the_other_fills(self):
        buy, sell, bars, sink = self.pair([START, (100, 101.5, 99.8, 101.2), (101.2, 101.6, 100.9, 101.4)])
        run([buy, sell], bars, sink=sink)
        self.assertEqual((buy["order_state"], sell["order_state"]), ("FILLED", "CANCELLED"))

    def test_unlinked_orders_do_not_cancel_each_other(self):
        bars = bars_from([START, (100, 101.5, 99.5, 101.2), (101.2, 101.4, 98.5, 99.0), (99, 99.5, 98, 98.5)])
        buy = pp.create(plan("BUY STOP", 101.0, 98.0, (104.0,), sid="a"), bars[:1], STEP, now=T0)
        sell = pp.create(plan("SELL STOP", 99.0, 102.0, (96.0,), sid="b"), bars[:1], STEP, now=T0)
        run([buy, sell], bars)
        self.assertEqual((buy["order_state"], sell["order_state"]), ("FILLED", "FILLED"))       # exactly the risk the dashboard warns about

    def test_the_sibling_that_expires_first_stays_expired(self):
        bars = bars_from([START, (100, 100.4, 99.6, 100), (100, 100.4, 99.6, 100), (100, 101.5, 99.5, 101.2)])
        buy = pp.create(plan("BUY STOP", 101.0, 98.0, (104.0,), expires=5, sid="a"), bars[:1], STEP, now=T0, oco_group="g", )
        sell = pp.create(plan("SELL STOP", 99.0, 102.0, (96.0,), expires=1, sid="b"), bars[:1], STEP, now=T0, oco_group="g")
        run([buy, sell], bars)
        self.assertEqual(buy["order_state"], "FILLED")
        self.assertEqual(sell["order_state"], "EXPIRED")


class Manage(unittest.TestCase):
    def test_cancel_a_waiting_order(self):
        o, bars, sink = make([START])
        pp.cancel([o], o["id"], "changed my mind", now=T0 + 99, sink=sink)
        self.assertEqual(o["order_state"], "CANCELLED")
        self.assertEqual(sink[-1]["note"], "changed my mind")

    def test_a_filled_or_unknown_order_cannot_be_cancelled(self):
        o, bars, _ = make([START, (100, 101.5, 99.8, 101.2), (101.2, 102, 100.5, 101.5)])
        run([o], bars)
        with self.assertRaises(pp.PaperError):
            pp.cancel([o], o["id"])
        with self.assertRaises(pp.PaperError):
            pp.cancel([o], "po-0000000000")
        with self.assertRaises(pp.PaperError):
            pp.cancel([o], "bad id")
        o2, _, _ = make([START])
        with self.assertRaises(pp.PaperError):
            pp.cancel([o2], o2["id"], "<script>")

    def test_summary(self):
        a, bars, _ = make([START, (100, 101.5, 99.8, 101.2), (101.2, 104.5, 101.0, 104.2), (104, 104.5, 103.5, 104)])
        run([a], bars)
        b, _, _ = make([START], sid="b")
        s = pp.summary([a, b])
        self.assertEqual((s["total"], s["waiting"], s["open"], s["closed"]), (2, 1, 0, 1))
        self.assertGreater(s["total_r"], 0)
        self.assertIn("nothing was sent anywhere", s["note"])
        self.assertEqual(len(pp.live_orders([a, b])), 1)

    def test_persistence_and_the_audit_log(self):
        d = tempfile.mkdtemp()
        try:
            op, ap = os.path.join(d, "o.json"), os.path.join(d, "a.json")
            o, bars, sink = make([START, (100, 101.5, 99.8, 101.2), (101.2, 104.5, 101.0, 104.2), (104, 104.5, 103.5, 104)])
            run([o], bars, sink=sink)
            pp.save([o], op)
            pp._audit(sink, ap)
            pp._audit([], ap)
            back = pp.load(op)
            self.assertEqual(back[0]["id"], o["id"])
            self.assertEqual(back[0]["history"], o["history"])
            log = pp.audit_log(ap)
            self.assertEqual(len(log), len(sink))
            for e in log:
                for k in ("ts", "recorded_at", "order_id", "signal_id", "from_state", "to_state", "note", "symbol", "order_type", "strategy"):
                    self.assertIn(k, e)
            self.assertEqual(pp.load(os.path.join(d, "none.json")), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_broker_code(self):
        with open(pp.__file__, encoding="utf-8") as f:
            src = f.read()
        for w in ("urllib", "socket", "subprocess", "MetaTrader5", "order_send", "import requests"):
            self.assertNotIn(w, src)


if __name__ == "__main__":
    unittest.main()
