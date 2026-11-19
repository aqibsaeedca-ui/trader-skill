# Gold console rebuild: audit (2026-11-19)

Written before any rebuild code. Facts are from our own measured results unless marked otherwise. Nothing is deleted; "disable" means removed from the
ACTIVE Gold signal path, "archive" means kept, runnable and listed in `trader/archive/README.md`.

## 1. Why the current Gold signals lose (measured)

| Finding | Evidence |
| --- | --- |
| The engine loses after costs | Test period (last 40%): -0.16R per trade over 652 independent trades, corrected band -0.31R to -0.01R, profit factor 0.75. Whole replay -0.13R over 2,038 independent completed trades. |
| Repeated entries inflate everything | 17,990 recommendations are 3,578 independent opportunities. Counting each idea once cuts peak simultaneous risk from 171R to 23R and drawdown from 845R to 114R on the test period. |
| Grades A, B, C are poorly calibrated | Test averages: grade A about 0.00R, B -0.32R, C -0.35R. The score ranks plans in the right order (Spearman +0.125) but even the best grade only breaks even after costs. |
| Countertrend is not the explanation | With-trend -0.13R vs counter -0.16R. "Countertrend" is therefore a hypothesis the data did not support, not a proven cause. |
| Stops sit inside normal noise | A stop tighter than about 0.8R would have stopped out a quarter of the eventual winners. Duplicated idea (93% of losses), insufficient confirmation (88%) and tight stop (51%) are the causes with recorded evidence. They overlap. |
| Execution assumptions were unrealistic, then corrected | Engine 1 measured R in actual fill risk and counted unfinished trades as wins; engine 2 fixed that. Still unrealistic: GC=F last-trade delayed bars (no bid or ask), flat cost per unit, a limit fills on one tick of penetration, same-bar stop and target resolved as stop first. |
| Nothing tested has fixed it | 34 ideas tried so far on this data (filters, stops, exits, entry modes, regime classifier, session and news filters, a logistic model); none adopted. Strict mode (grade A only) allows nothing. |
| Live record is tiny | 9 completed live outcomes, 1 win and 8 losses. |
| MT5 | Not connected to this console (separate project). All sizes use user-entered broker values. |

## 2. What is retained, simplified, disabled, archived

RETAIN (unchanged, used by the new engine)
- `gold_sessions.py` (timezone-correct sessions), `macro.py` event store, `orders.py` (broker rules, sizing, account risk), `risk.py`, `portfolio.py`,
  `marketdata.py` freshness checks, `scorecard.py` grading engine 2, `event_backtest.py`, `experiments*.py` statistics, `command_centre.py` gate framework.
- Bitcoin analysis, watcher, alerts, news, backtest and every Bitcoin test. Not touched.

SIMPLIFY
- ONE active Gold decision: `webui/gold_core.py` produces at most one independent Gold opportunity from two families only (A and B below), on H4 and H1.
- ONE primary Gold page: the Command Centre, reduced to market, session, news, three key levels, strategy, trade plan and one "Why this trade?" explanation.
- Data separation: GC=F stays a labelled research feed. Plans carry no executable price until a fresh broker bid/ask exists.

DISABLE in the active Gold signal path (code and tests stay)
- The multi-zone pullback and breakout plan ranking from `gold_intraday` with grades A, B, C as a source of Gold plans.
- `pending_strategies` order families as a Gold signal source.
- Regime-classifier, strict-mode, session and news filters, entry-mode and exit variants: all were tested and none adopted; they are not active.
- Gold watcher alerts keep running as alerts, but they no longer feed the Command Centre candidate.

ARCHIVE (kept, marked research only)
- Original Gold engine and its replay results as the BENCHMARK: `experiment_result.json`, `experiment2_result.json`, `experiment3_result.json`, `recommendation_backtest.json`,
  `replay_opportunities.json`, `experiment_log.json`. Snapshot copies go to `trader/archive/` with a README. The originals stay where the pages read them.
- Strategy C (VWAP and EMA continuation): research only, cannot generate an executable signal. This data has no volume, so it cannot even be tested.
- Strategies A to F of the earlier register that are not A or B here.

## 3. Constraints that remain

Signal only / paper mode. Risk 0.25% per setup, 1% aggregate, 1.5% daily lockout, no martingale, no averaging down, no repeated hedging. No live order
and no change to any existing position without separate explicit approval. Nothing is committed or pushed without approval.

## 4. Honest limits of the validation that is possible today

- 6 months of GC=F H1 (and H4 built from it), 10 years of daily, 30 days of M15 and M5. M5 and M15 triggers therefore CANNOT be validated historically; they are shown
  as context only and are never part of a validated rule.
- No bid or ask, no tick volume, no broker spread, so spread and slippage are tested as cost sensitivity, not measured.
- A final untouched holdout is reserved inside the 6 months (the last 20%), opened once. The live forward record afterwards is the real holdout.
