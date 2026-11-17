# Phases 10 to 12: pre-registered test design

Written and committed BEFORE the experiments below were run. Same discipline as the Phase 3 to 5 and Phase 6 to 9 documents: rules, features,
split and decision criteria are fixed here, each hypothesis is evaluated once on the TEST split, rejected ones stay in the log, and a changed
rule becomes a new hypothesis.

## Data, split and decision rule (unchanged)

- Population: unique opportunities from the historical replay (first recommendation of each cluster), grading engine 2, baseline entry and
  exit rules. Result of a filled opportunity = the baseline's net R in planned risk.
- Split: first 60% TRAIN, last 40% TEST (same cut as Phases 3 to 9). TEST decides.
- A variant is ADOPTED only if on TEST: at least 100 filled opportunities kept; mean net R above 0 with the multiple-testing corrected
  bootstrap lower bound above 0; mean net R higher than the baseline's; TRAIN mean net R not worse than the baseline's. Fewer than 100:
  INSUFFICIENT SAMPLE. Otherwise REJECTED.
- Multiple testing: same data again, so every earlier hypothesis counts. 25 earlier plus 4 below gives N = 29, corrected level 0.05 / 29.

## Phase 10: session and news filters (3 hypotheses)

All filters use only what was known when the recommendation was made (its creation time).

- H10a: skip opportunities created within 60 minutes (before or after the event time) of a high-importance scheduled macro event in the stored
  calendar. (Same 3600 s window as the Phase 3 news flag.)
- H10b: skip opportunities created in the Asia session (00:00 to 07:00 UTC).
- H10c: only opportunities created in the London or New York sessions (07:00 to 21:00 UTC); the 21:00 to 24:00 UTC hours are skipped as well.

Session boundaries are the fixed UTC hours already used by the console (`regimes.SESSIONS`). They are not tuned. Results by session remain a
descriptive table, not a hypothesis. Daylight saving shifts of the real market opens are ignored; this is a stated limitation.
Not tested here: a news filter based on actual surprise or spread widening (no such data), and liquidity from real tick volume (none).

## Phase 11: machine-learning trade filter (1 hypothesis plus diagnostics)

- Model: L2-regularised logistic regression, standard library only, fixed settings (lambda 1.0, learning rate 0.1, 400 full-batch steps,
  features standardised with TRAIN means and standard deviations, missing values replaced by the TRAIN mean). No other model family is tried,
  because every extra model would be another hypothesis on the same data.
- Target: net R above 0 on a filled opportunity.
- Features (all from closed bars at the recommendation time): kind is a breakout; slope of the H1 EMA in ATR times the trade direction;
  ADX; efficiency ratio; volatility ratio; H1 bias agrees with the trade (+1, -1, 0); H4 bias agrees (+1, -1, 0); the old confluence score;
  distance to the entry in ATR; number of agreeing timeframes; planned risk in ATR of the plan chart; Asia, London, New York session flags;
  high-impact news within 60 minutes.
- Training uses TRAIN fills only, with a purge and an embargo: any TRAIN fill whose exit time is later than 24 hours before the split cut is
  dropped, so no training outcome overlaps the test period.
- H11 decision: the filter keeps an opportunity when its predicted probability is at or above the median of the predictions over the TRAIN
  fills (threshold fixed on TRAIN, never on TEST). Kept TEST opportunities are judged by the standard decision rule above.
- Diagnostics (descriptive, not hypotheses): TEST AUC, TEST Brier score against the Brier score of always predicting the TRAIN win rate
  (Brier skill), and a purged expanding-window walk-forward inside TRAIN (three folds, same purge) with its AUCs.
- Probabilities are shown to the user only if H11 is ADOPTED and the Brier skill is positive. Otherwise they stay INSUFFICIENT DATA.

## Phase 12: event-driven backtester (engineering check, not a hypothesis)

- An order-lifecycle engine processes bar-close events from every symbol and timeframe in global time order. Each order moves through the
  statuses CREATED, PENDING (waiting for the entry), TRIGGERED (breakout confirmed, waits for the next open), FILLED, PARTIALLY CLOSED (target 1
  reached, stop at the entry), then one final status: CLOSED WIN, CLOSED LOSS, TIMED OUT, EXPIRED, INVALIDATED, or OPEN (not finished when the
  data ends).
- Acceptance test: re-simulate every recorded replay recommendation with this engine and compare to the scorecard grader. Required: identical
  final state and net R (absolute difference below 1e-9) for at least 99.9% of recommendations. Every disagreement is listed.
- Descriptive risk-gate replay on the TEST opportunities, chronological, labelled historical backtest: each filled trade risks 0.25% of equity
  (1R); gates are at most 3 open positions, aggregate open planned risk at most 1%, at most one open position per market and side, and a
  daily lockout once realised losses that UTC day reach 1.5%. It reports what the gates would have skipped and the resulting total and
  maximum drawdown. It is arithmetic on the recorded outcomes, not a hypothesis, and it does not make a negative average positive.

## What would not count as evidence

A filter that looks good on TRAIN only, a result inside the corrected band's noise, or a filter that works only by taking fewer than 100 trades.
