# Gold intelligence engine: technical guide

This engine is a research and risk-management tool. It is read-only: it never places, changes or cancels an order,
and it holds no broker credentials. A passing test suite shows the software behaves as designed. It says nothing about
whether any strategy makes money.

## 1. Map of the code (all in `webui/`)

| File | Job |
| --- | --- |
| `marketdata.py` | Data checks (duplicates, gaps, bad candles, stale data, forming bar), provenance hashes, on-disk store, CSV importer, Yahoo, OANDA and Dukascopy adapters |
| `indicators.py` | Causal ATR, ADX, efficiency ratio, slope, realised volatility, range z-score |
| `regimes.py` | Regime classification, market structure (swings, BOS, CHoCH, sweeps, fair-value gaps, order blocks, previous day and week levels, sessions), higher-timeframe context |
| `strategies.py` | Four strategies: breakout and retest, trend continuation, liquidity sweep reversal, range |
| `backtester.py` | Execution model, walk-forward, holdout lock, statistics, validation gate, run records |
| `ml.py` | Pure-Python logistic regression and boosted trees, calibration, causal features, chronological splits, acceptance rules |
| `macro.py` | Economic calendar, observed event reactions, news-filter test |
| `risk.py` | Contract specifications, cost model, sizing, exposure, risk limits |
| `portfolio.py` | Read-only monitor over positions and pending orders you type in |
| `journal.py` | Trading journal with signal IDs and planned versus realised risk |
| `research.py` | Runs everything and writes `trader/validation.json` |
| `intelligence.py` | Builds the dashboard document with the NO TRADE gates |
| `watch_extras.py`, `gold_watch.py` | Alerts, per asset |
| `server.py`, `index.html` | Local web console (127.0.0.1 only) |

Runtime files go in `trader/` (ignored by git): `validation.json`, `models/`, `holdout_log.json`, `portfolio.json`,
`journal.json`, `events/events.json`, `marketdata/`, and the watcher's settings, alert log and state files.

## 2. No look-ahead, no repainting

- Every indicator value at bar `i` uses only bars up to `i`. Tests recompute on a prefix and compare.
- A structure event (swing, BOS, CHoCH, sweep, gap, order block) is recorded on the bar where its confirmation
  condition is met and never changes afterwards. A swing is confirmed `k` bars after it forms.
- Strategies see one closed bar at a time. Signals are decided at the close of bar `i` and executed from bar `i + 1`.
- Higher-timeframe context only exposes a block after it has fully closed.
- Live signals use closed bars only. The still-forming bar is dropped.
- Scheduled release times are known in advance, so using them is not look-ahead.

## 3. Execution model

- Market orders: next open, plus half the spread, plus slippage against you.
- Limit orders: price must trade through the level by a penetration margin. A touch is not a fill.
- Stop entries: the worse of the open and the level, plus slippage.
- If a stop and a target are both reachable in one bar, the stop is assumed first.
- A stop that gaps exits at the open.
- Last-trade bars stand in for bid and ask: longs are stopped on the low and sold on the high, shorts are stopped when the
  high plus the spread reaches the stop and covered when the low plus the spread reaches the target.
- After the first target the stop moves to entry (switchable). Targets split 50, 30 and 20 percent.
- One position at a time per strategy; optional daily loss limit in R.

## 4. Validation gate

A strategy can only be called `VALIDATED (research, past data only)` if all of these hold on out-of-sample data:

1. at least 100 walk-forward test trades (fewer is reported as `INSUFFICIENT SAMPLE`);
2. the block-bootstrap 95 percent band for net expectancy is above zero;
3. net expectancy is positive, and still positive when spread and slippage are doubled;
4. the deflated Sharpe ratio is at least 0.95 after counting every parameter set and strategy tried;
5. both halves of the out-of-sample record are positive;
6. an untouched final holdout (last 20 percent of bars) has at least 30 trades, a positive result, and was looked at once.

Folds are chronological with an embargo equal to the holding period, so no trade label overlaps a test window.
The holdout log (`trader/holdout_log.json`) records every parameter set that was tried on a holdout segment; a second
set marks that holdout as contaminated and the gate fails. The dashboard also treats validation as stale if the code
has changed since it was produced.

A model (`ml.py`) is accepted only if it beats a constant-probability baseline in log loss, has an AUC band above 0.5,
is calibrated (error at most 0.08), and its selected trades have a positive net expectancy whose band excludes zero and
that beats taking every trade. A rejected model is saved for the record but can never produce a probability.

## 5. NO TRADE

The dashboard shows LONG or SHORT only if the strategy is validated on the current code, it has a confirmed trigger on
the last closed bar, the plan passes your risk limits after costs, and (if an accepted model exists) its calibrated
probability clears the threshold chosen on validation data. Every refusal lists its reasons. Probability, expected return
and out-of-sample results are shown only when a real validated source produced them, otherwise the page says why not.

## 6. Running it

```
python webui/server.py                                     # the console at http://127.0.0.1:8765
python webui/research.py run --symbol GC=F --interval 60m  # also a button: Gold research run
python webui/macro.py update                               # official FOMC dates + derived NFP dates
python webui/macro.py import-csv my_events.csv             # CPI, PCE and anything else (datetime_utc,name,type,importance,actual,forecast,previous,source)
python webui/marketdata.py check --symbol GC=F --interval 15m
python -m unittest discover -s tests
```

## 7. Data, and what is missing

- Yahoo (default): delayed last-trade bars, no bid or ask, no volume. 15 minute history is about 30 days and 1 hour about
  6 months, so five-minute scalping cannot be tested on it. `GC=F` is continuous front-month futures, not spot XAUUSD.
  Each research run merges what it fetched into `trader/marketdata/`, so history grows with use.
- Dukascopy (free ticks with real bid and ask): `python webui/marketdata.py dukascopy --symbol XAUUSD --from 2025-01-01 --to 2025-07-01 --interval 5m`
  prints how many files that means and downloads nothing until you add `--yes`. Then `research.py run --symbol XAUUSD --interval 5m --source dukascopy`
  uses the measured spread for its costs.
- OANDA: set `OANDA_TOKEN` (and `OANDA_ENV=practice` or `live`) as environment variables, then `marketdata.py oanda ...`. Only the
  candles endpoint is ever called. The token is never printed, logged or stored.
- A CSV export from any platform can be validated with `marketdata.py check-csv` (you must state the server clock's UTC offset).
- MetaTrader 5 is deliberately not integrated.
- CPI and PCE release dates must come from your own CSV: the BLS site refuses automated downloads and this tool does not work around that.
  NFP dates are derived from the BLS rule and marked `derived`; check them, especially around government shutdowns.

## 8. Costs

Backtest costs default to placeholders (gold: spread 0.30 and slippage 0.10 per side in dollars per ounce) and are labelled as
placeholders everywhere they appear. Replace them with your broker's numbers in the research form, or let the run measure them
from stored bid and ask data.

## 9. Known limits

- Out-of-sample evidence so far is thin: the available gold history gives each strategy a handful of out-of-sample trades.
- Event studies have a handful of events per type, flagged as too few to rely on.
- The news keyword scores are not validated against later price moves.
- Holiday gaps in intraday data cannot be told apart from outages without a holiday calendar.
- Positions and orders are typed in by you; nothing is read from a broker.
