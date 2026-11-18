"""Gold session engine: daylight saving handled per country, session labels, closures, news windows, statistics. SYNTHETIC bars."""
import datetime as dt
import random
import unittest

from _load import load

gs = load("webui", "gold_sessions")
UTC = dt.timezone.utc


def ts(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def window(day, name):
    return gs.windows_for_day(day)[name]


def hm(t):
    return dt.datetime.fromtimestamp(t, UTC).strftime("%H:%M")


class DaylightSaving(unittest.TestCase):
    def test_london_session_starts_an_hour_earlier_in_utc_in_summer(self):
        self.assertEqual(hm(window(dt.date(2026, 1, 15), "london")[0]), "08:00")
        self.assertEqual(hm(window(dt.date(2026, 7, 15), "london")[0]), "07:00")

    def test_new_york_session_shifts_with_us_dst(self):
        self.assertEqual(hm(window(dt.date(2026, 1, 15), "new_york")[0]), "13:00")
        self.assertEqual(hm(window(dt.date(2026, 7, 15), "new_york")[0]), "12:00")

    def test_tokyo_never_shifts(self):
        self.assertEqual(hm(window(dt.date(2026, 1, 15), "asia")[0]), hm(window(dt.date(2026, 7, 15), "asia")[0]))
        self.assertEqual(hm(window(dt.date(2026, 1, 15), "asia")[0]), "00:00")

    def test_the_two_countries_change_on_different_dates(self):
        # US clocks go forward 8 March 2026, UK on 29 March 2026: in between London is still GMT while New York is on summer time.
        d = dt.date(2026, 3, 16)
        self.assertEqual(hm(window(d, "london")[0]), "08:00")
        self.assertEqual(hm(window(d, "new_york")[0]), "12:00")
        # autumn: UK goes back 25 October, US 1 November
        d = dt.date(2026, 10, 28)
        self.assertEqual(hm(window(d, "london")[0]), "08:00")
        self.assertEqual(hm(window(d, "new_york")[0]), "12:00")

    def test_overlap_length_changes_with_the_gap_weeks(self):
        w = window(dt.date(2026, 1, 15), "overlap")
        self.assertEqual((hm(w[0]), hm(w[1])), ("13:00", "16:30"))
        w = window(dt.date(2026, 3, 16), "overlap")
        self.assertEqual((hm(w[0]), hm(w[1])), ("12:00", "16:30"))

    def test_offsets_are_read_from_the_zone_database_not_hardcoded(self):
        o = gs.offsets_on(ts(2026, 7, 15, 12))
        self.assertEqual((o["Europe/London"], o["America/New_York"], o["Asia/Tokyo"], o["Asia/Karachi"]), (1.0, -4.0, 9.0, 5.0))
        o = gs.offsets_on(ts(2026, 1, 15, 12))
        self.assertEqual((o["Europe/London"], o["America/New_York"]), (0.0, -5.0))

    def test_references_follow_local_time(self):
        s = gs.day_schedule(dt.date(2026, 7, 15))
        am = next(r for r in s["references"] if "AM auction" in r["name"])
        self.assertEqual(am["utc"], "2026-07-15T09:30Z")        # 10:30 London summer time
        w = gs.day_schedule(dt.date(2026, 1, 15))
        am = next(r for r in w["references"] if "AM auction" in r["name"])
        self.assertEqual(am["utc"], "2026-01-15T10:30Z")


class Labels(unittest.TestCase):
    def test_sessions_in_winter(self):
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 3)), "asia")
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 9)), "london")
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 14)), "overlap")
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 18)), "new_york")
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 16, 29)), "overlap")
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 16, 30)), "new_york")       # London window ends at 16:30 UTC in winter

    def test_summer_shifts_the_same_clock_time_into_another_session(self):
        # 12:15 UTC is London session only in winter; in summer New York has opened too, so it is the overlap.
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 12, 15)), "london")
        self.assertEqual(gs.session_of(ts(2026, 7, 15, 12, 15)), "overlap")

    def test_closures(self):
        self.assertEqual(gs.session_of(ts(2026, 1, 17, 12)), "closed")            # Saturday
        self.assertEqual(gs.session_of(ts(2026, 1, 16, 23)), "closed")            # Friday after 17:00 New York (22:00 UTC winter)
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 22, 30)), "closed")        # daily 17:00 to 18:00 New York break
        self.assertNotEqual(gs.session_of(ts(2026, 1, 15, 23, 30)), "closed")     # reopened at 18:00 New York
        self.assertEqual(gs.session_of(ts(2026, 1, 18, 22, 30)), "closed")        # Sunday before the 18:00 New York reopening
        self.assertNotEqual(gs.session_of(ts(2026, 1, 18, 23, 30)), "closed")

    def test_off_window_hours_are_transition(self):
        self.assertEqual(gs.session_of(ts(2026, 1, 15, 23, 30)), "transition")

    def test_high_impact_events_make_post_news_and_a_block_window(self):
        ev = [dict(ts=ts(2026, 1, 15, 13, 30), importance="high"), dict(ts=ts(2026, 1, 15, 5, 0), importance="low")]
        c = gs.classify(ts(2026, 1, 15, 13, 45), ev)
        self.assertEqual((c["session"], c["news_block"]), ("post_news", True))
        c = gs.classify(ts(2026, 1, 15, 13, 5), ev)               # 25 minutes before: blocked, but still labelled by session
        self.assertTrue(c["news_block"])
        self.assertNotEqual(c["session"], "post_news")
        c = gs.classify(ts(2026, 1, 15, 16, 0), ev)               # 150 minutes after: back to normal
        self.assertEqual((c["session"], c["news_block"]), ("overlap", False))
        c = gs.classify(ts(2026, 1, 15, 5, 5), ev)                # low-importance events never block
        self.assertFalse(c["news_block"])


class Statistics(unittest.TestCase):
    def bars(self, days=60, seed=2):
        rnd = random.Random(seed)
        out, px = [], 2000.0
        t0 = ts(2026, 1, 5, 0)
        for i in range(days * 24):
            t = t0 + i * 3600
            op = px
            px += rnd.uniform(-2, 2)
            out.append((t, max(op, px) + rnd.uniform(0, 1), min(op, px) - rnd.uniform(0, 1), px, op))
        return out

    def test_stats_report_instances_and_never_invent_spread_or_volume(self):
        s = gs.stats(self.bars())
        self.assertIn("london", s)
        for v in s.values():
            self.assertIsNone(v["spread"])
            self.assertIsNone(v["volume"])
        self.assertGreater(s["london"]["instances"], 30)

    def test_small_samples_say_insufficient_evidence(self):
        s = gs.stats(self.bars(days=12))
        self.assertTrue(all(v["character"] == "INSUFFICIENT EVIDENCE" for v in s.values()))

    def test_random_walk_has_no_strong_character_claim(self):
        s = gs.stats(self.bars(days=120))
        for name in ("london", "asia", "new_york"):
            self.assertLess(abs(s[name]["avg_autocorr"]), 0.15)             # no planted structure, so no strong autocorrelation
            self.assertNotIn("momentum-leaning", s[name]["character"])

    def test_tracker_uses_only_closed_bars_and_reports_ranges(self):
        bars = self.bars(days=10)
        now = ts(2026, 1, 12, 10)
        t = gs.tracker(bars, now)
        self.assertEqual(t["classification"]["session"], "london")
        self.assertLessEqual(t["current"]["high"], max(b[1] for b in bars if b[0] <= now))
        self.assertIsNone(t["spread"])
        self.assertTrue(0 < t["minutes_to_session_end"] <= 8.5 * 60)
        self.assertIn("asia", t["previous_ranges"])


if __name__ == "__main__":
    unittest.main()
