# Gold rebuild: pre-registered test design

Committed BEFORE the strategies were backtested. Rules, variants, windows and decision criteria are fixed here. A changed rule is a new hypothesis.

## Data, windows, costs

- GC=F H1 bars (about 6 months, delayed, last-trade) and H4 built from them in fixed 4-hour UTC blocks. Only CLOSED bars are used. A signal decided on the close
  of H1 bar i is filled at the OPEN of bar i+1 (market order), plus cost. No later bar is read when deciding.
- Windows by time order: DEVELOPMENT = first 80% of bars; within it three equal chronological folds F1, F2, F3 (descriptive consistency check; no parameter is fitted, so no
  fold is "trained on"). HOLDOUT = last 20%, opened exactly once, only for a variant adopted on DEVELOPMENT.
- Cost per unit = `assets.profile("GC=F")["default_cost"]`, the same flat cost the old engine used, charged once per round trip in price units and expressed in R of that trade's
  risk. Sensitivity runs at 0.5x, 1x and 2x of that cost (descriptive).
- R is measured in planned risk (entry to stop). Same-bar stop and target is resolved as stop first. A bar that gaps through the stop exits at its open. Time stop after 48 H1 bars.
- Unfinished trades at the end of data are counted as OPEN and excluded from averages.
- One independent opportunity at a time per market: while a trade of either strategy is open or a plan is pending, no new signal is taken; after a stop-out there is a cooldown of 6 H1 bars.

## Trend definition (objective, shared by A and B)

H4 uptrend: H4 close above its 50-bar simple average AND that average above its value 6 H4 bars earlier. H4 downtrend is the mirror. Anything else is NO TREND and neither strategy trades.
ATR is the 14-bar Wilder average true range on H1.

## Strategy A: H4/H1 trend continuation (3 hypotheses)

- A1 Donchian breakout, fixed exit. With an H4 uptrend, an H1 close above the highest high of the previous 20 H1 bars. Stop 2.0 ATR below the signal close. Target 2R. Shorts mirror.
- A2 Donchian breakout, channel exit. Same entry and stop as A1, no fixed target; exit on an H1 close below the lowest low of the previous 10 H1 bars (above the highest high for shorts), or the stop.
- A3 Structural pullback. With an H4 uptrend, in the last 3 H1 bars price traded at or below the H1 20-bar exponential average and the signal bar closes back above it with a bullish
  close (close above open). Stop 0.25 ATR below the lowest low of those 3 bars. Target 2R. Shorts mirror.

## Strategy B: trend-aligned structural rejection (2 hypotheses)

- Zones: H4 pivot highs and lows (3 bars each side) that were confirmed at least 6 H4 bars before the signal. A zone is the pivot price plus and minus 0.25 ATR.
- Setup: long only at a pivot-low zone in an H4 uptrend (short only at a pivot-high zone in a downtrend). The signal H1 bar must have its low inside the zone, close above the zone top for
  longs (below the zone bottom for shorts), and show a rejection wick of at least 50% of the bar range with the close in the better 40% of the range.
- Stop: beyond the zone by 0.5 ATR and beyond the signal bar extreme. Skip if the planned risk is below 1.0 ATR or above 4.0 ATR.
- Opposing structure: skip if the nearest opposing confirmed pivot zone lies within 1.5R of the entry.
- B1: target 2R. B2: target at the nearest opposing confirmed pivot zone edge, taken only when it is between 1.5R and 4R away.

## Strategy C (VWAP and EMA continuation)

Research only. Not tested here: this data has no volume. It cannot produce a signal.

## Decision rule

A variant is ADOPTED on DEVELOPMENT only if: at least 100 filled trades; mean net R above 0 with the multiple-testing corrected bootstrap lower bound above 0; mean net R positive in at
least 2 of the 3 folds; and mean net R above the old engine's on the same window. Otherwise REJECTED, or INSUFFICIENT SAMPLE if fewer than 100 fills. The holdout is then opened once:
a variant stays eligible only if the holdout mean net R is above 0 on at least 20 fills. A variant with no holdout evidence is not eligible.
Multiple testing: 29 earlier ideas plus 5 here gives N = 34, corrected level 0.05 / 34.

## Old engine benchmark

The archived engine's unique opportunities (`replay_opportunities.json`, grading engine 2, same cost per unit) restricted to the same DEVELOPMENT and HOLDOUT time windows, same R unit, same split
cut timestamps. Reported beside A and B. Both systems are compared only on identical dates.

## What is not tested

M5 and M15 entry triggers, spread, broker slippage, pending-order fills and a session edge (sessions are reported as descriptive tables; enabling a session needs its own 100 fills).
Nothing here is tuned on the holdout. If nothing is adopted, the console says NO TRADE and no Gold plan is issued.
