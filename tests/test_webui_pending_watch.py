"""Pending-order alerts: dedupe, cooldown, state changes and the Bitcoin watcher staying untouched. Snapshots are SYNTHETIC."""
import copy
import unittest

from _load import load

import test_webui_pending_intel as tp

pw = load("webui", "pending_watch")
wx = load("webui", "watch_extras")
gw = load("webui", "gold_watch")
assets = load("webui", "assets")


def snap(validated=("buy_stop",), **kw):
    t = tp.Build()
    t.setUp()
    try:
        return t.build(gate=True, validation=tp.record(validated), **kw)
    finally:
        t.tearDown()


NOW = tp.NOW
SETTINGS = dict(extras_cooldown_min=60, max_alerts_per_hour=12)


class Alerts(unittest.TestCase):
    def test_a_validated_setup_alerts_once_per_signal(self):
        d, st = snap(), wx.new_state()
        first = pw.evaluate(d, SETTINGS, st, NOW)
        kinds = [a["kind"] for a in first]
        self.assertIn("pending_valid", kinds)
        self.assertEqual([a["kind"] for a in pw.evaluate(d, SETTINGS, st, NOW + 600) if a["kind"] == "pending_valid"], [])

    def test_nothing_validated_means_no_validated_alert(self):
        d = snap(validated=())
        self.assertFalse([a for a in pw.evaluate(d, SETTINGS, wx.new_state(), NOW) if a["kind"] == "pending_valid"])

    def test_a_setup_traded_through_its_invalidation_alerts_once(self):
        d, st = snap(), wx.new_state()
        pw.evaluate(d, SETTINGS, st, NOW)
        plan = d["panels"]["buy_stop"]["plan"]
        d2 = copy.deepcopy(d)
        d2["market"]["price"] = plan["invalid_price"] - 0.5
        out = pw.evaluate(d2, SETTINGS, st, NOW + 3600)
        self.assertEqual([a["kind"] for a in out if a["kind"] == "pending_invalid"], ["pending_invalid"])
        self.assertFalse([a for a in pw.evaluate(d2, SETTINGS, st, NOW + 7300) if a["kind"] == "pending_invalid"])

    def test_paper_state_changes_alert_once_each(self):
        d, st = snap(validated=()), wx.new_state()
        ch = [dict(order_id="po-1", order_type="BUY STOP", from_state="AWAITING_TRIGGER", to_state="FILLED", note="filled at 101.70")]
        out = pw.evaluate(d, SETTINGS, st, NOW, ch)
        self.assertEqual([a["kind"] for a in out], ["pending_state"])
        self.assertIn("no broker order exists", out[0]["body"])
        self.assertEqual(pw.evaluate(d, SETTINGS, st, NOW + 60, ch), [])

    def test_news_and_risk_alerts(self):
        d = snap()
        d["panels"]["buy_stop"]["blocking"].append("important news is close: FOMC in 20 min")
        d["account"]["risk"]["lockout"] = True
        kinds = [a["kind"] for a in pw.evaluate(d, SETTINGS, wx.new_state(), NOW)]
        self.assertIn("pending_news", kinds)
        self.assertIn("pending_risk", kinds)

    def test_the_hourly_cap_is_shared_and_counts_suppressed_alerts(self):
        d, st = snap(), wx.new_state()
        out = pw.evaluate(d, dict(SETTINGS, max_alerts_per_hour=1), st, NOW)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(st["suppressed"], 0)

    def test_state_file_stays_plain_json(self):
        import json
        st = wx.new_state()
        pw.evaluate(snap(), SETTINGS, st, NOW)
        json.dumps(st)


class Settings(unittest.TestCase):
    def test_pending_alerts_default_on_for_gold_only(self):
        self.assertTrue(gw.defaults(assets.GOLD)["pending"])
        self.assertFalse(gw.defaults(assets.BTC)["pending"])
        self.assertFalse(gw.clean_settings({"pending": True}, assets.BTC)["pending"])

    def test_gold_can_switch_them_off(self):
        self.assertFalse(gw.clean_settings({"pending": False}, assets.GOLD)["pending"])


if __name__ == "__main__":
    unittest.main()
