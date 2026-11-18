"""Server wiring: new commands, input validation and the HTTP guards on the write endpoints.
Every file the server would write is redirected to a temporary folder."""
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest

from _load import ROOT, load

server = load("webui", "server")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SIG = dict(id="range-60m-1700000000-long-abc123", strategy="range", side="long", decided_t=1700000000, price=2000.0, stop=1990.0,
           targets=[2015.0, 2030.0], note="Rejected the lower edge", invalidation="Close below the range", regime="sideways")


class Redirected(unittest.TestCase):
    """Point every runtime file at a temp folder for the duration of a test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (server.portfolio.PATH, server.journal.PATH, server.research.VALIDATION, server.ml.MODEL_DIR)
        server.portfolio.PATH = os.path.join(self.dir, "portfolio.json")
        server.journal.PATH = os.path.join(self.dir, "journal.json")
        server.research.VALIDATION = os.path.join(self.dir, "validation.json")
        server.ml.MODEL_DIR = os.path.join(self.dir, "models")

    def tearDown(self):
        server.portfolio.PATH, server.journal.PATH, server.research.VALIDATION, server.ml.MODEL_DIR = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)


class Commands(unittest.TestCase):
    def test_new_commands_exist_in_the_gold_group_and_their_scripts_exist(self):
        by = {c["id"]: c for c in server.COMMANDS}
        for cid in ("intel", "research", "macro-show", "macro-update", "macro-study", "data-check", "gold", "gold-backtest", "gold-news", "btc", "btc-news"):
            self.assertIn(cid, by)
            self.assertTrue(os.path.exists(os.path.join(ROOT, by[cid]["script"])), cid)
        for cid in ("intel", "macro-show", "scorecard", "profit", "pending"):
            self.assertEqual(by[cid]["group"], "Gold")                   # the essential tabs
        for cid in ("research", "macro-update", "macro-study", "experiments", "experiments2", "experiments3", "lossattr", "recbacktest"):
            self.assertEqual(by[cid]["group"], "Gold lab")               # research and experiments live in the collapsed group
        self.assertEqual(by["intel"]["view"], "intel")
        self.assertEqual(by["research"]["view"], "research")
        self.assertIn("--json", by["research"]["fixed"])                  # the page needs the structured record
        self.assertEqual(len({c["id"] for c in server.COMMANDS}), len(server.COMMANDS))

    def test_default_arguments_build_cleanly(self):
        for c in server.COMMANDS:
            args = server.build_args(c, {})
            self.assertTrue(all(isinstance(a, str) for a in args), c["id"])

    def test_research_arguments(self):
        c = next(c for c in server.COMMANDS if c["id"] == "research")
        a = server.build_args(c, dict(symbol="GC=F", interval="15m", source="dukascopy", spread="0.4", quick=True))
        self.assertEqual(a[a.index("--interval") + 1], "15m")
        self.assertIn("--quick", a)
        with self.assertRaises(server.BadInput):
            server.build_args(c, dict(interval="7m"))
        with self.assertRaises(server.BadInput):
            server.build_args(c, dict(spread="-1"))
        with self.assertRaises(server.BadInput):
            server.build_args(c, dict(symbol="GC=F; rm -rf"))

    def test_no_command_can_place_an_order(self):
        for c in server.COMMANDS:
            if c["id"] in ("intel", "research", "macro-show", "macro-update", "macro-study", "data-check"):
                self.assertNotIn("broker", c["script"])
                self.assertNotIn("order", c["fixed"])


class PortfolioApi(Redirected):
    STATE = dict(equity=5000, positions=[dict(id="p1", symbol="GC=F", side="long", units=2, entry=2000, stop=1990, target=2030)],
                 pending=[], limits=dict(max_open_risk_pct=2, daily_loss_limit_pct=3, max_positions=3))

    def test_round_trip(self):
        out = server.portfolio_action(dict(state=self.STATE))
        self.assertEqual(out["equity"], 5000.0)
        self.assertEqual(server.portfolio_action(dict(action="get"))["positions"][0]["id"], "p1")

    def test_invalid_state_is_a_bad_input_and_nothing_is_written(self):
        with self.assertRaises(server.BadInput):
            server.portfolio_action(dict(state=dict(self.STATE, equity=-1)))
        self.assertFalse(os.path.exists(server.portfolio.PATH))
        with self.assertRaises(server.BadInput):
            server.portfolio_action(dict(state="nope"))


class JournalApi(Redirected):
    def add(self, **kw):
        body = dict(action="add_signal", signal=SIG, decision="TRADE", reasons=["all gates passed"], symbol="GC=F", interval="60m")
        body.update(kw)
        return server.journal_action(body)

    def test_add_fill_close_flow(self):
        e = self.add()
        self.assertEqual(e["id"], SIG["id"])
        self.assertEqual(e["versions"]["strategy_status"], "NOT RUN")        # the server fills versions in, the client cannot
        self.assertEqual(len(e["versions"]["code"]), 12)
        f = server.journal_action(dict(action="fill", id=SIG["id"], price=2001, units=2))
        self.assertAlmostEqual(f["fill"]["slippage"], 1.0)
        c = server.journal_action(dict(action="close", id=SIG["id"], price=2020, notes="took T1", tags="target, patient", stop_moved=False))
        self.assertAlmostEqual(c["close"]["pnl"], 38.0)
        self.assertIn("patient", c["tags"])
        got = server.journal_action(dict(action="get"))
        self.assertEqual(got["summary"]["closed"], 1)

    def test_client_supplied_versions_are_ignored(self):
        e = self.add(code_version="forged", strategy_status="VALIDATED")
        self.assertNotEqual(e["versions"]["code"], "forged")
        self.assertNotEqual(e["versions"]["strategy_status"], "VALIDATED")

    def test_bad_signals_are_rejected(self):
        bad = [dict(SIG, id="nonsense"), dict(SIG, strategy="magic"), dict(SIG, side="buy"), dict(SIG, price="x"), dict(SIG, note="<script>"),
               dict(SIG, price=float("nan")), dict(SIG, id="range-60m-1700000000-long-ABC123")]
        for s in bad:
            with self.assertRaises(server.BadInput, msg=str(s)):
                self.add(signal=s)
        with self.assertRaises(server.BadInput):
            self.add(symbol="EVIL")
        with self.assertRaises(server.BadInput):
            self.add(interval="1d")
        with self.assertRaises(server.BadInput):
            self.add(decision="MAYBE")

    def test_unknown_actions_and_ids(self):
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="delete_everything"))
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="fill", id="x", price=1, units=1))
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="fill", id=SIG["id"], price=2000, units=1))        # never journalled
        self.add(decision="NO TRADE", reasons=["not validated"])
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="fill", id=SIG["id"], price=2000, units=1))        # refused signals cannot be filled

    def test_bad_tags_and_notes_are_rejected(self):
        self.add()
        server.journal_action(dict(action="fill", id=SIG["id"], price=2000, units=1))
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="close", id=SIG["id"], price=2010, tags="Bad Tag!"))
        with self.assertRaises(server.BadInput):
            server.journal_action(dict(action="close", id=SIG["id"], price=2010, notes="<img src=x>"))


class Http(Redirected):
    """The real request handler, on a private port."""

    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.old_port = server.PORT
        server.PORT = self.port
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", self.port), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        server.PORT = self.old_port
        super().tearDown()

    def req(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        data = json.dumps(body) if body is not None else None
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
        except (ConnectionAbortedError, ConnectionResetError):          # Windows can abort a POST to a 404 before the reply is read; ask once more
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
        txt = r.read().decode()
        conn.close()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, txt

    def test_reads_work_with_the_right_host(self):
        s, d = self.req("GET", "/api/portfolio")
        self.assertEqual(s, 200)
        self.assertEqual(d["equity"], 2700.0)
        s, d = self.req("GET", "/api/journal")
        self.assertEqual((s, d["summary"]["signals"]), (200, 0))

    def test_a_foreign_host_is_refused(self):
        self.assertEqual(self.req("GET", "/api/portfolio", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("POST", "/api/portfolio", {"state": {}}, host="evil.example:80")[0], 403)

    def test_writes_need_json_and_a_local_origin(self):
        body = {"state": PortfolioApi.STATE}
        self.assertEqual(self.req("POST", "/api/portfolio", body, headers={"Origin": "http://evil.example"})[0], 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/portfolio", body=json.dumps(body), headers={"Host": "127.0.0.1:%d" % self.port, "Content-Type": "text/plain"})
        self.assertEqual(conn.getresponse().status, 415)
        conn.close()
        s, d = self.req("POST", "/api/portfolio", body, headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(s, 200)
        self.assertEqual(d["equity"], 5000.0)

    def test_bad_input_is_a_400_not_a_crash(self):
        self.assertEqual(self.req("POST", "/api/portfolio", {"state": {"equity": -5}})[0], 400)
        self.assertEqual(self.req("POST", "/api/journal", {"action": "nope"})[0], 400)
        self.assertEqual(self.req("POST", "/api/journal", {"action": "add_signal", "signal": {"id": "x"}})[0], 400)

    def test_unknown_paths(self):
        self.assertEqual(self.req("GET", "/api/orders")[0], 404)
        self.assertEqual(self.req("POST", "/api/orders", {})[0], 404)
        self.assertEqual(self.req("POST", "/api/eth/watch", {"action": "check"})[0], 404)

    def test_journal_over_http(self):
        body = dict(action="add_signal", signal=SIG, decision="NO TRADE", reasons=["strategy not validated"], symbol="GC=F", interval="60m")
        s, d = self.req("POST", "/api/journal", body)
        self.assertEqual((s, d["decision"]), (200, "NO TRADE"))
        s, d = self.req("GET", "/api/journal")
        self.assertEqual(d["summary"]["refused"], 1)

    def test_the_commands_list_includes_the_new_pages(self):
        s, d = self.req("GET", "/api/commands")
        self.assertEqual(s, 200)
        ids = {c["id"] for c in d["commands"]}
        self.assertTrue({"intel", "research", "macro-show"} <= ids)

    def test_no_order_endpoint_exists_anywhere(self):
        with open(server.__file__, encoding="utf-8") as f:
            src = f.read()
        for word in ("/api/order", "place_order", "submit_order", "create_order"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()


class PendingApi(Http):
    """Pending-order settings and paper orders over HTTP. Nothing here can reach a broker."""

    def setUp(self):
        super().setUp()
        self.saved_p = (server.orders.SETTINGS, server.paper.PATH, server.paper.AUDIT)
        server.orders.SETTINGS = os.path.join(self.dir, "pending_settings.json")
        server.paper.PATH = os.path.join(self.dir, "paper_orders.json")
        server.paper.AUDIT = os.path.join(self.dir, "paper_audit.jsonl")

    def tearDown(self):
        server.orders.SETTINGS, server.paper.PATH, server.paper.AUDIT = self.saved_p
        super().tearDown()

    def test_commands_exist(self):
        by = {c["id"]: c for c in server.COMMANDS}
        for cid in ("pending", "research-pending"):
            self.assertIn(cid, by)
            self.assertTrue(os.path.exists(os.path.join(ROOT, by[cid]["script"])), cid)
        self.assertEqual(by["pending"]["view"], "pending")

    def test_settings_round_trip_and_validation(self):
        s, d = self.req("GET", "/api/pending/settings")
        self.assertEqual((s, d["saved"]), (200, False))
        prof = dict(d["settings"]["profile"], spread=0.4)
        s, d = self.req("POST", "/api/pending/settings", {"settings": dict(d["settings"], profile=prof)})
        self.assertEqual((s, d["saved"], d["settings"]["profile"]["spread"]), (200, True, 0.4))
        self.assertEqual(self.req("POST", "/api/pending/settings", {"settings": {"profile": {"spread": -1}}})[0], 400)

    def test_paper_orders_start_empty_and_say_no_broker_order_exists(self):
        s, d = self.req("GET", "/api/paper_orders")
        self.assertEqual((s, d["orders"]), (200, []))
        self.assertIn("no broker", d["broker_orders"])

    def test_bad_paper_requests_are_400(self):
        self.assertEqual(self.req("POST", "/api/paper_orders", {"action": "nope"})[0], 400)
        self.assertEqual(self.req("POST", "/api/paper_orders", {"action": "create", "symbol": "EVIL", "interval": "60m", "order_type": "BUY STOP"})[0], 400)
        self.assertEqual(self.req("POST", "/api/paper_orders", {"action": "cancel", "id": "missing"})[0], 400)

    def test_paper_writes_need_a_local_origin(self):
        self.assertEqual(self.req("POST", "/api/paper_orders", {"action": "get"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/pending/settings", {"action": "get"}, host="evil.example:80")[0], 403)


class PolicyApi(Http):
    """The strict mode setting over HTTP, with the file redirected."""

    def setUp(self):
        super().setUp()
        self.saved_pol = server.strategy_policy.PATH
        server.strategy_policy.PATH = os.path.join(self.dir, "strict_mode.json")

    def tearDown(self):
        server.strategy_policy.PATH = self.saved_pol
        super().tearDown()

    def test_default_off_toggle_and_reject_junk(self):
        self.assertEqual(self.req("GET", "/api/policy"), (200, dict(strict=False)))
        self.assertEqual(self.req("POST", "/api/policy", {"strict": True}), (200, dict(strict=True)))
        self.assertEqual(self.req("GET", "/api/policy")[1], dict(strict=True))
        self.assertEqual(self.req("POST", "/api/policy", {"strict": "yes"})[0], 400)

    def test_the_policy_endpoint_is_guarded_like_the_others(self):
        self.assertEqual(self.req("POST", "/api/policy", {"strict": True}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/policy", {"strict": True}, host="evil.example:80")[0], 403)
        self.assertFalse(server.strategy_policy.load()["strict"])

    def test_the_experiment_commands_exist(self):
        by = {c["id"]: c for c in server.COMMANDS}
        for cid in ("scorecard", "recbacktest", "lossattr", "experiments", "experiments2"):
            self.assertIn(cid, by)
            self.assertTrue(os.path.exists(os.path.join(ROOT, by[cid]["script"])), cid)

class CommandCentreApi(Http):
    """The broker quote endpoint, with the quote file redirected. The analysis endpoint needs the network and is covered by injected tests."""

    def setUp(self):
        super().setUp()
        self.saved_q = server.command_centre.QUOTE
        server.command_centre.QUOTE = os.path.join(self.dir, "broker_quote.json")

    def tearDown(self):
        server.command_centre.QUOTE = self.saved_q
        super().tearDown()

    def test_quote_is_validated_and_guarded(self):
        self.assertEqual(self.req("POST", "/api/gold/command-centre/quote", {"bid": 2001, "ask": 2000})[0], 400)
        self.assertEqual(self.req("POST", "/api/gold/command-centre/quote", {"bid": "x", "ask": 1})[0], 400)
        self.assertEqual(self.req("POST", "/api/gold/command-centre/quote", {"bid": 2000, "ask": 2000.3}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertFalse(os.path.exists(server.command_centre.QUOTE))
        code, body = self.req("POST", "/api/gold/command-centre/quote", {"bid": 2000, "ask": 2000.3})
        self.assertEqual((code, body["status"]), (200, "ok"))

    def test_the_page_and_endpoint_are_part_of_the_console(self):
        self.assertTrue(callable(server.command_centre.build))


for _cls in (PendingApi, PolicyApi, CommandCentreApi):                          # these only add their own tests; they must not re-run the ones they inherit
    for _name in dir(Http):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)
