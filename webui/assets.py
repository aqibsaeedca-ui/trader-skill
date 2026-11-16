"""Per-asset settings shared by the gold and Bitcoin analysis. Standard library only.

The analysis code is the same for every asset; only the numbers that depend on the market change:
round-number spacing, how many bars make a year, what one "unit" is and how it can be bought.
"""

GOLD = dict(
    id="gold", name="Gold", short="gold", cmd="gold", symbols=("GC=F", "MGC=F", "XAUUSD=X"),
    unit="oz", per_year=252, week=5,
    steps=dict(d=100, w=250, m15=25, h1=50, h4=100),     # round-number spacing, in dollars
    lot=dict(zip(("GC=F", "MGC=F", "XAUUSD=X"), (100, 10, 1))), micro=10, micro_name="micro future (10 oz)",
    one_name="1 oz", etf_name="gold ETF or fractional shares", short_note="Short via spot/CFD only",
    default_cost=0.5, max_cost=50)

BTC = dict(
    id="btc", name="Bitcoin", short="bitcoin", cmd="btc", symbols=("BTC-USD",),
    unit="BTC", per_year=365, week=7,
    steps=dict(d=5000, w=10000, m15=500, h1=1000, h4=2500),
    lot={}, micro=0.1, micro_name="micro future (0.1 BTC)",
    one_name="1 BTC", etf_name="spot bitcoin or a bitcoin ETF", short_note="Short via futures, perps or margin only",
    default_cost=25, max_cost=500)

PROFILES = (GOLD, BTC)


def profile(symbol):
    s = (symbol or "").upper()
    for p in PROFILES:
        if s in p["symbols"]:
            return p
    return GOLD


def by_id(asset_id):
    for p in PROFILES:
        if p["id"] == asset_id:
            return p
    raise KeyError(asset_id)


def public(symbol):
    """The bits the web page needs, as plain JSON."""
    p = profile(symbol)
    return {k: p[k] for k in ("id", "name", "short", "cmd", "unit", "micro", "micro_name", "one_name",
                              "etf_name", "short_note")}


def tick_for(price):
    """Prices in a trade plan are snapped to this, so entries read as clean numbers."""
    return 0.5 if price < 10000 else 5.0 if price < 100000 else 10.0
