"""Statistical and machine-learning research layer. Pure Python (no numpy, no scikit-learn).

Order of work: interpretable baselines first (a constant prior and a regularised logistic regression), then small
gradient-boosted trees. Every feature is causal: it uses only bars up to the decision bar. Labels look forward and
are used only for training and scoring. Splits are chronological with an embargo so label windows never overlap
between train, validation and test. Probabilities are calibrated on the validation block, never on the test block.

A model is REJECTED unless it beats the simple baselines out of sample, after costs. Rejection is the expected
outcome on small or noisy data and is reported plainly; a rejected model's probabilities are never shown as advice.
"""
import hashlib
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regimes  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "trader", "models")

FEATURES = [
    "side", "ret1", "ret4", "ret16", "adx", "er", "slope", "vol_ratio", "range_z", "bias", "bos_age", "bos_dir", "choch_age",
    "choch_dir", "d_against", "d_toward", "range_pos", "body", "wick_toward", "wick_away", "sma_dist", "sess_asia", "sess_london",
    "sess_ny", "strong_with", "strong_against", "weak_with", "weak_against", "sideways", "event_next", "event_last"]

ACCEPT_DEFAULTS = dict(min_test_rows=200, min_logloss_gain=0.002, max_ece=0.08, min_selected=30, auc_boot=200, seed=7)


# ------------------------------------------------------------------------------------ numerics

def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 50)))
    e = math.exp(max(z, -50))
    return e / (1.0 + e)


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _solve(A, b):
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            M[p][c] = 1e-12
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


class Scaler:
    def __init__(self, X):
        d = len(X[0])
        n = len(X)
        self.mean = [sum(r[j] for r in X) / n for j in range(d)]
        self.std = []
        for j in range(d):
            v = sum((r[j] - self.mean[j]) ** 2 for r in X) / max(1, n - 1)
            self.std.append(math.sqrt(v) if v > 1e-12 else 1.0)

    def apply(self, X):
        return [[(r[j] - self.mean[j]) / self.std[j] for j in range(len(r))] for r in X]

    def to_dict(self):
        return dict(mean=self.mean, std=self.std)


def fit_logistic(X, y, l2=1.0, iters=30):
    """Ridge-regularised logistic regression by Newton steps. w[0] is the bias and is not penalised."""
    n, d = len(X), len(X[0])
    w = [0.0] * (d + 1)
    for _ in range(iters):
        g = [0.0] * (d + 1)
        H = [[0.0] * (d + 1) for _ in range(d + 1)]
        for xi, yi in zip(X, y):
            z = w[0] + sum(w[j + 1] * xi[j] for j in range(d))
            p = sigmoid(z)
            r, s = yi - p, max(p * (1 - p), 1e-6)
            v = [1.0] + xi
            for a in range(d + 1):
                g[a] += r * v[a]
                va = v[a] * s
                row = H[a]
                for b in range(a, d + 1):
                    row[b] += va * v[b]
        for a in range(d + 1):
            for b in range(a):
                H[a][b] = H[b][a]
            if a > 0:
                g[a] -= l2 * w[a]
                H[a][a] += l2
            H[a][a] += 1e-8
        step = _solve(H, g)
        w = [wi + si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return w


def predict_logistic(w, X):
    d = len(w) - 1
    return [sigmoid(w[0] + sum(w[j + 1] * xi[j] for j in range(d))) for xi in X]


class GradientBoosting:
    """Newton-boosted decision trees on the logistic loss (the same idea as XGBoost, tiny and deterministic)."""

    def __init__(self, n_estimators=40, lr=0.1, depth=2, lam=5.0, min_leaf=20, n_thresh=12):
        self.p = dict(n_estimators=n_estimators, lr=lr, depth=depth, lam=lam, min_leaf=min_leaf, n_thresh=n_thresh)
        self.base = 0.0
        self.trees = []

    def _build(self, X, g, h, idx, depth):
        p = self.p
        G, Hs = sum(g[i] for i in idx), sum(h[i] for i in idx)
        leaf = dict(leaf=G / (Hs + p["lam"]))
        if depth == 0 or len(idx) < 2 * p["min_leaf"]:
            return leaf
        best, best_gain = None, 1e-9
        base_score = G * G / (Hs + p["lam"])
        for f in range(len(X[0])):
            order = sorted(idx, key=lambda i: X[i][f])
            gl = hl = 0.0
            step = max(1, len(order) // p["n_thresh"])
            for k in range(len(order) - 1):
                i = order[k]
                gl += g[i]
                hl += h[i]
                if (k + 1) % step or k + 1 < p["min_leaf"] or len(order) - k - 1 < p["min_leaf"] or X[order[k]][f] == X[order[k + 1]][f]:
                    continue
                gr, hr = G - gl, Hs - hl
                gain = gl * gl / (hl + p["lam"]) + gr * gr / (hr + p["lam"]) - base_score
                if gain > best_gain:
                    best_gain, best = gain, (f, (X[order[k]][f] + X[order[k + 1]][f]) / 2.0)
        if best is None:
            return leaf
        f, thr = best
        left = [i for i in idx if X[i][f] <= thr]
        right = [i for i in idx if X[i][f] > thr]
        return dict(f=f, thr=thr, l=self._build(X, g, h, left, depth - 1), r=self._build(X, g, h, right, depth - 1))

    def fit(self, X, y):
        n = len(X)
        self.base = logit(sum(y) / n)
        F = [self.base] * n
        for _ in range(self.p["n_estimators"]):
            pr = [sigmoid(v) for v in F]
            g = [yi - pi for yi, pi in zip(y, pr)]
            h = [max(pi * (1 - pi), 1e-6) for pi in pr]
            tree = self._build(X, g, h, list(range(n)), self.p["depth"])
            self.trees.append(tree)
            for i in range(n):
                F[i] += self.p["lr"] * self._walk(tree, X[i])
        return self

    @staticmethod
    def _walk(node, x):
        while "leaf" not in node:
            node = node["l"] if x[node["f"]] <= node["thr"] else node["r"]
        return node["leaf"]

    def raw(self, X):
        return [self.base + self.p["lr"] * sum(self._walk(t, x) for t in self.trees) for x in X]

    def predict(self, X):
        return [sigmoid(v) for v in self.raw(X)]

    def importance(self, names):
        counts = {}

        def walk(n):
            if "leaf" in n:
                return
            counts[names[n["f"]]] = counts.get(names[n["f"]], 0) + 1
            walk(n["l"])
            walk(n["r"])
        for t in self.trees:
            walk(t)
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def to_dict(self):
        return dict(params=self.p, base=self.base, trees=self.trees)

    @classmethod
    def from_dict(cls, d):
        g = cls(**d["params"])
        g.base, g.trees = d["base"], d["trees"]
        return g


# ------------------------------------------------------------------------------------ metrics

def log_loss(y, p):
    return -sum(yi * math.log(min(max(pi, 1e-9), 1 - 1e-9)) + (1 - yi) * math.log(min(max(1 - pi, 1e-9), 1 - 1e-9)) for yi, pi in zip(y, p)) / len(y)


def brier(y, p):
    return sum((pi - yi) ** 2 for yi, pi in zip(y, p)) / len(y)


def auc(y, p):
    """Probability that a random positive outranks a random negative (ties count half)."""
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(p)), key=lambda i: p[i])
    ranks = [0.0] * len(p)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    s = sum(ranks[i] for i in range(len(y)) if y[i] == 1)
    return (s - pos * (pos + 1) / 2.0) / (pos * neg)


def auc_ci(y, p, n_boot=200, seed=7):
    rnd = random.Random(seed)
    n = len(y)
    vals = []
    for _ in range(n_boot):
        idx = [rnd.randrange(n) for _ in range(n)]
        a = auc([y[i] for i in idx], [p[i] for i in idx])
        if a is not None:
            vals.append(a)
    if len(vals) < 20:
        return (None, None)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def calibration_bins(y, p, n_bins=10):
    """Reliability table: for each probability bin, how many cases and how often the event really happened."""
    rows = []
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [i for i in range(len(p)) if lo <= p[i] < hi or (b == n_bins - 1 and p[i] == 1.0)]
        if not idx:
            rows.append(dict(lo=lo, hi=hi, n=0, mean_p=None, freq=None, ci=[None, None]))
            continue
        k = sum(y[i] for i in idx)
        w = _wilson(k, len(idx))
        rows.append(dict(lo=lo, hi=hi, n=len(idx), mean_p=sum(p[i] for i in idx) / len(idx), freq=k / len(idx), ci=list(w)))
    return rows


def _wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def ece(y, p, n_bins=10):
    n = len(y)
    tot = 0.0
    for r in calibration_bins(y, p, n_bins):
        if r["n"]:
            tot += abs(r["freq"] - r["mean_p"]) * r["n"] / n
    return tot


def fit_platt(raw_scores, y):
    """Map a model's raw log-odds to a calibrated probability: p = sigmoid(a * score + b)."""
    w = fit_logistic([[s] for s in raw_scores], y, l2=1e-6)
    return dict(a=w[1], b=w[0])


def apply_platt(pl, raw_scores):
    return [sigmoid(pl["a"] * s + pl["b"]) for s in raw_scores]


# ------------------------------------------------------------------------------------ dataset

def barrier(bars, i, side, atr, stop_atr, target_r, horizon, cost_per_unit):
    """Label for a trade entered at the open of bar i+1: target (target_r x risk) before stop (1 x risk) within the
    horizon. Stop and target in one bar counts as the stop. Time exit marks to the close of the last bar."""
    entry = bars[i + 1][4]
    risk = stop_atr * atr
    stop = entry - side * risk
    target = entry + side * target_r * risk
    mae = 0.0
    last = min(len(bars) - 1, i + horizon)
    for j in range(i + 1, last + 1):
        h, l, c = bars[j][1], bars[j][2], bars[j][3]
        mae = max(mae, ((entry - l) if side == 1 else (h - entry)) / risk)
        if (l <= stop) if side == 1 else (h >= stop):
            r, y, how = -1.0, 0, "stop"
            break
        if (h >= target) if side == 1 else (l <= target):
            r, y, how = target_r, 1, "target"
            break
    else:
        j = last
        r = (bars[last][3] - entry) * side / risk
        y, how = (1 if r >= target_r else 0), "time"
    cost = cost_per_unit / risk
    return dict(y=y, r_gross=r, r_net=r - cost, bars=j - i, mae_r=mae, how=how, cost_r=cost)


def feature_row(A, i, side, event_times=()):
    """Causal features for a trade in direction `side` (+1 long, -1 short), decided at the close of bar i."""
    bars = A.bars
    atr = A.atr[i]
    t, h, l, c, o = bars[i][0], bars[i][1], bars[i][2], bars[i][3], bars[i][4]
    snap = A.snaps[i]
    cl = lambda k: bars[max(0, i - k)][3]
    sl = A.slope[i] / atr if (A.slope[i] is not None and atr) else 0.0
    sup, res = A.nearest(c)
    d_sup = min((c - sup[0]["price"]) / atr, 8.0) if sup else 8.0
    d_res = min((res[0]["price"] - c) / atr, 8.0) if res else 8.0
    win = bars[max(0, i - 19):i + 1]
    hi, lo = max(b[1] for b in win), min(b[2] for b in win)
    pos = (c - lo) / (hi - lo) if hi > lo else 0.5
    rng = h - l
    body = (c - o) / rng if rng > 0 else 0.0
    up_w = (h - max(o, c)) / rng if rng > 0 else 0.0
    dn_w = (min(o, c) - l) / rng if rng > 0 else 0.0
    sma = sum(b[3] for b in win) / len(win)
    tr = snap["trend"]
    strong_up, strong_dn = tr == "strong_bull", tr == "strong_bear"
    weak_up, weak_dn = tr == "weak_bull", tr == "weak_bear"
    bos, choch = A.last_bos, A.last_choch
    ev_next = ev_last = 1.0
    if event_times:
        nxt = [e for e in event_times if e > t]
        prv = [e for e in event_times if e <= t]
        ev_next = min((min(nxt) - t) / 60.0, 480.0) / 480.0 if nxt else 1.0
        ev_last = min((t - max(prv)) / 60.0, 480.0) / 480.0 if prv else 1.0
    f = dict(
        side=float(side), ret1=side * (c - cl(1)) / atr, ret4=side * (c - cl(4)) / atr, ret16=side * (c - cl(16)) / atr,
        adx=(snap["adx"] or 0.0) / 100.0, er=snap["er"] or 0.0, slope=max(-1.0, min(1.0, side * sl)),
        vol_ratio=snap["vol_ratio"] or 1.0, range_z=max(0.0, min(6.0, A.rz[i] or 0.0)),
        bias=side * (1.0 if snap["bias"] == "bullish" else -1.0 if snap["bias"] == "bearish" else 0.0),
        bos_age=min(i - bos["i"], 50) / 50.0 if bos else 1.0, bos_dir=side * (1.0 if bos and bos["dir"] == "bull" else -1.0 if bos else 0.0),
        choch_age=min(i - choch["i"], 50) / 50.0 if choch else 1.0,
        choch_dir=side * (1.0 if choch and choch["dir"] == "bull" else -1.0 if choch else 0.0),
        d_against=d_sup if side == 1 else d_res, d_toward=d_res if side == 1 else d_sup,
        range_pos=pos if side == 1 else 1 - pos, body=side * body, wick_toward=up_w if side == 1 else dn_w,
        wick_away=dn_w if side == 1 else up_w, sma_dist=side * (c - sma) / atr,
        sess_asia=1.0 if snap["session"] == "asia" else 0.0, sess_london=1.0 if snap["session"] == "london" else 0.0,
        sess_ny=1.0 if snap["session"] == "ny" else 0.0,
        strong_with=1.0 if (strong_up and side == 1) or (strong_dn and side == -1) else 0.0,
        strong_against=1.0 if (strong_dn and side == 1) or (strong_up and side == -1) else 0.0,
        weak_with=1.0 if (weak_up and side == 1) or (weak_dn and side == -1) else 0.0,
        weak_against=1.0 if (weak_dn and side == 1) or (weak_up and side == -1) else 0.0,
        sideways=1.0 if tr == "sideways" else 0.0, event_next=ev_next, event_last=ev_last)
    return [f[k] for k in FEATURES]


def build_dataset(bars, interval_sec=None, horizon=24, target_r=1.5, stop_atr=1.0, stride=2, cost_per_unit=0.0,
                  event_times=None, warmup=60, cfg=None):
    """One row per (bar, side). Features use bars up to i; the label uses bars after i. stride thins the rows so
    neighbouring rows are less alike (their label windows still overlap, which is why the splits use an embargo)."""
    A = regimes.Analysis(bars, cfg, event_times=event_times, interval_sec=interval_sec)
    ev = sorted(event_times or [])
    rows = []
    for i in range(len(bars)):
        A.step(i)
        if i < warmup or i % stride or A.atr[i] is None or i + horizon + 1 >= len(bars):
            continue
        for side in (1, -1):
            lab = barrier(bars, i, side, A.atr[i], stop_atr, target_r, horizon, cost_per_unit)
            rows.append(dict(i=i, t=bars[i][0], side=side, x=feature_row(A, i, side, ev), **lab))
    return rows


def split_chronological(rows, horizon, fracs=(0.6, 0.2, 0.2)):
    """Train, validation and test blocks in time order. The last `horizon` bars of each earlier block are dropped
    (embargo) so no label window reaches into the next block."""
    if not rows:
        return [], [], []
    last_i = max(r["i"] for r in rows)
    first_i = min(r["i"] for r in rows)
    span = last_i - first_i
    b1 = first_i + int(span * fracs[0])
    b2 = first_i + int(span * (fracs[0] + fracs[1]))
    train = [r for r in rows if r["i"] < b1 - horizon]
    val = [r for r in rows if b1 <= r["i"] < b2 - horizon]
    test = [r for r in rows if r["i"] >= b2]
    return train, val, test


# ------------------------------------------------------------------------------------ evaluation

def _mean(v):
    return sum(v) / len(v) if v else None


def _ci_mean(v, n_boot=500, seed=7):
    if len(v) < 10:
        return (None, None)
    rnd = random.Random(seed)
    n = len(v)
    ms = sorted(sum(v[rnd.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (ms[int(0.025 * n_boot)], ms[int(0.975 * n_boot) - 1])


def pick_threshold(p_val, r_val, min_n=20):
    """Probability cut-off that maximises net expectancy on the VALIDATION block (never on test)."""
    best, best_e = None, None
    for q in (0.5, 0.6, 0.7, 0.8, 0.9):
        srt = sorted(p_val)
        thr = srt[int(q * (len(srt) - 1))]
        sel = [r for p, r in zip(p_val, r_val) if p >= thr]
        if len(sel) >= min_n:
            e = _mean(sel)
            if best_e is None or e > best_e:
                best, best_e = thr, e
    return best


def evaluate_rows(rows, horizon, seed=7, accept=None, model_kind="both", gbm=None):
    """Train on the first block, calibrate and choose a threshold on the second, score the untouched third.
    Returns every model's metrics, the baselines and an accept/reject decision with reasons."""
    acc = dict(ACCEPT_DEFAULTS, **(accept or {}))
    tr, va, te = split_chronological(rows, horizon)
    out = dict(rows=len(rows), train=len(tr), val=len(va), test=len(te), horizon=horizon, models={}, baselines={}, accepted=None)
    if len(tr) < 100 or len(va) < 50 or len(te) < acc["min_test_rows"]:
        out["decision"] = dict(status="REJECTED", reasons=["too few rows (train %d, validation %d, test %d); need at least 100, 50 and %d"
                                                          % (len(tr), len(va), len(te), acc["min_test_rows"])])
        return out
    Xtr, ytr = [r["x"] for r in tr], [r["y"] for r in tr]
    sc = Scaler(Xtr)
    Z = lambda rs: sc.apply([r["x"] for r in rs])
    Ztr, Zva, Zte = sc.apply(Xtr), Z(va), Z(te)
    yva, yte = [r["y"] for r in va], [r["y"] for r in te]
    rva, rte = [r["r_net"] for r in va], [r["r_net"] for r in te]
    prior = sum(ytr) / len(ytr)
    out["baselines"] = dict(
        prior=dict(p=prior, log_loss=log_loss(yte, [prior] * len(yte)), brier=brier(yte, [prior] * len(yte))),
        take_all=dict(n=len(rte), expectancy=_mean(rte), ci=list(_ci_mean(rte, seed=seed))),
        no_trade=dict(n=0, expectancy=0.0))
    cands = {}
    w = fit_logistic(Ztr, ytr, l2=5.0)
    cands["logistic"] = dict(raw_va=[logit(p) for p in predict_logistic(w, Zva)], raw_te=[logit(p) for p in predict_logistic(w, Zte)],
                             params=dict(weights=w))
    if model_kind in ("both", "gbm"):
        g = GradientBoosting(**(gbm or {})).fit(Ztr, ytr)
        cands["gbm"] = dict(raw_va=g.raw(Zva), raw_te=g.raw(Zte), params=g.to_dict(), importance=g.importance(FEATURES))
    best_name, best_gain = None, None
    for name, c in cands.items():
        pl = fit_platt(c["raw_va"], yva)
        p_va, p_te = apply_platt(pl, c["raw_va"]), apply_platt(pl, c["raw_te"])
        thr = pick_threshold(p_va, rva, 20)
        sel = [r for p, r in zip(p_te, rte) if thr is not None and p >= thr]
        lo, hi = auc_ci(yte, p_te, acc["auc_boot"], seed)
        m = dict(log_loss=log_loss(yte, p_te), brier=brier(yte, p_te), auc=auc(yte, p_te), auc_ci=[lo, hi], ece=ece(yte, p_te),
                 platt=pl, threshold=thr, selected=dict(n=len(sel), expectancy=_mean(sel), ci=list(_ci_mean(sel, seed=seed))),
                 bins=calibration_bins(yva, p_va), params=c["params"], importance=c.get("importance"),
                 log_loss_gain=out["baselines"]["prior"]["log_loss"] - log_loss(yte, p_te))
        out["models"][name] = m
        if best_gain is None or m["log_loss_gain"] > best_gain:
            best_name, best_gain = name, m["log_loss_gain"]
    out["scaler"] = sc.to_dict()
    out["best"] = best_name
    out["decision"] = decide(out, best_name, acc)
    return out


def decide(ev, name, acc):
    """Accept only if the model beats the constant prior AND the take-everything baseline out of sample, after costs,
    is calibrated, and its selected trades have a positive net expectancy whose band excludes zero."""
    m = ev["models"][name]
    reasons = []
    if m["log_loss_gain"] < acc["min_logloss_gain"]:
        reasons.append("log loss improves on the constant prior by only %.4f (needs %.4f)" % (m["log_loss_gain"], acc["min_logloss_gain"]))
    if not m["auc_ci"][0] or m["auc_ci"][0] <= 0.5:
        reasons.append("the AUC band reaches 0.5, so ranking skill is not distinguishable from chance")
    if m["ece"] > acc["max_ece"]:
        reasons.append("calibration error %.3f is above %.2f" % (m["ece"], acc["max_ece"]))
    s = m["selected"]
    if s["n"] < acc["min_selected"]:
        reasons.append("only %d trades pass the threshold on test (needs %d)" % (s["n"], acc["min_selected"]))
    elif not s["expectancy"] or s["expectancy"] <= 0:
        reasons.append("selected trades have no positive net expectancy after costs")
    elif not s["ci"][0] or s["ci"][0] <= 0:
        reasons.append("the band on selected-trade expectancy includes zero")
    base = ev["baselines"]["take_all"]["expectancy"]
    if s["n"] >= acc["min_selected"] and s["expectancy"] is not None and base is not None and s["expectancy"] <= base:
        reasons.append("selected trades do no better than taking every trade (%.3f vs %.3f R)" % (s["expectancy"], base))
    return dict(status="ACCEPTED (research, past data only)" if not reasons else "REJECTED", reasons=reasons, model=name)


# ------------------------------------------------------------------------------------ registry

def code_version():
    h = hashlib.sha256()
    with open(os.path.abspath(__file__), "rb") as f:
        h.update(f.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def package(ev, rows, symbol, interval, config, seed):
    """A versioned, self-describing record of one training run (accepted or not)."""
    name = ev.get("best")
    body = dict(created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), symbol=symbol, interval=interval, kind=name,
                features=FEATURES, config=config, seed=seed, code_version=code_version(), decision=ev["decision"],
                metrics={k: {kk: vv for kk, vv in v.items() if kk != "params"} for k, v in ev.get("models", {}).items()},
                baselines=ev.get("baselines"), counts=dict(rows=ev["rows"], train=ev["train"], val=ev["val"], test=ev["test"]),
                data_sha256=hashlib.sha256(json.dumps([(r["i"], r["side"], r["y"]) for r in rows]).encode()).hexdigest())
    if name:
        body["scaler"] = ev["scaler"]
        body["params"] = ev["models"][name]["params"]
        body["platt"] = ev["models"][name]["platt"]
        body["threshold"] = ev["models"][name]["threshold"]
        body["bins"] = ev["models"][name]["bins"]
    body["id"] = hashlib.sha1(json.dumps({k: body[k] for k in ("symbol", "interval", "kind", "features", "config", "seed", "code_version", "data_sha256")},
                                         sort_keys=True).encode()).hexdigest()[:12]
    return body


def save_model(body, directory=None):
    d = directory or MODEL_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s_%s_%s.json" % (body["symbol"].replace("=", "").replace("/", ""), body["interval"], body["id"]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f)
    os.replace(tmp, path)
    return path


def latest_accepted(symbol, interval, directory=None):
    """Newest saved model for this market that passed the acceptance rules, or None."""
    d = directory or MODEL_DIR
    best = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if m.get("symbol") == symbol and m.get("interval") == interval and m["decision"]["status"].startswith("ACCEPTED"):
            if best is None or m["created"] > best["created"]:
                best = m
    return best


def predict(model, x):
    """Calibrated probability for one feature vector, with the uncertainty of its calibration bin. Raises if the
    model was rejected: a rejected model must never produce a probability that looks usable."""
    if not model["decision"]["status"].startswith("ACCEPTED"):
        raise ValueError("model %s was rejected and cannot be used to estimate probabilities" % model["id"])
    sc = model["scaler"]
    z = [[(v - m) / s for v, m, s in zip(x, sc["mean"], sc["std"])]]
    if model["kind"] == "logistic":
        raw = logit(predict_logistic(model["params"]["weights"], z)[0])
    else:
        raw = GradientBoosting.from_dict(model["params"]).raw(z)[0]
    p = apply_platt(model["platt"], [raw])[0]
    bin_ = next((b for b in model["bins"] if b["n"] and b["lo"] <= p < b["hi"]), None)
    return dict(p=p, bin_n=bin_["n"] if bin_ else 0, interval=bin_["ci"] if bin_ else [None, None], model_id=model["id"])
