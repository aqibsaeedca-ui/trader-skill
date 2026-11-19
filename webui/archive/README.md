# Archived research (kept, runnable, not part of the active Gold signal engine)

Archived on 2026-11-19 when the Gold console was simplified to two strategy families (see `webui/docs/GOLD_REBUILD_AUDIT.md`). Nothing was deleted and nothing was moved,
so every page, test and import keeps working. "Archived" means the code is excluded from the active Gold decision path and its results are frozen as the BENCHMARK.

## The original Gold engine (benchmark)

| Code | What it did | Why it is archived |
| --- | --- | --- |
| `gold.py` `trade_plans`, `gold_intraday.py` plan ranking | Multi-zone pullback and breakout plans on 15m, 1h, 4h, daily with A/B/C scores | Loses about -0.08R (Gold only) to -0.16R (with Bitcoin) per independent trade; duplicates; grades poorly calibrated |
| `pending_strategies.py`, `pending.py`, `pending_watch.py`, `paper.py` | BUY/SELL STOP and LIMIT order families | Not validated; paper simulation only |
| `regimes.py` classifier v2, `experiments.py`, `experiments2.py`, `experiments3.py`, `strategy_policy.py` | Regime, filter, stop, exit, session, news and logistic experiments | 34 ideas tried, none adopted; kept as the research record and the statistics library |
| `scorecard.py`, `recommendation_backtest.py`, `loss_attribution.py` | Recommendation scorecard and replay | Kept active as measurement tools, they judge any engine |

Frozen result snapshots (local, not in Git because `trader/` is ignored): `trader/archive/*.original-engine.json`.

## Strategy C (VWAP/EMA regime continuation)

Research only. It needs volume; this data has none. It cannot generate a signal until it is independently validated.

## Other families from the earlier register (Asian range reversion, sweep and reclaim, opening-range breakout, post-news continuation)

Not part of the active engine. Not implemented, not validated.
