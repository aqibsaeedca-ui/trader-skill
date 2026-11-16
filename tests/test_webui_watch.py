"""Watcher: intelligence alerts, dedupe, cooldowns, rate limits, restart safety and Bitcoin isolation. All inputs SYNTHETIC."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from _load import load

wx = load("webui", "watch_extras")
gw = load("webui", "gold_watch")
assets = load("webui", "assets")

NOW = 1_700_000_000


def intel(price=100.0, regime="sideways", bar="2026-01-01T10:00:00+00:00", decision=None, candidate=None, alerts=(), lockout=False,
          upcoming=(), stale=False, market_open=True, age=300, spread_series=None, interval="60m"):
    a = dict(title="Range trading", strategy="range", decision=decision or "NO TRADE", candidate=candidate,
             evidence=dict(status="VALIDATED (research, past data only)" if decision else "NOT VALIDATED"), blocking=[], info=[])
    d = dict(interval=interval,
             overview=dict(price=price, last_closed_bar=bar, regime=regime, trend="sideways", volatility="normal", events=dict(upcoming=list(upcoming), recent=[]),
                           data_quality=dict(series=[dict(interval=interval, stale=stale, market_open=market_open, age_sec=age, status="ok")])),
             assessments=[a], risk_monitor=dict(alerts=list(alerts), lockout=lockout))
    if spread_series is not None:
        d["spread_series"] = spread_series
    return d


def cand(i=1, side="long", entry=100.0, stop=95.0):
    return dict(id="range-60m-%d-%s-abc123" % (1_700_000_000 + i, side), side=side, entry=entry, stop=stop, targets=[110.0, 120.0], rr_net=1.8)


SET = dict(event_lead_min=30, max_alerts_per_hour=12, extras_cooldown_min=60)


def run(state, **kw):
    now = kw.pop("now", NOW)
    return wx.evaluate(intel(**kw), SET, state, now)


class Setups(unittest.TestCase):
    def test_a_validated_confirmed_setup_alerts_once(self):
        s = wx.new_state()
        out = run(s, decision="LONG", candidate=cand())
        self.assertEqual([a["kind"] for a in out], ["setup_confirmed"])
        self.assertIn("nothing is placed", out[0]["body"].lower())
        self.assertEqual(out[0]["signal_id"], cand()["id"])
        self.assertEqual(run(s, decision="LONG", candidate=cand(), now=NOW + 60), [])
        self.assertEqual(run(s, decision="LONG", candidate=cand(), now=NOW + 7200), [])      # once per signal, ever

    def test_an_unvalidated_candidate_never_alerts(self):
        self.assertEqual(run(wx.new_state(), decision=None, candidate=cand()), [])

    def test_a_new_signal_alerts_again(self):
        s = wx.new_state()
        run(s, decision="LONG", candidate=cand(1))
        self.assertEqual(len(run(s, decision="LONG", candidate=cand(2), now=NOW + 3600)), 1)

    def test_invalidation_alerts_once_and_stops_tracking(self):
        s = wx.new_state()
        run(s, decision="LONG", candidate=cand(stop=95))
        self.assertEqual(run(s, price=96.0, now=NOW + 60), [])
        out = run(s, price=94.5, now=NOW + 120)
        self.assertEqual([a["kind"] for a in out], ["setup_invalid"])
        self.assertEqual(run(s, price=90.0, now=NOW + 180), [])
        self.assertEqual(s["live"], {})

    def test_short_invalidation(self):
        s = wx.new_state()
        run(s, decision="SHORT", candidate=cand(side="short", entry=100, stop=105))
        self.assertEqual(run(s, price=104.0, now=NOW + 60), [])
        self.assertEqual([a["kind"] for a in run(s, price=105.5, now=NOW + 120)], ["setup_invalid"])

    def test_old_setups_expire_quietly(self):
        s = wx.new_state()
        run(s, decision="LONG", candidate=cand(stop=95))
        self.assertEqual(run(s, price=96, now=NOW + 49 * 3600), [])
        self.assertEqual(s["live"], {})

    def test_state_survives_a_json_round_trip(self):
        s = wx.new_state()
        run(s, decision="LONG", candidate=cand())
        s2 = json.loads(json.dumps(s))
        self.assertEqual(run(s2, decision="LONG", candidate=cand(), now=NOW + 600), [])
        self.assertEqual([a["kind"] for a in run(s2, price=90.0, now=NOW + 700)], ["setup_invalid"])


class Regime(unittest.TestCase):
    def test_meaningful_change_on_a_new_bar(self):
        s = wx.new_state()
        self.assertEqual(run(s, regime="sideways", bar="b1"), [])
        self.assertEqual(run(s, regime="sideways", bar="b2", now=NOW + 3600), [])
        out = run(s, regime="strong_bull", bar="b3", now=NOW + 7200)
        self.assertEqual([a["kind"] for a in out], ["regime_change"])
        self.assertIn("sideways to strong bull", out[0]["title"])

    def test_the_same_bar_never_alerts_twice(self):
        s = wx.new_state()
        run(s, regime="sideways", bar="b1")
        run(s, regime="strong_bull", bar="b2", now=NOW + 3600)
        self.assertEqual(run(s, regime="strong_bear", bar="b2", now=NOW + 3700), [])

    def test_minor_changes_are_ignored(self):
        s = wx.new_state()
        run(s, regime="sideways", bar="b1")
        self.assertEqual(run(s, regime="weak_trend", bar="b2", now=NOW + 3600), [])
        self.assertEqual(run(s, regime="vol_compression", bar="b3", now=NOW + 7200), [])

    def test_leaving_a_strong_trend_matters(self):
        s = wx.new_state()
        run(s, regime="strong_bull", bar="b1")
        self.assertEqual([a["kind"] for a in run(s, regime="sideways", bar="b2", now=NOW + 3600)], ["regime_change"])


class Events(unittest.TestCase):
    def ev(self, minutes):
        return dict(ts=NOW + minutes * 60, utc="2026-01-01T13:30:00+00:00", name="Nonfarm payrolls", source="BLS rule (derived)", reliability="derived")

    def test_close_event_alerts_once(self):
        s = wx.new_state()
        out = run(s, upcoming=[self.ev(20)])
        self.assertEqual([a["kind"] for a in out], ["high_impact_event"])
        self.assertIn("derived", out[0]["body"])
        self.assertEqual(run(s, upcoming=[self.ev(20)], now=NOW + 60), [])

    def test_far_events_are_quiet(self):
        self.assertEqual(run(wx.new_state(), upcoming=[self.ev(300)]), [])

    def test_lead_time_is_a_setting(self):
        s = wx.new_state()
        out = wx.evaluate(intel(upcoming=[self.ev(100)]), dict(SET, event_lead_min=120), s, NOW)
        self.assertEqual(len(out), 1)


class Data(unittest.TestCase):
    def test_stale_data_only_when_the_market_should_be_open(self):
        self.assertEqual([a["kind"] for a in run(wx.new_state(), stale=True, market_open=True, age=7200)], ["stale_data"])
        self.assertEqual(run(wx.new_state(), stale=True, market_open=False, age=7200), [])
        self.assertEqual(run(wx.new_state(), stale=False), [])

    def test_cooldown_then_repeat(self):
        s = wx.new_state()
        self.assertEqual(len(run(s, stale=True, age=7200)), 1)
        self.assertEqual(run(s, stale=True, age=7200, now=NOW + 1800), [])
        self.assertEqual(len(run(s, stale=True, age=7200, now=NOW + 3700)), 1)

    def test_abnormal_spread_needs_real_spread_data(self):
        calm = [0.3] * 40
        self.assertEqual(run(wx.new_state(), spread_series=calm + [0.4]), [])
        self.assertEqual([a["kind"] for a in run(wx.new_state(), spread_series=calm + [1.5])], ["abnormal_spread"])
        self.assertEqual(run(wx.new_state()), [])                                   # Yahoo has none: no alert, no guess
        self.assertEqual(run(wx.new_state(), spread_series=[0.3] * 5 + [9.0]), [])   # too little history


class Portfolio(unittest.TestCase):
    def kinds(self, alerts, lockout=False, state=None, now=NOW):
        return [a["kind"] for a in run(state or wx.new_state(), alerts=alerts, lockout=lockout, now=now)]

    def test_each_portfolio_alert_maps_to_its_kind(self):
        self.assertEqual(self.kinds(["1 position(s) have no stop"]), ["unprotected"])
        self.assertEqual(self.kinds(["pending order o1 has no stop"]), ["unprotected"])
        self.assertEqual(self.kinds(["pending orders on both sides: a buy and a sell are waiting at the same time"]), ["conflicting_orders"])
        self.assertEqual(self.kinds(["a pending short order would hedge an open position"]), ["conflicting_orders"])
        self.assertEqual(self.kinds(["net exposure 30.00 units is above your limit of 20.00"]), ["exposure"])
        self.assertEqual(self.kinds(["open risk 300.00 exceeds the 200.00 budget"]), ["exposure"])
        self.assertEqual(self.kinds(["daily loss limit reached (350.00 of 300.00)"], lockout=True), ["daily_loss"])

    def test_daily_loss_fires_once_per_day(self):
        s = wx.new_state()
        # NOW is 22:13 UTC, so half an hour later is still the same UTC day and a day later is the next one
        self.assertEqual(len(run(s, lockout=True)), 1)
        self.assertEqual(run(s, lockout=True, now=NOW + 1800), [])
        self.assertEqual(len(run(s, lockout=True, now=NOW + 86400 + 10)), 1)

    def test_repeat_alerts_wait_for_the_cooldown(self):
        s = wx.new_state()
        self.assertEqual(self.kinds(["1 position(s) have no stop"], state=s), ["unprotected"])
        self.assertEqual(self.kinds(["1 position(s) have no stop"], state=s, now=NOW + 600), [])
        self.assertEqual(self.kinds(["1 position(s) have no stop"], state=s, now=NOW + 3700), ["unprotected"])

    def test_alerts_say_nothing_is_changed_automatically(self):
        out = run(wx.new_state(), alerts=["1 position(s) have no stop"])
        self.assertIn("nothing is changed automatically", out[0]["body"].lower())


class Limits(unittest.TestCase):
    def test_hourly_cap_holds_extra_alerts_back_and_counts_them(self):
        s = wx.new_state()
        out = wx.evaluate(intel(alerts=["a no stop", "b no stop", "c no stop"]), dict(SET, max_alerts_per_hour=2), s, NOW)
        self.assertEqual(len(out), 2)
        self.assertEqual(s["suppressed"], 1)
        later = wx.evaluate(intel(alerts=["a no stop", "b no stop", "c no stop"]), dict(SET, max_alerts_per_hour=2), s, NOW + 3700)
        self.assertEqual(len(later), 2)                                              # the cap resets as the hour passes

    def test_old_causes_are_forgotten_after_a_week(self):
        s = wx.new_state()
        run(s, alerts=["1 position(s) have no stop"])
        self.assertTrue(s["fired"])
        run(s, now=NOW + 8 * 86400)
        self.assertFalse(s["fired"])

    def test_alert_shape(self):
        out = run(wx.new_state(), alerts=["1 position(s) have no stop"])[0]
        for k in ("ts", "tf", "kind", "title", "body", "price"):
            self.assertIn(k, out)
        self.assertEqual(out["tf"], "intel")

    def test_evaluate_does_not_modify_the_snapshot(self):
        d = intel(alerts=["1 position(s) have no stop"], decision="LONG", candidate=cand())
        before = json.dumps(d, sort_keys=True)
        wx.evaluate(d, SET, wx.new_state(), NOW)
        self.assertEqual(json.dumps(d, sort_keys=True), before)

    def test_module_has_no_network_or_order_code(self):
        with open(wx.__file__, encoding="utf-8") as f:
            src = f.read()
        for word in ("urllib", "socket", "subprocess", "place_order", "modify_order", "cancel_order"):
            self.assertNotIn(word, src)


class WatcherSettings(unittest.TestCase):
    def test_defaults_keep_bitcoin_as_before(self):
        self.assertTrue(gw.defaults(assets.GOLD)["intel"])
        self.assertFalse(gw.defaults(assets.BTC)["intel"])

    def test_new_settings_are_validated(self):
        s = gw.clean_settings(dict(intel=False, event_lead_min=10, max_alerts_per_hour=5, extras_cooldown_min=30), assets.GOLD)
        self.assertEqual((s["intel"], s["event_lead_min"], s["max_alerts_per_hour"], s["extras_cooldown_min"]), (False, 10, 5, 30))
        bad = gw.clean_settings(dict(intel="yes", event_lead_min=9999, max_alerts_per_hour=0, extras_cooldown_min="x"), assets.GOLD)
        self.assertEqual((bad["intel"], bad["event_lead_min"], bad["max_alerts_per_hour"], bad["extras_cooldown_min"]), (True, 30, 12, 60))

    def test_level_alert_settings_are_unchanged(self):
        s = gw.clean_settings({"near_atr": 99, "poll_min": 7, "tfs": ["1d"], "toast": "yes"}, assets.BTC)
        self.assertEqual((s["near_atr"], s["poll_min"], s["tfs"], s["toast"]), (0.5, 5, ["15m", "1h", "4h"], True))


class WatcherRuntime(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (gw.TRADER, gw.SETTINGS_FILE, gw.LOG_FILE)
        gw.TRADER = self.dir
        gw.SETTINGS_FILE, gw.LOG_FILE = os.path.join(self.dir, "gold_watch.json"), os.path.join(self.dir, "gold_alerts.json")

    def tearDown(self):
        gw.TRADER, gw.SETTINGS_FILE, gw.LOG_FILE = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_each_asset_has_its_own_state_file(self):
        g, b = gw.Watcher(assets.GOLD), gw.Watcher(assets.BTC)
        self.assertNotEqual(g.state_file, b.state_file)
        self.assertTrue(b.state_file.endswith("btc_watch_state.json"))

    def test_state_survives_a_restart(self):
        w = gw.Watcher(assets.BTC)
        w.xstate["fired"]["setup:abc"] = NOW
        w.xstate["live"]["abc"] = dict(side="long", stop=1.0, strategy="x", at=NOW)
        w._save(w.state_file, w.xstate)
        w2 = gw.Watcher(assets.BTC)
        self.assertEqual(w2.xstate["fired"], {"setup:abc": NOW})
        self.assertEqual(w2.status()["live_setups"], 1)

    def test_a_corrupt_state_file_falls_back_cleanly(self):
        w = gw.Watcher(assets.BTC)
        with open(w.state_file, "w") as f:
            f.write("{not json")
        self.assertEqual(gw.Watcher(assets.BTC).xstate, wx.new_state())
        with open(w.state_file, "w") as f:
            json.dump({"fired": "oops", "live": 3}, f)
        self.assertEqual(gw.Watcher(assets.BTC).xstate, wx.new_state())

    def test_changing_the_market_resets_only_intelligence_state(self):
        w = gw.Watcher(assets.GOLD)
        w.xstate["fired"]["k"] = NOW
        w.configure({"symbol": "GC=F"})
        self.assertIn("k", w.xstate["fired"])
        w.configure({"symbol": "MGC=F"})
        self.assertEqual(w.xstate["fired"], {})

    def test_status_reports_suppressed_alerts(self):
        w = gw.Watcher(assets.BTC)
        w.xstate["suppressed"] = 4
        self.assertEqual(w.status()["suppressed"], 4)

    def test_a_failure_in_intelligence_never_silences_level_alerts(self):
        import intelligence
        w = gw.Watcher(assets.BTC)
        w.configure({"symbol": "BTC-USD", "intel": True, "toast": False})
        keep = (gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build)
        level_alert = dict(ts="t", tf="15m", title="level", body="near a level", price=1.0, kind="level")
        try:
            gw.gi.analyse = lambda sym: dict(bars={"15m": [], "1h": [], "1d": []})
            gw.evaluate = lambda res, settings, state, now: [dict(level_alert)]
            gw.snapshot = lambda res, tfs: None

            def boom(*a, **k):
                raise SystemExit("data outage")
            intelligence.build = boom
            out = w.check_once()
            self.assertEqual([a["kind"] for a in out], ["level"])
            self.assertIn("intelligence alerts unavailable", w.status()["error"])
            self.assertIn("data outage", w.status()["error"])
        finally:
            gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build = keep

    def test_intelligence_off_means_it_is_never_called(self):
        import intelligence
        w = gw.Watcher(assets.BTC)
        w.configure({"symbol": "BTC-USD", "intel": False, "toast": False})
        keep = (gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build)
        called = []
        try:
            gw.gi.analyse = lambda sym: dict(bars={})
            gw.evaluate = lambda res, settings, state, now: []
            gw.snapshot = lambda res, tfs: None
            intelligence.build = lambda *a, **k: called.append(1)
            w.check_once()
            self.assertEqual(called, [])
            self.assertEqual(w.status()["error"], "")
        finally:
            gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build = keep

    def test_new_alerts_are_saved_with_ids(self):
        import intelligence
        w = gw.Watcher(assets.BTC)
        w.configure({"symbol": "BTC-USD", "intel": True, "toast": False})
        keep = (gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build, wx.evaluate)
        try:
            gw.gi.analyse = lambda sym: dict(bars={"15m": [], "1h": [], "1d": []})
            gw.evaluate = lambda res, settings, state, now: []
            gw.snapshot = lambda res, tfs: None
            intelligence.build = lambda *a, **k: {}
            gw.watch_extras.evaluate = lambda *a, **k: [dict(ts="t", tf="intel", kind="stale_data", title="x", body="y", price=1.0)]
            out = w.check_once()
            self.assertEqual(out[0]["id"], 1)
            with open(w.log_file) as f:
                self.assertEqual(json.load(f)[0]["kind"], "stale_data")
        finally:
            gw.gi.analyse, gw.evaluate, gw.snapshot, intelligence.build = keep[:4]
            gw.watch_extras.evaluate = wx_evaluate_original


wx_evaluate_original = gw.watch_extras.evaluate


if __name__ == "__main__":
    unittest.main()
