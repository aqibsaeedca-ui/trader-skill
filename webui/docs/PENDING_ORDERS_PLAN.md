# Pending Order Intelligence: audit and plan

## 1. What already exists (checked in the code)

| Need | Where it is today |
| --- | --- |
| Structure, regimes, levels, sweeps | `regimes.py` (causal, closed bars only) |
| Strategy framework with market, limit and stop entries, expiry | `strategies.py` `Strategy.signal`, `backtester.py` `_simulate` |
| Fill model | `backtester._simulate`: limit needs penetration, stop entry fills at the worse of open and level plus slippage, stop first in an ambiguous bar, gaps exit at the open |
| Walk-forward, holdout, cost stress, gate | `backtester.walk_forward`, `verdict`, `research.py` |
| Costs, lot sizing, exposure, loss lockout | `risk.py` |
| User-entered positions and pending orders | `portfolio.py` (read-only monitor) |
| Alerts with dedupe, cooldown, hourly cap | `watch_extras.py`, `gold_watch.py` |
| Bid and ask | Only from stored Dukascopy or OANDA data. Yahoo has none. |
| MetaTrader 5 connection | None. `PROVIDERS["mt5"]` is a placeholder that refuses, and you asked earlier that MT5 not be integrated. |

Gaps this work fills: no pending-order types, no entry-price engine, no broker rule checks, no order-level statistics
(fill rate, expiry, cancellation), no paper-order lifecycle, no account-wide scenario risk across several resting orders.

## 2. What is possible without MT5, and what is not

Built as a broker rules model plus a paper simulator, because nothing may be sent to a broker:

- **Order semantics and constraints** (`orders.py`): BUY STOP above the Ask, SELL STOP below the Bid, BUY LIMIT below the Ask,
  SELL LIMIT above the Bid, each at least the broker's stop level away; stop loss and take profit at least the stop level
  from the order price; prices on the tick size; lots on the lot step.
- **Hedging versus netting** is a profile setting. One MT5 position carries one take-profit, so three targets are planned as
  three separate orders (hedging accounts, only if the lots allow it) or as a client-side partial-exit plan (marked as
  needing a controller). It is never assumed that one position holds three independent take-profits.
- **OCO**: MT5 does not cancel a sibling order by itself. It exists only in the paper simulator. A live version would need a
  documented client-side controller, and the dashboard says so.
- **Stop-limit orders** (BUY STOP LIMIT, SELL STOP LIMIT) are two-price orders: a stop trigger, then a limit order that may never fill.
  They are modelled for validation only, are OFF until the broker profile says the account supports them, and are not backtested.

Not possible here, and shown as unavailable rather than guessed: reading real equity or positions from MT5, reading the
broker's real stop level, freeze level, commission and swap, and confirming stop-limit support. These come from the
broker profile you enter. Equity comes from `trader/portfolio.json`; if it has never been saved, recommendations are blocked.

## 3. Plan

1. `orders.py`: broker profile, order validation, tick rounding, execution plan, multi-target modes, account-wide risk.
2. `pending_strategies.py`: BUY STOP, SELL STOP, BUY LIMIT, SELL LIMIT as four independent, gated strategies with regime rules and small calibration grids.
3. `backtester.py`: order statistics (submitted, filled, expired, cancelled, entry slippage) with uncertainty bands; stop orders cancel on invalidation.
4. `research.py`: a pending-order family validated on its own record, never mixed with the existing four strategies.
5. `paper.py`: paper orders with a full lifecycle, one-cancels-the-other, structural invalidation, an audit log.
6. `pending.py` and server routes: the Pending Order Intelligence document and paper-order endpoints.
7. Dashboard section with four panels, a comparison table, and the NO VALID PENDING ORDER state.
8. Watcher: state tracking and alerts with dedupe, cooldowns and an audit trail.

## 4. Rules

- A pending-order strategy is shown as recommendable only after it passes the same out-of-sample gate as the other strategies.
- Fill probability and win probability after a fill are reported separately, each with an uncertainty band, and only when
  there are enough historical orders. Otherwise the page says there is not enough data.
- Zero recommendations is a normal answer.
- Nothing here places, changes or cancels a real order.
