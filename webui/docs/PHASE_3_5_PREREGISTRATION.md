# Phases 3 to 5: pre-registered test design

Written and committed BEFORE any of the experiments below were run. The rules, thresholds, split and decision criteria are fixed here.
Nothing may be changed after seeing results. If a rule is changed later it becomes a new, separately logged hypothesis, and the
number of hypotheses (which sets the multiple-testing correction) goes up.

## Data and unit of analysis

- Population: the historical replay of the Gold and Bitcoin recommendations (grading engine 2, see `scorecard.py`), about 6 months.
- Unit: one UNIQUE OPPORTUNITY. Repeated recommendations of the same idea are clustered (`loss_attribution.clusters`); the first
  recommendation of each cluster represents it. Results are never reported per repeated recommendation.
- Split: opportunities are ordered by recommendation time. The first 60% are TRAIN (descriptive only; no rule is fitted on them).
  The last 40% are TEST and carry the decision. Every hypothesis is evaluated on TEST once.
- Costs: the profile's round-trip cost per unit, charged on every filled trade, in R.
- Metrics: trades, mean net R per filled trade, mean net R per recommended opportunity (a missed trade counts as 0), fill rate,
  profit factor, maximum drawdown in R, bootstrap 95% band for mean net R (opportunities resampled).
- Not available from this data, so NOT tested: spread expansion, bid/ask, distance to opposing liquidity beyond the swing levels, M15
  momentum for periods older than the 30 day 15 minute history.

## Decision rule (identical for every hypothesis)

A variant is ADOPTED only if ALL hold on TEST:
1. at least 100 unique opportunities that filled;
2. mean net R is above 0 and the Bonferroni-corrected lower bound of the bootstrap band is above 0
   (corrected for the total number of hypotheses N below);
3. mean net R is higher than the baseline's on the same split;
4. its TRAIN mean net R is not worse than the baseline's TRAIN mean net R (the direction is consistent).

Fewer than 100 test fills: INSUFFICIENT SAMPLE. Otherwise REJECTED. A rejected variant is not re-tuned. If no breakout variant is adopted,
breakout plans stay disabled for strict mode.

Total hypotheses N = 12: regime ablation (1), phase 4 variants B, C, D (3), phase 5 variants stop order, retest, and six filters (8).

## Phase 3: regime classifier (version 2) and ablation

Nine classes from completed candles only: strong bullish trend, strong bearish trend, weak bullish trend, weak bearish trend,
sideways range, volatility compression, volatility expansion, high-impact news instability, uncertain or transitional.

Rules, evaluated on the H1 snapshot at the last closed bar, with the H4 snapshot as context (all fixed now):
- news instability: a high-impact release within 60 minutes either side, or the engine's post-news flag;
- volatility expansion: vol_ratio at least 1.3 and not a strong trend; compression: vol_ratio at most 0.7 and ADX below 20;
- strong trend: ADX at least 25, efficiency ratio at least 0.30, H1 slope sign agrees with the H1 bias, and the H4 bias agrees;
- weak trend: ADX at least 18 and efficiency ratio at least 0.20 with the H1 bias agreeing in direction;
- sideways range: ADX below 18 and bias range;
- transitional: anything where the H1 and H4 biases disagree or the features are missing.

Ablation: for each feature group (H4 bias, H1 bias, slope, ADX and efficiency ratio, volatility ratio, news) the group is neutralised, labels
are recomputed, and the TEST eta squared (share of net-R variance explained by the labels) is compared with the full classifier and with
the 95th percentile of 500 label permutations. The classifier "has signal" only if the full TEST eta squared exceeds that percentile.
A feature group "adds value" only if the classifier has signal and removing the group lowers TEST eta squared.

## Phase 4: trend filter and reversal filter (pullback plans only)

- A: no filter (baseline).
- B: higher-timeframe trend filter. LONG pullback allowed only if H4 bias is bullish AND H1 bias is bullish AND the H1 trend is a bullish
  trend (strong or weak) AND the last closed candle on the plan's chart is bullish AND the entry is not within 1.0 ATR below the H1 swing high
  AND blended net reward (the plan's blended R minus cost in R) is at least 1.0. SHORT pullback: the mirror image.
- C: confirmed reversal, for COUNTER-trend plans only (H1 bias opposite to the plan side): allowed only if a liquidity sweep in the plan's
  direction occurred within the last 12 H1 bars AND a change of character occurred within the last 12 H1 bars AND the H1 bias now agrees
  with the plan side AND blended net reward is at least 1.0. The pullback limit itself is the retest.
- D: combined: allowed if B passes (with-trend plans) or C passes (counter-trend plans).
Countertrend trades are not banned unless these tests say so.

## Phase 5: breakout and breakdown (breakout plans only)

Entry modes, tested on the same opportunities:
- A (baseline): wait for a bar to close beyond the level, enter at the next open (current rule).
- Stop order: enter when price trades beyond the level, at the worse of the open and the level plus one tick.
- Retest: after the confirming close, wait up to 6 bars for a bar that trades back to within 0.15 ATR of the level and closes on the
  breakout side, then enter at the next open. Cancelled if a bar closes beyond the stop first, or the first target is reached first.
Filters, each applied alone to mode A: vol expansion (H1 vol_ratio at least 1.2); trend alignment (plan labelled with-trend); breakout
distance (confirming close at least 0.2 ATR beyond the level); candle quality (body at least half of the confirming bar's range); no recent
adverse sweep (no sweep against the trade in the last 6 H1 bars); no news (no high-impact release within 60 minutes either side).

## Reporting

Every hypothesis, its parameters, its TRAIN and TEST results and its decision are written to `trader/experiment_log.json`, including the
rejected ones. Rejected variants stay in the log.
