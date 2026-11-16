# XAUUSD engine: audit and phased plan

Scope: the Gold and Bitcoin parts of the local trader console (`webui/`). Method: read every source file in
`webui/`, ran the gold and BTC backtests, ran the Gold assessment end to end, and checked the test suite.
Nothing in this document is evidence that any strategy is profitable.

## 1. What exists today

| Piece | File | What it does |
| --- | --- | --- |
| Data | `gold.py` `fetch_bars` | Yahoo Finance unofficial chart API. Daily, weekly, 60m, 15m bars as `(t, high, low, close, open)`. No bid/ask, no volume. |
| Intraday | `gold_intraday.py` | 15m (30 days), 1h (6 months), 4h resampled from 1h in fixed UTC blocks, daily 2y, weekly 5y. In-memory TTL cache. Multi-timeframe confluence. |
| Zones and setups | `gold.py` `compute_setups` | Fractal swings, Fibonacci, pivots, averages, round numbers clustered into zones. Four scenarios per chart. |
| Ranked trade plans | `gold.py` `trade_plans` | Up to 10 candidate trades per chart, hand-weighted score, grade A/B/C, three targets. This is what the page shows. |
| Backtest | `gold_backtest.py` | Daily bars only. Replays the four `compute_setups` scenarios day by day. Flat cost per unit. |
| News | `gold_news.py` | Keyword rulebook over Yahoo/Google News RSS titles, cross-market correlation table. |
| Watcher | `gold_watch.py` | Per-asset thread, polls every 2 to 15 minutes, alerts near key levels and setup triggers. |
| Server/UI | `server.py`, `index.html` | Localhost-only, allowlisted commands, read-only. |

## 2. Findings

Severity: critical = the page can mislead a trading decision; high = results not trustworthy or not
reproducible; medium = real weakness with a bounded effect; low = hygiene.

### Critical

- **C1. Five-minute scalping cannot be researched with the current data.** `gold_intraday.analyse` requests
  15m for 30 days and 60m for 6 months (lines near 108-109). Yahoo has no usable 5m or 1m history beyond a few
  weeks and returned HTTP 404 for `XAUUSD=X`. There is no honest way to backtest M5 strategies on this source.
  Fix: user-supplied broker history (MT5 export) or a broker API adapter (Phase 2).
- **C2. What the page shows is not what was backtested.** The backtest replays `compute_setups()["setups"]`
  on daily bars (`gold_backtest.py` main loop). The page shows `trade_plans()` rankings on 15m, 1h, 4h, daily and
  weekly (`gold.py` `trade_plans`). Its score weights (zone count, timeframe agreement, trend, reward,
  distance) are hand-set and have never been validated; the A/B/C grades are not probabilities. The
  intraday plans have no historical test at all. Fix: label as unvalidated now; replace with tested
  strategies and calibrated probabilities (Phases 4 to 6).
- **C3. The analysis engine had no automated tests.** All 13 files under `tests/` cover the skills. None
  imported `webui`. Fix: new tests in this change; more per phase.

### High

- **H1. Costs are ignored on the page and thin in the backtest.** Plan R multiples, sizing and targets are
  gross (`trade_plans`, `size_dict`). The backtest subtracts one flat cost per unit from the exit R
  (`simulate`, `out[key] = ((px - fill) * sgn - cost) / risk`). No spread, commission, slippage or swap model,
  and no cost sensitivity. Pullback limits are filled whenever price touches the level (queue position
  ignored) and breakouts fill at the next open with zero slippage, both optimistic. Fix: `risk.py` cost model
  and a cost-sensitivity table in the backtest (this change); net-of-cost plans (Phase 6).
- **H2. Data is not traceable or reproducible.** No provider name, fetch time, row count or hash is recorded;
  the only cache is an in-memory TTL dict (`gold_intraday.cached`). A result cannot be reproduced after
  Yahoo revises or drops a bar. Fix: `marketdata.py` provenance records, hashes and an on-disk store.
- **H3. Instrument mismatch.** `GC=F` is the continuous front-month COMEX future, not spot XAUUSD. Rolls
  appear as price gaps and the backtest treats them as real gaps. Prices differ from an MT5 or OANDA
  XAUUSD quote. Fix: surfaced on the page now; roll-aware or spot history in Phase 2.
- **H4. The forming bar is used as if it were complete.** `fetch_bars` returns the current incomplete bar,
  and `compute_setups` takes `last = bars[-1][3]` (line 221), so ATR, averages, swings and zones include it.
  `gold_intraday.resample` says the last 4h block may still be forming. The backtest only ever sees completed
  bars. Production and backtest therefore differ, and levels can shift while a bar develops (repaint risk).
  Fix: `marketdata.completed()` exists now; signals move to closed bars in Phase 3.
- **H5. Overfitting and multiple testing.** Zone width caps (0.6 and 1.2 ATR), stop offsets (0.5 ATR), swing
  width, the plan score weights and the A/B grade cut-offs were chosen while looking at the same history.
  There is no untouched holdout. The backtest is called walk-forward but it is a forward replay of fixed
  rules, not a parameter re-fit; the only stability check is two halves. Fix: Phase 6.
- **H6. News scoring is not evidence.** `gold_news.classify` is keyword matching; no release timestamps, no
  calendar, no consensus versus actual, and the pressure score (`aggregate`) has never been tested against
  forward returns. Fix: Phase 8.

### Medium

- **M1. Cross-asset alignment by calendar day.** `gold_news.series` keys bars by `t // 86400`, mixing session
  clocks of futures, the dollar index, yields and stocks. Correlations are rough.
- **M2. Sizing ignores contract specifications.** No lot step, minimum lot, tick value, margin or broker
  differences. Fix: `risk.py` contract table and sizing (this change); broker overrides in Phase 2.
- **M3. Watcher state is in memory** (`gold_watch.Watcher.state`), so a restart can repeat alerts. It alerts on
  the older `setups`, not on ranked plans. No stale-data, spread, exposure or loss-limit alerts. Fix: Phase 10.
- **M4. Unlocked caches.** Module-level `_INTRA` (gold.py) and `_cache` (gold_intraday.py) are shared across
  threads without a lock. Dict operations are safe in CPython but the TTL logic can fetch twice.
- **M5. The 95% band assumes independent trades**, but trades overlap in time (the report says so). Needs a
  block bootstrap (Phase 6).

### Low

- **L1.** 4h bars are fixed UTC blocks, not broker session aligned; no session labelling.
- **L2.** Yahoo is an unofficial API; an outage becomes a one-line error.
- **L3.** "52-week" statistics use bar counts, not dates.

### Verified good (do not regress)

- No look-ahead in the setup replay: setups are computed on `bars[:t + 1]` and fills start at bar `t + 1`
  (`gold_backtest.py` main loop and `simulate`).
- Same-bar ambiguity is handled conservatively: the stop is checked before the target, and a gap through a
  stop exits at the open (`exit_trade`).
- The server is localhost-only, checks Host and Origin, runs an allowlist of argv lists (no shell), stores
  no secrets, and never places orders.

## 3. Phased plan

| Phase | Scope | Status after this change |
| --- | --- | --- |
| 1 Audit | This document | Done |
| 2 Data architecture | Provider adapters, validation, provenance, store, CSV import with spreads | Built: Yahoo, OANDA (candles with bid and ask, needs your token), Dukascopy (free ticks with bid and ask, download needs `--yes`), CSV import, validation, hashes, store. MetaTrader 5 deliberately not integrated. Not exercised live: OANDA and Dukascopy. |
| 3 Regimes and structure | ADX/ATR/efficiency regimes, BOS/CHoCH, sweeps, FVG, order blocks, causal | Built (`regimes.py`, `indicators.py`), with non-repainting tests |
| 4 Strategies A to D | Breakout-retest, trend continuation, sweep reversal, range | Built (`strategies.py`), each independently configurable and gated |
| 5 Statistical and ML layer | Logistic and gradient boosting baselines, calibration | Built in pure Python (`ml.py`) with rejection rules. On real gold data it is rejected. |
| 6 Backtester | Event-driven, walk-forward, holdout, purging, stress tests, bootstrap, deflated Sharpe | Built (`backtester.py`) |
| 7 | Not present in the request (it jumps from 6 to 8) | Still missing: send the text if it matters |
| 8 Macro engine | CPI, NFP, FOMC timestamps, expectations versus actual, event-window tests | Built (`macro.py`): official FOMC, derived NFP, CSV for CPI and PCE, observed reactions, news-filter test |
| 9 Dashboard | Overview, trade assessment, risk monitor, performance, journal | Built: new Gold intelligence page |
| 10 Watcher | Setup validity, regime change, event, spread, stale-data, exposure alerts | Built (`watch_extras.py`), Bitcoin alerts unchanged by default |
| 11 Tests | Unit, integration, regression | 600+ tests; every new module has its own test file |
| 12 Delivery | Separate commits, no push without approval | Following this rule; nothing is pushed |

## 4. What I need from you before Phase 2 can finish

1. **A source of M5 history.** The practical option is an MT5 history export (CSV) for your broker's XAUUSD,
   plus the broker server's UTC offset and the symbol's point size. The importer will not guess the offset.
   Or an OANDA practice-account token, stored in an environment variable, never pasted in chat.
2. **Your broker's XAUUSD specification:** contract size per lot, minimum lot, lot step, typical and
   worst-case spread, commission per lot, swap.
3. **The text of Phase 7.**
4. **Your risk limits:** maximum open risk per account, daily loss limit, maximum open positions.

## 5. Status of the findings after this change

| Finding | Status |
| --- | --- |
| C1 no M5 history | Open in practice. Yahoo still cannot supply it. The Dukascopy and OANDA adapters and the CSV importer are ready; until you approve a download or supply a file, five-minute strategies cannot be tested. |
| C2 page is not what was backtested | Fixed for the new dashboard: it shows only the four tested strategies, each with its own validation record. The older Gold assessment page keeps its UNVALIDATED label. |
| C3 no engine tests | Fixed: every new module has tests, including causality, execution, statistics, gates and the HTTP guards. |
| H1 costs | Fixed in the new engine: spread, slippage and commission are in every fill, with 0x to 3x stress and a break-even cost. The older assessment page is still gross of costs. |
| H2 traceability | Fixed for research runs: dataset hash, seed, configuration and code version are stored with every result. |
| H3 instrument mismatch | Disclosed everywhere. Spot-quality history needs Dukascopy or OANDA data. |
| H4 forming bar | Fixed in the new engine: only closed bars are used. The older assessment page still uses the forming bar. |
| H5 overfitting | Addressed: walk-forward selection, embargo, an untouched holdout with a use-once log, deflated Sharpe and Holm correction. It cannot be fully removed with so little data. |
| H6 news scoring | Partly addressed: releases are timestamped with source and reliability and reactions are measured, but the keyword scores on the news page are still not validated. |
| M1 cross-asset alignment | Open. |
| M2 sizing and contract specs | Fixed in `risk.py` and used by the dashboard. |
| M3 watcher | Fixed: alert state is saved, new alert kinds are deduplicated and rate-limited. |
| M4 and M5, L1 to L3 | Open or unchanged. |

## 6. What the first data checks found (live Yahoo, 2026-11-16)

- GC=F 1h history has a 61-hour hole between 2026-07-10 and 2026-07-15 that no holiday explains, plus four
  shorter holiday-shaped gaps. It is 149 missing hourly bars in total (95% complete over six months). Any level
  built from that stretch is built from partial data.
- Yahoo stamps the newest bar with the quote time (for example 11:06:32) instead of the bar start, and that bar
  is still forming. The checker now says so instead of calling it an overlap.
- GC=F daily has 19 single missing weekdays that are most likely exchange holidays; they are counted separately.
- Daily and weekly bar stamps move by an hour at daylight-saving changes; the checker tolerates that.

## 7. First cost result (daily setups, exit at first target, 10 years of GC=F)

- Average before any cost: +0.07R with a 95% band of about plus or minus 0.21R, so it cannot be told from luck.
- Break-even round-trip cost: about $0.95 per ounce. A realistic spread plus slippage plus commission on an
  XAUUSD CFD can sit near that, so even the point estimate has almost no margin.
- On BTC-USD the same setups lose before any cost (-0.12R), and more after it.

## 8. First results from the new engine (live Yahoo data, 2026-11-16)

- GC=F 1 hour, 2853 closed bars (about six months). Each of the four strategies produced between 2 and 13 out-of-sample trades
  and 3 to 5 holdout trades. All four are `INSUFFICIENT SAMPLE`: nothing can be said for or against them yet.
- Machine learning (target before stop, costs included): rejected. Out-of-sample AUC was 0.44 to 0.47, below chance, and the
  selected trades did no better than taking every trade.
- Regime labels on gold 1 hour: sideways 46 percent, weak trend 17, strong downtrend 14, strong uptrend 12, major sweep reversal 8.
- Observed reactions of hourly gold: after FOMC statements the move over the first hour was about 8 times a normal hour (4 events),
  after derived NFP dates about 2 times (5 events). Both are far too few events to rely on.
- Result: the dashboard shows NO TRADE, as it should.

## 9. Rules this work follows

- Research results and live verified evidence stay separate, and simulated or sample data is labelled.
- No order is ever placed or modified. The system stays read-only and paper only.
- A strategy is not called profitable unless its out-of-sample net results support it.
- Missing data is reported, never interpolated.
