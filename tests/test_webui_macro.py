"""Economic calendar, time zones, event studies and the news-filter test. Bars and trades are SYNTHETIC."""
import datetime as dt
import os
import random
import shutil
import tempfile
import unittest

from _load import load

mc = load("webui", "macro")


def utc(y, mo, d, h=0, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp())


FED_HTML = """
<h4><a id="x">2024 FOMC Meetings</a></h4>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">30-31</div>
<div class="fomc-meeting__minutes"><strong>Minutes:</strong> (Released February 21, 2024)</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>April/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1</div>
<div class="fomc-meeting__minutes"> (Released May 22, 2024)</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>December</strong></div>
<div class="fomc-meeting__date">17-18*</div></div>
<h4><a id="y">2023 FOMC Meetings</a></h4>
<div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>November</strong></div>
<div class="fomc-meeting__date">31-1</div></div>
"""


class TimeZones(unittest.TestCase):
    def test_us_daylight_saving_dates(self):
        self.assertFalse(mc.us_dst(dt.date(2024, 3, 9)))
        self.assertTrue(mc.us_dst(dt.date(2024, 3, 10)))                    # second Sunday of March
        self.assertTrue(mc.us_dst(dt.date(2024, 11, 2)))
        self.assertFalse(mc.us_dst(dt.date(2024, 11, 3)))                   # first Sunday of November
        self.assertFalse(mc.us_dst(dt.date(2024, 1, 15)))
        self.assertTrue(mc.us_dst(dt.date(2024, 7, 4)))

    def test_new_york_to_utc(self):
        self.assertEqual(mc.ny_to_utc(2024, 1, 31, 14), utc(2024, 1, 31, 19))      # EST = UTC-5
        self.assertEqual(mc.ny_to_utc(2024, 7, 31, 14), utc(2024, 7, 31, 18))      # EDT = UTC-4
        self.assertEqual(mc.ny_to_utc(2024, 3, 8, 8, 30), utc(2024, 3, 8, 13, 30))
        self.assertEqual(mc.ny_to_utc(2024, 3, 11, 8, 30), utc(2024, 3, 11, 12, 30))


class Fomc(unittest.TestCase):
    def test_parse_statements_and_minutes(self):
        ev = mc.parse_fomc_html(FED_HTML, observed_at=utc(2024, 1, 1))
        stm = {e["utc"]: e for e in ev if e["type"] == "FOMC"}
        self.assertIn("2024-01-31T19:00:00+00:00", stm)                     # 14:00 EST on the last day
        self.assertIn("2024-05-01T18:00:00+00:00", stm)                     # April/May meeting ends 1 May, 14:00 EDT
        self.assertIn("2024-12-18T19:00:00+00:00", stm)                     # the asterisk is ignored
        self.assertIn("2023-12-01T19:00:00+00:00", stm)                     # 31-1 crosses October... November into December
        minutes = [e for e in ev if e["type"] == "FOMC_MINUTES"]
        self.assertEqual({e["utc"] for e in minutes}, {"2024-02-21T19:00:00+00:00", "2024-05-22T18:00:00+00:00"})

    def test_every_event_records_its_source_and_when_it_was_seen(self):
        ev = mc.parse_fomc_html(FED_HTML, observed_at=utc(2024, 1, 1))
        for e in ev:
            self.assertEqual((e["source"], e["reliability"]), ("federalreserve.gov", "official"))
            self.assertEqual(e["observed_at"], "2024-01-01T00:00:00+00:00")
            self.assertEqual(e["importance"], "high" if e["type"] == "FOMC" else "medium")

    def test_unrecognised_page_gives_no_events(self):
        self.assertEqual(mc.parse_fomc_html("<html>nothing here</html>"), [])

    def test_fetch_refuses_a_page_it_cannot_read(self):
        class Op:
            def open(self, req, timeout=None):
                import io
                return io.BytesIO(b"<html>changed layout</html>")
        with self.assertRaises(SystemExit):
            mc.fetch_fomc(Op())


class Nfp(unittest.TestCase):
    def test_rule_matches_known_release_dates(self):
        got = {e["utc"][:10] for e in mc.nfp_dates(2024, 2024)}
        for known in ("2024-02-02", "2024-03-08", "2024-04-05", "2024-05-03", "2024-06-07", "2024-07-05", "2024-08-02"):
            self.assertIn(known, got)

    def test_always_a_friday_at_830_new_york(self):
        for e in mc.nfp_dates(2023, 2027):
            d = dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc)
            self.assertEqual(d.weekday(), 4)
            self.assertIn((d.hour, d.minute), ((13, 30), (12, 30)))

    def test_marked_derived_so_it_is_never_mistaken_for_official(self):
        e = mc.nfp_dates(2024, 2024)[0]
        self.assertEqual(e["reliability"], "derived")
        self.assertIn("verify", e["notes"])


class Csv(unittest.TestCase):
    TEXT = ("datetime_utc,name,type,importance,actual,forecast,previous,source\n"
            "2024-02-13 13:30,US CPI,CPI,high,3.1,2.9,3.4,bls.gov\n"
            "2024-03-12T12:30:00Z,US CPI,CPI,,3.2,3.1,3.1,\n"
            "1710246600,PCE,PCE,high,,,,\n"
            "not-a-date,Broken,CPI,high,,,,\n"
            ",NoDate,CPI,high,,,,\n")

    def test_good_rows_import_bad_rows_are_listed(self):
        ev, rej = mc.parse_csv(self.TEXT, observed_at=utc(2024, 1, 1))
        self.assertEqual(len(ev), 3)
        self.assertEqual([r[0] for r in rej], [5, 6])
        self.assertEqual(ev[0]["utc"], "2024-02-13T13:30:00+00:00")
        self.assertEqual((ev[0]["actual"], ev[0]["forecast"], ev[0]["previous"]), (3.1, 2.9, 3.4))
        self.assertEqual(ev[1]["utc"], "2024-03-12T12:30:00+00:00")
        self.assertEqual(ev[2]["ts"], 1710246600)
        self.assertTrue(all(e["reliability"] == "manual" for e in ev))
        self.assertEqual(ev[1]["importance"], "high")                       # defaults by type

    def test_unknown_types_become_other(self):
        ev, _ = mc.parse_csv("datetime_utc,name,type\n2024-02-13 13:30,Thing,WEIRD\n")
        self.assertEqual(ev[0]["type"], "OTHER")

    def test_make_event_validates(self):
        with self.assertRaises(ValueError):
            mc.make_event(0, "x", "NOPE", "s", "official")
        with self.assertRaises(ValueError):
            mc.make_event(0, "x", "CPI", "s", "guess")


class Store(unittest.TestCase):
    def ev(self, ts, rel, typ="CPI"):
        return mc.make_event(ts, "x", typ, "src", rel, observed_at=0)

    def test_better_source_wins_and_worse_never_replaces(self):
        a = self.ev(1000, "derived")
        b = self.ev(1000, "official")
        self.assertEqual(mc.merge([a], [b])[0]["reliability"], "official")
        self.assertEqual(mc.merge([b], [a])[0]["reliability"], "official")
        self.assertEqual(len(mc.merge([a], [self.ev(1000, "derived", "NFP")])), 2)       # different type, kept apart

    def test_merge_sorts_by_time(self):
        m = mc.merge([self.ev(3000, "manual")], [self.ev(1000, "manual"), self.ev(2000, "manual")])
        self.assertEqual([e["ts"] for e in m], [1000, 2000, 3000])

    def test_store_round_trip(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "e", "events.json")
            mc.save_store([self.ev(5, "manual")], p)
            self.assertEqual(mc.load_store(p)[0]["ts"], 5)
            self.assertEqual(mc.load_store(os.path.join(d, "none.json")), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_window_splits_upcoming_and_recent(self):
        now = 100000
        evs = [self.ev(now + 3600, "official", "FOMC"), self.ev(now - 3600, "official", "NFP"),
               self.ev(now + 10 * 86400, "official", "CPI"), self.ev(now + 60, "manual", "OTHER")]
        evs[3]["importance"] = "low"
        up, rc = mc.window(evs, now, hours_ahead=48, hours_back=24, high_only=True)
        self.assertEqual([e["type"] for e in up], ["FOMC"])
        self.assertEqual([e["type"] for e in rc], ["NFP"])
        up_all, _ = mc.window(evs, now, 48, 24, high_only=False)
        self.assertEqual(len(up_all), 2)

    def test_summary_lists_sources_and_has_a_disclaimer(self):
        s = mc.summary(now=100000, events=[self.ev(100500, "official", "FOMC")])
        self.assertEqual(len(s["upcoming"]), 1)
        self.assertIn("src (official)", s["sources"])
        self.assertIn("not proof", s["note"])
        self.assertNotIn("study", s)


def bars_with_spike(n=400, spike_at=200, seed=2, step=3600, t0=1_700_006_400, spike=6.0):
    rnd = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        c = o + rnd.gauss(0, 0.5) + (spike if i == spike_at else 0.0)
        h = max(o, c) + abs(rnd.gauss(0, 0.15))
        l = min(o, c) - abs(rnd.gauss(0, 0.15))
        out.append((t0 + i * step, h, l, c, o))
        p = c
    return out


class Study(unittest.TestCase):
    def test_a_planted_spike_shows_up_as_excess_reaction(self):
        bars = bars_with_spike()
        e = mc.make_event(bars[200][0], "Release", "NFP", "t", "manual", observed_at=0)
        res = mc.event_study(bars, [e])
        row = res["NFP"]
        self.assertEqual(row["n"], 1)
        self.assertGreater(row["by_k"][1]["excess"], 3)
        self.assertGreater(row["by_k"][1]["mean_abs_move_atr"], 3)
        self.assertFalse(row["enough"])                                       # one event is never enough

    def test_events_with_no_reaction_show_no_excess(self):
        bars = bars_with_spike(spike=0.0)
        evs = [mc.make_event(bars[i][0], "R", "CPI", "t", "manual", observed_at=0) for i in range(60, 360, 15)]
        res = mc.event_study(bars, evs)
        self.assertEqual(res["CPI"]["n"], len(evs))
        self.assertTrue(res["CPI"]["enough"])
        self.assertLess(res["CPI"]["by_k"][3]["excess"], 1.8)
        self.assertGreater(res["CPI"]["by_k"][3]["excess"], 0.4)

    def test_uses_the_atr_known_before_the_release(self):
        bars = bars_with_spike()
        e = mc.make_event(bars[200][0], "Release", "NFP", "t", "manual", observed_at=0)
        a = mc.event_study(bars, [e])["NFP"]["by_k"][1]["mean_abs_move_atr"]
        bars2 = list(bars)
        t, h, l, c, o = bars2[210]
        bars2[210] = (t, h + 50, l - 50, c, o)                              # chaos AFTER the event must not change the scale
        b = mc.event_study(bars2, [e])["NFP"]["by_k"][1]["mean_abs_move_atr"]
        self.assertAlmostEqual(a, b)

    def test_events_outside_the_data_are_skipped(self):
        bars = bars_with_spike()
        far = mc.make_event(bars[-1][0] + 86400 * 30, "R", "CPI", "t", "manual", observed_at=0)
        early = mc.make_event(bars[2][0], "R", "CPI", "t", "manual", observed_at=0)
        self.assertEqual(mc.event_study(bars, [far, early]), {})

    def test_surprise_sign_is_a_frequency_not_a_claim(self):
        bars = bars_with_spike()
        e = mc.make_event(bars[200][0], "R", "CPI", "t", "manual", actual=3.2, forecast=3.0, observed_at=0)
        row = mc.event_study(bars, [e])["CPI"]
        self.assertEqual(row["surprise_agree"]["n"], 1)
        self.assertIn("not a mechanism", row["surprise_agree"]["note"])

    def test_spread_ratio_when_a_spread_series_is_given(self):
        bars = bars_with_spike()
        spread = {b[0]: 0.3 for b in bars}
        for k in range(200, 203):
            spread[bars[k][0]] = 0.9
        e = mc.make_event(bars[200][0], "R", "NFP", "t", "manual", observed_at=0)
        self.assertAlmostEqual(mc.event_study(bars, [e], spread=spread)["NFP"]["spread_ratio"], 3.0)

    def test_too_little_data(self):
        self.assertEqual(mc.event_study(bars_with_spike(30, 10), []), {})


def trade(t, r):
    return dict(entry_t=t, r=r)


class NewsFilter(unittest.TestCase):
    EV = [mc.make_event(1_000_000 + k * 100_000, "R", "NFP", "t", "manual", observed_at=0) for k in range(40)]

    def trades(self, inside_mean, outside_mean, n_in=60, n_out=200, seed=1):
        rnd = random.Random(seed)
        out = [trade(self.EV[k % 40]["ts"] + rnd.randint(-1000, 3000), rnd.gauss(inside_mean, 1)) for k in range(n_in)]
        out += [trade(1_000_000 + 50_000 + (k % 39) * 100_000 + rnd.randint(0, 20000), rnd.gauss(outside_mean, 1)) for k in range(n_out)]
        return out

    def test_a_clear_penalty_supports_a_filter(self):
        res = mc.evaluate_news_filter(self.trades(-0.6, 0.1), self.EV)
        self.assertTrue(res["recommended"])
        self.assertLess(res["p_value"], 0.05)
        self.assertIn("past data only", res["verdict"])

    def test_no_difference_means_no_filter(self):
        res = mc.evaluate_news_filter(self.trades(0.05, 0.05), self.EV)
        self.assertFalse(res["recommended"])
        self.assertIn("NO EVIDENCE", res["verdict"])

    def test_better_inside_never_recommends_a_filter(self):
        self.assertFalse(mc.evaluate_news_filter(self.trades(0.5, -0.2), self.EV)["recommended"])

    def test_few_inside_trades_is_no_evidence_not_a_guess(self):
        res = mc.evaluate_news_filter(self.trades(-2.0, 0.2, n_in=10), self.EV)
        self.assertFalse(res["recommended"])
        self.assertIsNone(res["p_value"])
        self.assertIn("need 30", res["verdict"])

    def test_only_high_impact_events_count_by_default(self):
        low = [dict(e, importance="low") for e in self.EV]
        res = mc.evaluate_news_filter(self.trades(-0.6, 0.1), low)
        self.assertEqual(res["inside"]["n"], 0)
        self.assertFalse(res["recommended"])

    def test_window_edges(self):
        ev = [mc.make_event(10_000, "R", "NFP", "t", "manual", observed_at=0)]
        res = mc.evaluate_news_filter([trade(10_000 - 1800, 1.0), trade(10_000 - 1801, 1.0), trade(10_000 + 3600, 1.0), trade(10_000 + 3601, 1.0)],
                                      ev, minutes_before=30, minutes_after=60, min_inside=1)
        self.assertEqual(res["inside"]["n"], 2)
        self.assertEqual(res["outside"]["n"], 2)


class Wording(unittest.TestCase):
    def test_module_never_claims_causation(self):
        with open(os.path.join(os.path.dirname(mc.__file__), "macro.py"), encoding="utf-8") as f:
            src = f.read().lower()
        self.assertIn("not causal", src)
        self.assertNotIn("because of the release", src)


if __name__ == "__main__":
    unittest.main()
