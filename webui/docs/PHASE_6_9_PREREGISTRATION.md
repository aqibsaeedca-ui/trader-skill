# Phases 6 to 9: pre-registered test design

Written and committed BEFORE any of the experiments below were run. Same discipline as `PHASE_3_5_PREREGISTRATION.md`: rules, grids,
split and decision criteria are fixed here, each hypothesis is evaluated once on the TEST split, rejected ones stay in the log, and a
changed rule becomes a new hypothesis.

## Data, split and decision rule (unchanged from Phases 3 to 5)

- Population: unique opportunities from the historical replay (first recommendation of each cluster), grading engine 2.
- Split: first 60% TRAIN (descriptive, nothing is fitted), last 40% TEST (decides). Same cut as Phases 3 to 5.
- Entries are held identical across variants: every variant uses the fill price and fill bar that the baseline rule produced. Only the stop
  or the exit rule changes. A variant may leave a trade open that the baseline closed (or the reverse); open trades are excluded from the
  averages and counted.
- R for a variant is measured in that variant's OWN initial risk (entry to its own stop), costs are charged in R of that risk. This is the
  standard fixed-risk comparison: position size adapts to the stop distance.
- A variant is ADOPTED only if on TEST: at least 100 filled opportunities; mean net R above 0 with the Bonferroni corrected lower bound of
  the bootstrap band above 0; mean net R higher than the baseline's; and TRAIN mean net R not worse than the baseline's. Fewer than 100:
  INSUFFICIENT SAMPLE. Otherwise REJECTED.
- Multiple testing: this work uses the same data as Phases 3 to 5, so the correction counts every hypothesis tested so far. 12 earlier
  plus 13 below gives N = 25, corrected level 0.05 / 25.

## Phase 6: stops and exits (population: opportunities that filled, both plan kinds pooled)

Baseline: the plan's own stop with the plan's exit (50 / 30 / 20 split at targets 1, 2, 3, stop moved to the entry after target 1).

Stop variants (predeclared, limited grid, plan exit unchanged, targets stay at their price levels):
- S1, S2, S3: ATR stops at 1.0, 1.5 and 2.0 times the ATR of the plan's chart at the recommendation, measured from the entry.
- S4: swing stop: beyond the most recent H1 swing low (long) or swing high (short) by 0.25 H1 ATR, only when that swing lies on the correct side
  of the entry; otherwise the plan stop.

Exit variants (plan stop, entry unchanged):
- X1: fixed reward to risk: the whole position exits at 1.5R (stop at the plan stop).
- X2: structural target: the whole position exits at target 1.
- X3: ATR target: the whole position exits at 2.0 ATR from the entry.
- X4: partial exits with NO breakeven move (50 / 30 / 20, stop stays at the plan stop).
- X5: trailing stop: no fixed target; after the trade is 1R in profit the stop trails 1.5 ATR behind the best price seen on earlier bars.
- X6: time exit: close at the close of the 12th bar after entry, no target.
- X7: regime-invalidated exit: plan exit, plus exit at the close of the first bar where the H1 bias has turned against the trade.

Same-bar rules are unchanged: the stop is assumed first; a gap exits at the open; a limit order's fill bar gives no target credit.

Descriptive (not hypotheses, not decision-bearing): distribution of maximum adverse excursion for winners and losers in the baseline, and
results by trading session.

Hypotheses: 4 stops plus 7 exits = 11.

## Phase 7: grading

- H7: the plan's existing grade score (the hand-weighted confluence score) predicts net R. Spearman rank correlation between score and net R on
  TEST fills; adopted only if the correlation is positive and a 5000-draw permutation test gives p below the corrected level. 1 hypothesis.
- The evidence-based grade is a policy, not a fitted model. For each strategy (plan kind and side) its TEST record is classified:
  - A: passes the adoption criteria above for that strategy on its own;
  - NO TRADE: at least 100 test fills and the corrected band for mean net R lies entirely below zero;
  - B: at least 30 test fills, mean net R not statistically negative, evidence below the deployment threshold;
  - C: fewer than 30 test fills, or anything else (unvalidated).
  Probabilities are shown only where a calibrated model exists. None passed earlier acceptance, so the probability field reads INSUFFICIENT DATA.

## Phase 8: duplicated and overlapping signals

Opportunity clustering is the one already built (same market, side and kind, entry zone within half an hourly ATR, overlapping validity).
Accounting comparison on TEST, no hypothesis: (i) every recommendation taken as its own trade versus (ii) only the first recommendation of
each opportunity. Reported: trades, total cost in R, total net R, maximum drawdown in R, peak concurrent open risk in R. This is mechanical
accounting; it says nothing about edge. No new hypothesis.

## Phase 9: strict mode versus baseline mode

- Baseline mode: the original strategy, every opportunity as it is today.
- Strict mode: only opportunities whose strategy has evidence grade A.
- H9: strict mode beats baseline mode. Successful only if on TEST strict mode has at least 100 trades AND (mean net R above the baseline's with the
  corrected band above zero, OR maximum drawdown at least 30% lower at a mean net R not below the baseline's). Zero or few trades is NOT
  success: it is capital preservation, reported as NOT DEMONSTRATED, never as better selection. 1 hypothesis.
- Strict mode is a setting stored locally (default off, so the baseline behaves as today). The engine may produce no trades for long periods.

Total hypotheses in this document: 11 + 1 + 1 = 13.

## Clarifications (added before any experiment was run)

- H9 must not select strategies on the data it is judged on. For H9 only, the evidence grade of each strategy is computed from the TRAIN split
  alone (A needs at least 100 train fills, a corrected band above zero on TRAIN, and a positive TRAIN mean), and strict mode is then judged on
  TEST. The grade shown on the page uses the TEST record as described in Phase 7, because that is the best current evidence, but it is never used
  to judge itself.
- "A passes the adoption criteria for that strategy on its own" (Phase 7) means: at least 100 TEST fills, corrected lower band above zero, and a
  positive TRAIN mean. There is no baseline comparison because the strategy is its own subject.
- The stop-variant skip rule: if a stop variant would put the stop on the wrong side of the baseline fill price, that opportunity is skipped for
  that variant and the skips are counted and shown.
- Phase 8 "every recommendation as its own trade" uses all filled recommendations of the replay; "first of each opportunity" uses the opportunity
  representatives. Both use grading engine 2.