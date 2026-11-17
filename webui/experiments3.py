"""Phases 10 to 12: session and news filters, a purged machine-learning trade filter, and the event-driven backtester check.
Standard library only. Read only: nothing here places or changes an order.

Every rule, feature, split and decision criterion was fixed in webui/docs/PHASE_10_12_PREREGISTRATION.md BEFORE this was run.
Each hypothesis is evaluated once on the TEST split and logged, rejected ones included.
"""
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import event_backtest as eb  # noqa: E402
import experiments as ex  # noqa: E402
import experiments2 as ex2  # noqa: E402
import loss_attribution as la  # noqa: E402
import regimes  # noqa: E402
import scorecard as sc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT = os.path.join(ROOT, "trader", "experiment3_result.json")
LOG = os.path.join(ROOT, "trader", "experiment_log.json")
N_TOTAL = 25 + 4
ALPHA = 0.05 / N_TOTAL
LAMBDA, LR, STEPS, EMBARGO = 1.0, 0.1, 400, 86400
FEATURES = ("breakout", "slope_dir", "adx", "er", "vol_ratio", "h1_agrees", "h4_agrees", "score", "dist_atr", "n_agree", "risk_atr", "asia", "london", "ny", "news")


# ---------------------------------------------------------------- session and news filters -----------------------------------------------------

FILTERS = (
    ("H10a", "Skip opportunities within 60 minutes of a high-impact macro event", "Created within 3600 s of a high-importance scheduled event in the stored calendar.",
     lambda r, f: not f.get("news_near")),
    ("H10b", "Skip the Asia session", "Skip opportunities created 00:00 to 07:00 UTC.",
     lambda r, f: regimes.session_of(r["created_ts"]) != "asia"),
    ("H10c", "Only the London and New York sessions", "Keep only opportunities created 07:00 to 21:00 UTC.",
     lambda r, f: regimes.session_of(r["created_ts"]) in ("london", "ny")),
)


# ---------------------------------------------------------------- logistic regression (standard library) ----------------------------------------

def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z))))


def vector(r, f):
    s1, s4 = f.get("s1") or {}, f.get("s4") or {}
    sgn = 1 if r["side"] == "long" else -1
    agree = lambda b: 0 if b not in ("bullish", "bearish") else (1 if (b == "bullish") == (sgn == 1) else -1)   # noqa: E731
    clip = lambda v, lo, hi: None if v is None else max(lo, min(hi, v))   # noqa: E731
    atr = f.get("tf_atr")
    sess = regimes.session_of(r["created_ts"])
    sl = s1.get("slope_atr")
    return [1.0 if r["kind"] == "breakout" else 0.0, None if sl is None else clip(sl * sgn, -3, 3), s1.get("adx"), s1.get("er"), clip(s1.get("vol_ratio"), 0, 3),
            float(agree(s1.get("bias"))), float(agree(s4.get("bias"))), r.get("score"), clip(r.get("dist_atr"), 0, 5), float(len(r.get("agree") or [])),
            (clip(abs(r["entry"] - r["stop"]) / atr, 0, 6) if atr else None), 1.0 if sess == "asia" else 0.0, 1.0 if sess == "london" else 0.0, 1.0 if sess == "ny" else 0.0,
            1.0 if f.get("news_near") else 0.0]


class Logit:
    def fit(self, X, y):
        n, d = len(X), len(FEATURES)
        self.mu = []
        for j in range(d):
            v = [x[j] for x in X if x[j] is not None]
            self.mu.append(sum(v) / len(v) if v else 0.0)
        Z = [[(self.mu[j] if x[j] is None else x[j]) for j in range(d)] for x in X]
        self.sd = []
        for j in range(d):
            m = sum(z[j] for z in Z) / n
            self.sd.append(math.sqrt(sum((z[j] - m) ** 2 for z in Z) / n) or 1.0)
        self.m = [sum(z[j] for z in Z) / n for j in range(d)]
        S = [[(z[j] - self.m[j]) / self.sd[j] for j in range(d)] for z in Z]
        self.w, self.b = [0.0] * d, 0.0
        for _ in range(STEPS):
            gw, gb = [0.0] * d, 0.0
            for s, t in zip(S, y):
                e = _sigmoid(sum(w * v for w, v in zip(self.w, s)) + self.b) - t
                gb += e
                for j in range(d):
                    gw[j] += e * s[j]
            self.b -= LR * gb / n
            for j in range(d):
                self.w[j] -= LR * (gw[j] / n + LAMBDA * self.w[j] / n)
        return self

    def predict(self, x):
        z = sum(w * ((((self.mu[j] if v is None else v) - self.m[j]) / self.sd[j])) for j, (w, v) in enumerate(zip(self.w, x))) + self.b
        return _sigmoid(z)


def auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    pairs = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    rank, i, rsum = 1, 0, 0.0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + 1 + j) / 2.0
        rsum += avg * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rsum - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))


def brier(p, y):
    return sum((a - b) ** 2 for a, b in zip(p, y)) / len(y) if y else None


# ---------------------------------------------------------------- run --------------------------------------------------------------------------

def run(reps, ctxs, bars_by_symbol, events, allrecs, now, log_path=None):
    reps = sorted(reps, key=lambda r: r["created_ts"])
    cut = reps[int(ex.SPLIT_FRAC * len(reps))]["created_ts"] if reps else 0
    F = {r["id"]: ex.features(r, ctxs, events) for r in reps}
    hyps = []

    def base_trade(r):
        res = r.get("result") or {}
        return (res["fill_time"], res["r_net"]) if res.get("final") and res.get("state") in ex.FINAL_FILLED and res.get("r_net") is not None else None

    def split_metrics(pred):
        out = {}
        for name in ("train", "test"):
            sel = [r for r in reps if (r["created_ts"] < cut) == (name == "train") and pred(r)]
            out[name] = ex.metrics([t for r in sel for t in [base_trade(r)] if t], len(sel), alpha=ALPHA)
        return out

    base = split_metrics(lambda r: True)
    hyps.append(dict(phase=10, id="BASE10", name="Baseline: every opportunity", definition="No filter.", train=base["train"], test=base["test"], decision="BASELINE", reasons=["reference for the comparison"]))
    for hid, name, df, keep in FILTERS:
        ev = split_metrics(lambda r, keep=keep: keep(r, F[r["id"]]))
        dec, why = ex.decide(ev["train"], ev["test"], base["train"], base["test"])
        hyps.append(dict(phase=10, id=hid, name=name, definition=df, train=ev["train"], test=ev["test"], decision=dec, reasons=why))

    # descriptive: mean net R by session of creation, filled trades, both splits together
    by_sess = {}
    for r in reps:
        t = base_trade(r)
        if t:
            by_sess.setdefault(regimes.session_of(r["created_ts"]), []).append(t[1])
    sess_doc = {k: dict(trades=len(v), mean_r=sum(v) / len(v)) for k, v in sorted(by_sess.items())}

    # ---- Phase 11: purged logistic filter
    fills = [(r, base_trade(r)) for r in reps]
    fills = [(r, t[1]) for r, t in fills if t]
    exit_ts = {}
    for r, _ in fills:
        e = (r["result"] or {}).get("exit_time")
        exit_ts[r["id"]] = la._ts(e) if e else None

    def purged(items, boundary):
        return [(r, v) for r, v in items if exit_ts.get(r["id"]) is not None and exit_ts[r["id"]] <= boundary - EMBARGO]

    train_all = [(r, v) for r, v in fills if r["created_ts"] < cut]
    train_fit = purged(train_all, cut)
    ml = dict(features=list(FEATURES), train_fills=len(train_all), train_used_after_purge=len(train_fit), purged=len(train_all) - len(train_fit))
    keep_test = lambda r: False   # noqa: E731
    if len(train_fit) >= 50 and len({v > 0 for _, v in train_fit}) == 2:
        model = Logit().fit([vector(r, F[r["id"]]) for r, _ in train_fit], [1 if v > 0 else 0 for _, v in train_fit])
        ptrain = [model.predict(vector(r, F[r["id"]])) for r, _ in train_fit]
        thr = sorted(ptrain)[len(ptrain) // 2]
        te = [(r, v) for r, v in fills if r["created_ts"] >= cut]
        pte = [model.predict(vector(r, F[r["id"]])) for r, _ in te]
        yte = [1 if v > 0 else 0 for _, v in te]
        base_rate = sum(1 if v > 0 else 0 for _, v in train_fit) / len(train_fit)
        b_model, b_base = brier(pte, yte), brier([base_rate] * len(yte), yte)
        skill = (1 - b_model / b_base) if b_base else None
        # purged expanding-window walk-forward inside TRAIN (descriptive)
        ordered = sorted(train_all, key=lambda x: x[0]["created_ts"])
        blocks = [ordered[i * len(ordered) // 4:(i + 1) * len(ordered) // 4] for i in range(4)]
        folds = []
        for k in range(1, 4):
            if not blocks[k]:
                continue
            b0 = blocks[k][0][0]["created_ts"]
            fit = purged([x for blk in blocks[:k] for x in blk], b0)
            if len(fit) >= 50 and len({v > 0 for _, v in fit}) == 2:
                m = Logit().fit([vector(r, F[r["id"]]) for r, _ in fit], [1 if v > 0 else 0 for _, v in fit])
                a = auc([m.predict(vector(r, F[r["id"]])) for r, _ in blocks[k]], [1 if v > 0 else 0 for _, v in blocks[k]])
                folds.append(dict(fold=k, fit_n=len(fit), test_n=len(blocks[k]), auc=a))
        ml.update(threshold=thr, test_auc=auc(pte, yte), test_brier=b_model, base_rate_brier=b_base, brier_skill=skill, train_base_rate=base_rate, walk_forward=folds,
                  weights={n: w for n, w in zip(FEATURES, model.w)})
        pred_cache = {r["id"]: model.predict(vector(r, F[r["id"]])) for r in reps}
        keep_test = lambda r: pred_cache[r["id"]] >= thr   # noqa: E731
    ev = split_metrics(keep_test)
    dec, why = ex.decide(ev["train"], ev["test"], base["train"], base["test"])
    if "test_auc" not in ml:
        dec, why = "INSUFFICIENT SAMPLE", ["too few purged training fills to fit the model"]
    hyps.append(dict(phase=11, id="H11", name="Purged logistic-regression filter", definition="Keep opportunities whose predicted win probability is at or above the TRAIN median; fitted on purged TRAIN fills only.",
                     train=ev["train"], test=ev["test"], decision=dec, reasons=why))
    accepted = dec == "ADOPTED" and (ml.get("brier_skill") or 0) > 0
    ml["probability_status"] = "calibrated probabilities accepted" if accepted else "INSUFFICIENT DATA"

    # ---- Phase 12: event-driven engine reconciled with the grader, then the risk-gate replay on TEST
    rec_ev = eb.reconcile(reps, bars_by_symbol, now)      # the full plan is stored only for the unique opportunities, so the check covers those
    te_trades = []
    for r in reps:
        if r["created_ts"] >= cut:
            res = r.get("result") or {}
            if res.get("final") and res.get("state") in ex.FINAL_FILLED and res.get("r_net") is not None and res.get("fill_time") and res.get("exit_time"):
                te_trades.append(dict(fill_time=res["fill_time"], exit_time=res["exit_time"], r_net=res["r_net"], symbol=r["symbol"], side=r["side"]))
    gate = eb.gate_replay(te_trades)

    doc = dict(created=sc._iso(now), split=dict(fraction=ex.SPLIT_FRAC, cut=sc._iso(cut), train_opportunities=len([r for r in reps if r["created_ts"] < cut]),
                                                test_opportunities=len([r for r in reps if r["created_ts"] >= cut])),
               hypotheses_total=4, hypotheses_cumulative=N_TOTAL, bonferroni_alpha=ALPHA, hypotheses=hyps, sessions=sess_doc, ml=ml, engine=rec_ev, gate=gate,
               not_tested=["a news filter based on surprise size or spread widening (no such data)", "real tick-volume liquidity", "other model families (each would be another hypothesis)",
                           "real-market session opens with daylight saving"],
               caution="Six months, two markets, delayed last-trade bars, flat costs. Opportunities overlap in time, so the bands are optimistic. Nothing is tuned on the test split. Past results are not a promise. Not financial advice.")
    append_log(doc, log_path)
    return doc


def append_log(doc, path=None):
    p = path or LOG
    try:
        with open(p, encoding="utf-8") as f:
            log = json.load(f)
        log = log if isinstance(log, list) else []
    except (OSError, ValueError):
        log = []
    entry = dict(created=doc["created"], kind="phase10-12", split=doc["split"],
                 hypotheses=[dict(phase=h["phase"], id=h["id"], name=h["name"], definition=h["definition"], decision=h["decision"], reasons=h["reasons"],
                                  train_mean_r=h["train"].get("mean_r"), test_mean_r=h["test"].get("mean_r"), test_trades=h["test"].get("trades")) for h in doc["hypotheses"]],
                 engine_agreement=doc["engine"].get("agreement"))
    key = lambda e: [(h["id"], h["decision"], h["test_mean_r"], h["test_trades"]) for h in e["hypotheses"]]   # noqa: E731
    prev = [e for e in log if e.get("kind") == "phase10-12"]
    if prev and key(prev[-1]) == key(entry):
        return
    log.append(entry)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(log[-200:], f, indent=1)
    os.replace(p + ".tmp", p)


def save_result(doc, path=None):
    p = path or RESULT
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(p + ".tmp", p)


def load_result(path=None):
    try:
        with open(path or RESULT, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("hypotheses") else None
    except (OSError, ValueError):
        return None


def main(argv=None):
    import argparse
    import gold_intraday as gi
    import macro
    import recommendation_backtest as rb
    ap = argparse.ArgumentParser(description="Phases 10 to 12 pre-registered experiments.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rerun", action="store_true", help="recompute with the latest data instead of showing the saved result")
    a = ap.parse_args(argv)
    now0 = time.time()
    if not a.rerun:
        saved = load_result()
        if saved:
            print(json.dumps(saved) if a.json else "saved result from %s" % saved["created"])
            return 0
    reps, allrecs = ex.load_opportunities(), ex2.load_all()
    if not reps or not allrecs:
        rb.run()
        reps, allrecs = ex.load_opportunities(), ex2.load_all()
    if not reps:
        print(json.dumps(dict(error="no replay opportunities; run Backtest every recommendation first")) if a.json else "no replay opportunities")
        return 1
    events = macro.load_store()
    bars_by, errors = {}, []
    for s in sorted({r["symbol"] for r in reps}):
        try:
            d, w, h1, m15 = (gi.get_bars(s, "1d", "2y", 1800), gi.get_bars(s, "1wk", "5y", 3600), gi.get_bars(s, "60m", "6mo", 300), gi.get_bars(s, "15m", "30d", 60))
            bars_by[s] = {"15m": m15, "1h": h1, "4h": gi.resample(h1, 4), "daily": d, "weekly": w}
        except (Exception, SystemExit) as e:
            errors.append("%s: %s" % (s, str(e)[:160]))
    reps = [r for r in reps if r["symbol"] in bars_by]
    allrecs = [r for r in allrecs if r["symbol"] in bars_by]
    ctxs = la.build_contexts(bars_by, events)
    end = max((bars_by[s]["15m"] or bars_by[s]["1h"])[-1][0] for s in bars_by)
    doc = run(reps, ctxs, bars_by, events, allrecs, end)
    doc["errors"], doc["seconds"] = errors, round(time.time() - now0, 1)
    save_result(doc)
    if a.json:
        print(json.dumps(doc))
    else:
        for h in doc["hypotheses"]:
            print("%-7s %-58s %-20s test n=%s mean=%s" % (h["id"], h["name"][:58], h["decision"], h["test"].get("trades"), h["test"].get("mean_r")))
        print("engine agreement", doc["engine"]["agreement"], "gate", doc["gate"]["taken"], "of", doc["gate"]["candidates"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
