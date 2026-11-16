"""Causal indicators over bars (t, high, low, close, open). Standard library only.

Every function returns a list the same length as the input. Element i uses ONLY bars[0..i], so a value never
changes when later bars arrive. Warm-up elements are None. tests/test_webui_regimes.py checks this property.
"""
import math


def true_ranges(bars):
    out = []
    for i, b in enumerate(bars):
        h, l = b[1], b[2]
        if i == 0:
            out.append(h - l)
        else:
            pc = bars[i - 1][3]
            out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(bars, n=14):
    """Wilder average true range. First value at index n (mean of true ranges 1..n)."""
    tr = true_ranges(bars)
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    cur = sum(tr[1:n + 1]) / n
    out[n] = cur
    for i in range(n + 1, len(bars)):
        cur = (cur * (n - 1) + tr[i]) / n
        out[i] = cur
    return out


def adx(bars, n=14):
    """Wilder ADX with +DI and -DI. Returns (adx, plus_di, minus_di). ADX starts at index 2n-1."""
    m = len(bars)
    a, p, q = [None] * m, [None] * m, [None] * m
    if m < 2 * n:
        return a, p, q
    tr = true_ranges(bars)
    pdm, mdm = [0.0] * m, [0.0] * m
    for i in range(1, m):
        up = bars[i][1] - bars[i - 1][1]
        dn = bars[i - 1][2] - bars[i][2]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = [None] * m

    def di(i):
        pd = 100.0 * s_p / s_tr if s_tr else 0.0
        md = 100.0 * s_m / s_tr if s_tr else 0.0
        p[i], q[i] = pd, md
        tot = pd + md
        dx[i] = 100.0 * abs(pd - md) / tot if tot else 0.0
    di(n)
    for i in range(n + 1, m):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        di(i)
    cur = sum(dx[n:2 * n]) / n
    a[2 * n - 1] = cur
    for i in range(2 * n, m):
        cur = (cur * (n - 1) + dx[i]) / n
        a[i] = cur
    return a, p, q


def realised_vol(bars, n=20):
    """Standard deviation of log returns over the last n bars (per-bar, not annualised)."""
    c = [b[3] for b in bars]
    r = [None] + [math.log(c[i] / c[i - 1]) if c[i] > 0 and c[i - 1] > 0 else 0.0 for i in range(1, len(c))]
    out = [None] * len(c)
    for i in range(n, len(c)):
        w = r[i - n + 1:i + 1]
        mu = sum(w) / n
        out[i] = math.sqrt(sum((x - mu) ** 2 for x in w) / (n - 1))
    return out


def efficiency_ratio(bars, n=20):
    """Kaufman efficiency: net move over the window divided by the total path. 1 = straight line, 0 = noise."""
    c = [b[3] for b in bars]
    out = [None] * len(c)
    for i in range(n, len(c)):
        path = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i] - c[i - n]) / path if path else 0.0
    return out


def slope(bars, n=20):
    """Least-squares slope of closes over the last n bars, in price units per bar."""
    c = [b[3] for b in bars]
    out = [None] * len(c)
    xs = list(range(n))
    mx = (n - 1) / 2.0
    sxx = sum((x - mx) ** 2 for x in xs)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        my = sum(w) / n
        out[i] = sum((x - mx) * (y - my) for x, y in zip(xs, w)) / sxx
    return out


def rolling_range(bars, n=20):
    """(highest high, lowest low) over the last n bars including this one."""
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        w = bars[i - n + 1:i + 1]
        out[i] = (max(b[1] for b in w), min(b[2] for b in w))
    return out


def range_zscore(bars, n=20):
    """How unusual this bar's true range is against the previous n bars (excluding itself)."""
    tr = true_ranges(bars)
    out = [None] * len(bars)
    for i in range(n, len(bars)):
        w = tr[i - n:i]
        mu = sum(w) / n
        sd = math.sqrt(sum((x - mu) ** 2 for x in w) / (n - 1))
        out[i] = (tr[i] - mu) / sd if sd else 0.0
    return out


def ema(vals, n):
    """Exponential average; None inputs are skipped until n real values exist."""
    out = [None] * len(vals)
    k = 2.0 / (n + 1)
    cur, seen, acc = None, 0, 0.0
    for i, v in enumerate(vals):
        if v is None:
            continue
        if cur is None:
            acc += v
            seen += 1
            if seen == n:
                cur = acc / n
                out[i] = cur
        else:
            cur = v * k + cur * (1 - k)
            out[i] = cur
    return out
