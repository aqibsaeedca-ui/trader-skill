"""Market data validation, provenance, store and CSV import. All bars here are SYNTHETIC test data."""
import datetime as dt
import os
import shutil
import tempfile
import unittest

from _load import load

md = load("webui", "marketdata")


def ts(y, mo, d, h=0, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp())


def series(start, n, step, price=2000.0):
    return [(start + i * step, price + 1, price - 1, price + 0.5, price) for i in range(n)]


MON = ts(2024, 1, 8, 10)          # a Monday, market open


class Validate(unittest.TestCase):
    def test_clean_series_is_ok(self):
        bars = series(MON, 8, 900)
        r = md.validate_bars(bars, "15m", "cme_gold", now=bars[-1][0] + 900 + 60)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["unexpected_missing"], 0)
        self.assertEqual(r["completeness_pct"], 100.0)
        self.assertFalse(r["forming"])

    def test_missing_bars_midweek_are_unexpected(self):
        bars = series(MON, 4, 900) + series(MON + 8 * 900, 4, 900)     # 4 bars missing
        r = md.validate_bars(bars, "15m", "cme_gold", now=bars[-1][0] + 960)
        self.assertEqual(r["unexpected_missing"], 4)
        self.assertEqual(r["status"], "warn")
        self.assertLess(r["completeness_pct"], 100)

    def test_weekend_gap_is_expected_for_gold(self):
        fri = ts(2024, 1, 12, 20, 0)
        sun = ts(2024, 1, 14, 23, 0)
        bars = series(fri, 4, 900) + series(sun, 4, 900)
        r = md.validate_bars(bars, "15m", "cme_gold", now=bars[-1][0] + 960)
        self.assertEqual(r["unexpected_missing"], 0)
        self.assertGreater(r["expected_missing"], 100)

    def test_weekend_gap_is_unexpected_for_24x7(self):
        fri = ts(2024, 1, 12, 20, 0)
        mon = ts(2024, 1, 15, 0, 0)
        bars = series(fri, 4, 3600) + series(mon, 4, 3600)
        r = md.validate_bars(bars, "60m", "24x7", now=bars[-1][0] + 3700)
        self.assertGreater(r["unexpected_missing"], 40)

    def test_duplicates_are_reported_and_dedupe_keeps_last(self):
        a = (MON, 11, 9, 10, 10)
        b = (MON, 12, 9, 11, 10)
        bars = [a, b, (MON + 900, 11, 9, 10, 10)]
        r = md.validate_bars(bars, "15m", "cme_gold", now=MON + 2000)
        self.assertEqual(len(r["duplicates"]), 1)
        clean, removed = md.dedupe_bars(bars)
        self.assertEqual(removed, [MON])
        self.assertEqual(clean[0], b)
        self.assertEqual(len(clean), 2)

    def test_invalid_ohlc_and_order_fail(self):
        bad = [(MON, 10, 12, 11, 11)]                                   # high below low
        self.assertEqual(md.validate_bars(bad, "15m", now=MON + 1000)["status"], "fail")
        shuffled = [(MON + 900, 11, 9, 10, 10), (MON, 11, 9, 10, 10)]
        r = md.validate_bars(shuffled, "15m", now=MON + 3000)
        self.assertEqual(r["out_of_order"], 1)
        self.assertEqual(r["status"], "fail")

    def test_close_outside_range_is_invalid(self):
        r = md.validate_bars([(MON, 11, 9, 15, 10)], "15m", now=MON + 1000)
        self.assertEqual(r["bad_ohlc"], 1)

    def test_stale_only_when_the_market_should_be_open(self):
        bars = series(MON, 4, 900)
        late = bars[-1][0] + 3 * 3600
        self.assertTrue(md.validate_bars(bars, "15m", "cme_gold", now=late)["stale"])
        sat = ts(2024, 1, 13, 12)
        weekend_bars = series(ts(2024, 1, 12, 19, 0), 4, 900)
        self.assertFalse(md.validate_bars(weekend_bars, "15m", "cme_gold", now=sat)["stale"])

    def test_forming_bar_detected_and_dropped(self):
        bars = series(MON, 4, 900)
        now = bars[-1][0] + 300                                         # last bar is 5 minutes into its period
        self.assertTrue(md.validate_bars(bars, "15m", now=now)["forming"])
        done, dropped = md.completed(bars, 900, now)
        self.assertTrue(dropped)
        self.assertEqual(len(done), 3)
        same, dropped2 = md.completed(bars, 900, bars[-1][0] + 901)
        self.assertFalse(dropped2)
        self.assertEqual(len(same), 4)

    def test_quote_stamped_last_bar_is_not_an_overlap(self):
        bars = series(MON, 4, 900) + [(MON + 3 * 900 + 400, 11, 9, 10, 10)]
        r = md.validate_bars(bars, "15m", now=MON + 6000)
        self.assertEqual(r["overlaps"], 0)
        self.assertTrue(r["last_bar_off_grid"])

    def test_single_missing_weekday_is_a_holiday_candidate(self):
        d0 = ts(2024, 1, 15, 5)                                          # Monday
        bars = [(d0, 11, 9, 10, 10), (d0 + 2 * 86400, 11, 9, 10, 10)]    # Tuesday missing
        r = md.validate_bars(bars, "1d", "cme_gold", now=d0 + 3 * 86400)
        self.assertEqual(r["holiday_candidates"], 1)
        self.assertEqual(r["unexpected_missing"], 0)

    def test_daylight_saving_shift_is_not_an_overlap(self):
        d0 = ts(2024, 3, 8, 5)
        bars = [(d0, 11, 9, 10, 10), (d0 + 86400 - 3600, 11, 9, 10, 10)]      # next stamp is an hour earlier
        r = md.validate_bars(bars, "1d", "cme_gold", now=d0 + 2 * 86400)
        self.assertEqual(r["overlaps"], 0)
        weekly = [(d0, 11, 9, 10, 10), (d0 + 604800 - 3600, 11, 9, 10, 10)]
        self.assertEqual(md.validate_bars(weekly, "1wk", "cme_gold", now=d0 + 700000)["overlaps"], 0)

    def test_real_overlap_on_daily_is_still_flagged(self):
        d0 = ts(2024, 3, 8, 5)
        bars = [(d0, 11, 9, 10, 10), (d0 + 40000, 11, 9, 10, 10), (d0 + 40000 + 86400, 11, 9, 10, 10)]
        self.assertEqual(md.validate_bars(bars, "1d", "cme_gold", now=d0 + 3 * 86400)["overlaps"], 1)

    def test_empty_series_fails(self):
        self.assertEqual(md.validate_bars([], "15m")["status"], "fail")

    def test_unknown_interval_rejected(self):
        with self.assertRaises(ValueError):
            md.validate_bars(series(MON, 2, 900), "7m")


class Provenance(unittest.TestCase):
    def test_hash_is_stable_and_sensitive(self):
        a = series(MON, 5, 900)
        b = list(a)
        self.assertEqual(md.bars_hash(a), md.bars_hash(b))
        b[2] = (b[2][0], b[2][1] + 0.01, b[2][2], b[2][3], b[2][4])
        self.assertNotEqual(md.bars_hash(a), md.bars_hash(b))

    def test_record_fields(self):
        p = md.provenance("yahoo", "GC=F", "15m", series(MON, 5, 900), fetched_at=MON, extra={"k": 1})
        for k in ("provider", "symbol", "interval", "fetched_at", "rows", "first", "last", "sha256", "k"):
            self.assertIn(k, p)
        self.assertEqual(p["rows"], 5)
        self.assertTrue(p["fetched_at"].endswith("+00:00"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = md.Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip_and_merge(self):
        bars = series(MON, 4, 900)
        self.store.save("yahoo", "GC=F", "15m", bars)
        more = series(MON + 4 * 900, 2, 900)
        meta = self.store.save("yahoo", "GC=F", "15m", more)
        loaded, m, revs = self.store.load("yahoo", "GC=F", "15m")
        self.assertEqual(len(loaded), 6)
        self.assertEqual(m["rows"], 6)
        self.assertEqual(meta["sha256"], md.bars_hash(loaded))
        self.assertEqual(revs, [])

    def test_restated_closed_bar_is_recorded_not_silent(self):
        bars = series(MON, 4, 900)
        self.store.save("yahoo", "GC=F", "15m", bars)
        changed = list(bars)
        changed[1] = (changed[1][0], 2100, 1990, 2050, 2000)
        meta = self.store.save("yahoo", "GC=F", "15m", changed)
        _, _, revs = self.store.load("yahoo", "GC=F", "15m")
        self.assertEqual(len(revs), 1)
        self.assertEqual(meta["restated_bars"], 1)

    def test_update_of_the_forming_last_bar_is_not_a_restatement(self):
        bars = series(MON, 4, 900)
        self.store.save("yahoo", "GC=F", "15m", bars)
        upd = list(bars)
        upd[-1] = (upd[-1][0], 2005, 1999, 2003, 2000)
        self.store.save("yahoo", "GC=F", "15m", upd)
        self.assertEqual(self.store.load("yahoo", "GC=F", "15m")[2], [])

    def test_missing_file_loads_empty(self):
        self.assertEqual(self.store.load("x", "y", "15m"), ([], {}, []))

    def test_unsafe_names_stay_inside_the_store(self):
        p = self.store._path("../evil", "../../x", "15m")
        self.assertTrue(os.path.abspath(p).startswith(os.path.abspath(self.dir)))


MT5 = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
       "2024.01.08\t09:05:00\t2050.10\t2051.00\t2049.50\t2050.60\t120\t0\t18\n"
       "2024.01.08\t09:10:00\t2050.60\t2052.00\t2050.00\t2051.80\t98\t0\t20\n"
       "2024.01.08\t09:15:00\tnot-a-number\t2052\t2050\t2051\t1\t0\t20\n"
       "garbage-time\t09:20:00\t1\t2\t0.5\t1\t1\t0\t20\n")


class CsvImport(unittest.TestCase):
    def test_server_time_is_converted_with_the_stated_offset(self):
        bars, extras, rejected = md.parse_csv(MT5, utc_offset_hours=2, point_size=0.01)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0][0], ts(2024, 1, 8, 7, 5))               # 09:05 server = 07:05 UTC
        self.assertEqual(bars[0], (ts(2024, 1, 8, 7, 5), 2051.0, 2049.5, 2050.6, 2050.1))
        self.assertEqual(extras["spread_points"][bars[0][0]], 18.0)
        self.assertAlmostEqual(extras["spread"][bars[0][0]], 0.18)
        self.assertEqual(len(rejected), 2)                               # nothing dropped silently

    def test_offset_is_required(self):
        with self.assertRaises(md.DataUnavailable):
            md.parse_csv(MT5, utc_offset_hours=None)

    def test_generic_csv_and_epoch(self):
        text = "time,open,high,low,close\n1704704700,10,12,9,11\n"
        bars, extras, rejected = md.parse_csv(text, utc_offset_hours=3)
        self.assertEqual(bars[0][0], 1704704700)                         # epoch is already UTC
        self.assertEqual(extras["spread_points"], {})
        self.assertEqual(rejected, [])

    def test_missing_columns_are_an_error(self):
        with self.assertRaises(md.DataUnavailable):
            md.parse_csv("a,b,c\n1,2,3\n", utc_offset_hours=0)

    def test_spread_price_needs_point_size(self):
        _, extras, _ = md.parse_csv(MT5, utc_offset_hours=2)
        self.assertNotIn("spread", extras)


class Providers(unittest.TestCase):
    def test_broker_feeds_do_not_pretend(self):
        for name in ("mt5", "oanda"):
            with self.assertRaises(md.DataUnavailable):
                md.PROVIDERS[name].fetch("XAUUSD", "5m")

    def test_yahoo_declares_what_it_lacks(self):
        notes = " ".join(md.PROVIDERS["yahoo"].describe("GC=F"))
        self.assertIn("no bid/ask", notes)
        self.assertIn("not spot XAUUSD", notes)

    def test_calendars_come_from_the_asset_profile(self):
        self.assertEqual(md.calendar_for("GC=F"), "cme_gold")
        self.assertEqual(md.calendar_for("BTC-USD"), "24x7")


if __name__ == "__main__":
    unittest.main()
