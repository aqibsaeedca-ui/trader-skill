# Gold Intelligence Engine 4.0: audit, research register, data limits and roadmap

Stage 1 and Stage 2 output, written 2026-11-18. Nothing here is a profitability claim. Status of each item is stated as measured by us, reported by others, or unverified.

## 1. Repository findings

What already exists and is reusable (all standard library, read only, tests pass: 909):

| Need in the new brief | What exists | Gap |
| --- | --- | --- |
| Loss attribution (brief Phase 1) | `loss_attribution.py`, page "Loss attribution report": evidence-only causes, clusters, simulator audit | Per-strategy and per-session cuts for the NEW strategy library |
| Unique opportunities (Phase 6) | Opportunity clustering in replay (`loss_attribution.clusters`) | Live engine still emits overlapping M5/M15/H1 plans; no cooldowns, no 2-loss lockout |
| Metrics, bootstrap, expectancy (Phase 7) | `experiments.metrics`, Wilson bands, Bonferroni, profit factor, drawdown | Sharpe/Sortino, parameter stability, cost sensitivity, random-entry control |
| Walk-forward without overfitting (Phase 8) | Pre-registration docs, 60/40 split, test decides, experiment log (29 ideas) | Final untouched holdout, purged walk-forward for rule strategies, random-entry baseline |
| Exits (Phase 10) | `experiments2.simulate_exit`: plan, no-breakeven, single target, trail, time, regime | Spread-aware stops, broker min-stop and volume rejection |
| Risk gates (Phase 11) | `event_backtest.gate_replay`: 0.25% / 3 open / 1% / one per market-side / 1.5% lockout, replayed on history | Not wired to live MT5 (belongs to `mt5-trader`) |
| Dashboard (Phase 12) | `profit` page sections A to H, Gold lab group | Session analytics, strategy research page, no-trade explanations |
| Backtester | `event_backtest.py` order lifecycle, reconciled 100% with the grader | Broker-aware costs (spread by hour, slippage, swap) |
| Sessions | `regimes.SESSIONS`: fixed UTC hours (asia 0-7, london 7-13, ny 13-21) | Not DST-aware, no session ranges, no VWAP, no event windows |

Findings that matter for this brief:

1. **No IANA time zones on this machine.** Python 3.9 `zoneinfo` fails for Asia/Tokyo, Europe/London, America/New_York and Asia/Karachi because the `tzdata` package is not installed (Windows ships no zone database). The brief forbids hardcoded UTC offsets. Options: install the `tzdata` package (a download, needs your approval) or implement the UK and US daylight-saving rules as code and test them against known transition dates. Both avoid fixed offsets; the first is the IANA source.
2. **Data is too thin for session research.** Available from Yahoo `GC=F` (delayed, futures, last-trade): 5-minute and 15-minute bars for only the last 30 days; 1-hour for 6 months; daily for 10 years. No bid or ask, no spread, no tick volume, and GC=F is not executable XAUUSD. Thirty days is about 20 trading days, which is roughly 20 Asian ranges and 20 London opens per strategy: far below any defensible sample. The independent reports below needed 22 years of 1-minute data or 2 years of broker 1-minute data.
3. **Nothing has been executed on a broker.** All results are historical backtest or a 9-trade live record.

## 2. Current Gold loss diagnosis (from our own measured results)

Source: replay of every recommendation, Gold and Bitcoin, 6 months, delayed bars, flat costs; held-out test = last 40%.

- The unfiltered engine averaged -0.16R per trade on the test period (652 trades, corrected band -0.31R to -0.01R, profit factor 0.75, worst drawdown 114R): statistically losing.
- Counting each idea once, the whole replay averages -0.13R (2,038 completed). Gold alone: -0.06R over 3,316 completed (descriptive, not separately pre-registered).
- Duplication: 17,990 recommendations are only 3,578 independent opportunities; costs and drawdown were inflated by repeats. This confirms the repeated-short pattern in your 17 November log.
- By session of creation (existing plans, descriptive): Asia -0.19R, London -0.10R, New York -0.09R, off-hours -0.34R.
- Countertrend label did not explain losses (with-trend -0.13R vs counter -0.16R). So "fading strong trends" is a hypothesis the data did not support.
- Supported by recorded evidence (overlapping, not proven causes): duplicated idea, insufficient confirmation, excessively tight stop (stop inside normal bar noise: a stop tighter than about 0.8R would have stopped out a quarter of eventual winners).
- Nine-trade log: 9 completed live outcomes, 1 win and 8 losses under the corrected grader; far too few to conclude anything.
- Of 31 variants tried (filters, stops, exits, entry modes, regime classifier, session and news filters, logistic model), none was adopted. Strict mode (A-grade only) currently allows nothing.
- Data quality and "possible execution-price inconsistencies": confirmed and fixed earlier (engine 1 measured R in actual fill risk and treated unfinished positions as wins; engine 2 uses planned risk and OPEN state).

Not explained yet and not testable with current data: spread and slippage effects, broker fill behaviour, session-range structure, VWAP.

## 3. Strategy research register

Evidence levels: A = our own backtest; B = adaptation for XAUUSD; C = independent evidence; D = original documented method; E = actual MT5 account. Verification column states what I actually read.

| # | Source | What it is | Verified by me | Rules and limits as read | Independent evidence for gold |
| --- | --- | --- | --- | --- | --- |
| R1 | Moskowitz, Ooi, Pedersen (2012), JFE, doi 10.1016/j.jfineco.2011.11.003 | Peer-reviewed time-series momentum across many futures | **Not verified**: ScienceDirect returned a bot-check page and I did not bypass it. Description is from background knowledge only | Daily/monthly horizons (roughly 1 to 12 month lookback), diversified portfolio, volatility scaling. Not an intraday gold rule | Portfolio-level evidence only; says nothing about a single-gold intraday edge |
| R2 | Iwatsubo, Watkins, Xu (2018), J. Commodity Markets 11:59-71 (RIETI 17-E-120) | Intraday seasonality in efficiency, liquidity, volatility, volume, platinum and gold futures, Tokyo and NY | **Verified abstract** (RIETI page) | Tokyo day session dominated by uninformed trading; informed trading dominates the New York day session. Describes microstructure, gives no trading rule | Supports using sessions as an explanatory variable; does not show a profitable rule |
| R3 | Original Turtle rules, turtletrader.com | Dennis and Eckhardt trend following, taught 1983 | **Verified page** (partial read, first part): scientific-method framing, trend following, rules not judgments | 20-day and 55-day channel breakouts, ATR-based sizing (details further down the page, not read in full by me). Daily horizon; the "$100 million" figure is the site's claim, unaudited by me | Daily-horizon benchmark only; not an M5 rule |
| R4 | Toby Crabel, "Opening Range Breakout: A Century of Evidence" (Substack, 1 Sep 2026) | Working-draft book, paid, 84 futures markets, 1923-2025 | **Verified first section** (paywalled) | Baseline: breakout at 0.8 x the ten-day range from the open, **no stop**, held to the next day's open, daily data | Not a gold intraday rule and uses no stops; our brief forbids stop-free live execution. Working draft, unreviewed |
| R5 | lindaraschke.net | Practitioner site (Holy Grail, Turtle Soup are from her books) | **Verified homepage only**: biography and resource links. It does not give strategy rules | Rules must come from her books (Street Smarts, with Connors). Not read by me | None provided there |
| R6 | "Intraday return predictability: commodity ETFs and volatility indices", PMC7480318 | Peer-reviewed (PMC) | **Verified abstract** | Intraday momentum exists in crude oil, gold and silver ETFs; the predictive half-hour differs by market; stronger on high-volatility and jump days; ETFs, not spot XAUUSD | Supportive but ETF-based; economic value claim not reproduced by us |
| R7 | ripbacktest.com, Gold opening-range breakout | Independent XAUUSD broker-data test, 22 years, 1-minute | **Verified** | 30-minute opening range at Asia, London, New York open, 12 variants: +4,774R before costs, -2,003R after costs over 52,616 trades; 0 of 12 passed every gate; best +0.03R per trade, interval includes zero. Self-published, not peer reviewed | Negative after costs |
| R8 | rezsync.com, London sweep of the Asian range | Independent broker-data test, XAUUSD and 3 FX pairs, 2 years, 1-minute with real spreads | **Verified** | Fading the London sweep lost -0.126R (535 trades) and -0.164R (532 trades) per trade, worse than random sides; no market positive. Self-published | Negative after costs: Strategy 3 starts as an unvalidated, probably failing hypothesis |
| R9 | SSRN 6650958, gold VWAP regime-filtered preprint | Preprint | **Not verified**: SSRN returned a bot-check page and I did not bypass it | Unknown to me; any reported performance must be treated as unverified | Unknown |

Honest reading: the two independent intraday gold tests I could read (R7, R8) both found no net edge after costs, and R7 shows gross edge of about 0.07 to 0.18R per trade eaten by costs of 0.09 to 0.17R. The peer-reviewed and practitioner sources that are verified describe daily-horizon trend following or microstructure, not a gold intraday rule that is shown to pay. This does not prove our own tests will fail, but it sets a low prior and makes spread and cost modelling decisive.

## 4. Session-specific strategy research plan

Hypotheses to test, each pre-registered before running, one per strategy and session pair, judged once on a held-out period, corrected for all earlier ideas (currently N = 29, rising). Priority is by independent evidence and by data feasibility.

| Priority | Strategy (brief number) | Session | Prior from the register | Data it needs | Can start now? |
| --- | --- | --- | --- | --- | --- |
| 1 | S7 Turtle / Donchian 20 and 55 day, daily benchmark (and TSMM-style momentum) | Daily | Documented, daily horizon | GC=F daily (10 years available) | **Yes, daily only** (labelled futures, not XAUUSD) |
| 2 | S2 London open, Asian range breakout (stop and confirmed-retest) | London | R7 negative net | 1-minute or 5-minute XAUUSD with spread, 2+ years | No |
| 3 | S4 New York opening range (08:20, 08:30, 09:30 NY references, 15 and 30 minutes) | NY | R7 negative net; R6 mildly supportive | Same | No |
| 4 | S8 VWAP plus EMA regime filter (combinations A to E) | All | R9 unverified | Volume (tick volume from broker, labelled) | No |
| 5 | S6 Holy Grail pullback (ADX above 30, EMA20) | All | None verified | M5/M15/H1 with 2+ years | Only crudely on 1h 6 months, too small |
| 6 | S9 London-New York overlap momentum | Overlap | R2, R6 background | Same | No |
| 7 | S1 Asian range mean reversion | Asia | None verified; R2 says uninformed trading in Tokyo | Same | No |
| 8 | S3 Turtle Soup / sweep fade | London | R8 negative | Same | No |
| 9 | S5 post-news continuation | NY | None; policy default is block | Point-in-time releases and 1-minute prices | No |

Method for every strategy (from brief Phase 8): frozen hypothesis, final untouched holdout, chronological training windows, purge and embargo where labels overlap, broker-aware costs, neighbouring-parameter stability, multiple-testing correction, comparison with simple baseline and with random entry timing under identical exits and exposure, every failed experiment retained in `trader/experiment_log.json`.

Decision rule stays as before: at least 100 test fills, mean net R above 0 with the corrected lower bound above 0, beats baseline and random-entry control, train not worse. If nothing passes, the report says so and execution stays disabled.

## 5. Data requirements and limitations

Required for credible session research:
1. XAUUSD **1-minute bars with bid, ask or spread**, at least 2 full years, ideally 5 or more. Sources: (a) the MT5 broker's own history through the separate `mt5-trader` project once a terminal is installed, which gives broker-specific spreads and is what you would actually trade; (b) a public tick archive such as Dukascopy, which gives bid and ask but is not your broker's price. A download needs your explicit approval (file name, source and size to be stated first).
2. Scheduled-event timestamps with point-in-time actuals for NFP, CPI, PCE and FOMC (the console stores forecast events; actual values and revisions are not yet stored point-in-time).
3. Contract-roll and broker-closure calendars if GC=F is used.
4. An IANA time-zone database (`tzdata` package) or equivalent tested DST rules.

Limitations that stay even with that data: broker-specific spread and slippage; tick volume is broker activity, not exchange volume (VWAP built on it must be labelled so); GC=F is a different instrument; holdout can be used only once; a strategy that passes could still fail forward.

Current feasible work with no new data: the daily Turtle/Donchian and momentum benchmark on 10 years of GC=F daily bars.

## 6. Prioritised implementation roadmap

| Stage | Work | Needs from you | Status |
| --- | --- | --- | --- |
| 1 | Audit and loss diagnosis | none | **Done (this document)** |
| 2 | Research register | none | **Done, with 2 sources unverified (captcha) and 2 only partly read** |
| 3 | Timezone-correct session engine: sessions, DST, ranges, VWAP where volume exists, event windows | Approve `pip install tzdata`, or accept rule-coded DST | Next |
| 4 | Strategy modules S1 to S9 as pure, tested functions | none to write; data to run | After 3 |
| 5 | Broker-aware backtester (spread by hour, slippage, commission, swap, min stop and volume) | XAUUSD 1-minute data with spread | Blocked on data |
| 6 | Session-by-session research, pre-registered | Same data | Blocked on data |
| 7 | Regime-to-strategy selector and NO TRADE path | none | After 6 results exist |
| 8 | Live opportunity de-duplication, cooldowns, 2-loss lockout | none for the console part; MT5 account exposure belongs in `mt5-trader` | Can be done on the console side |
| 9 | Dashboard extension (session analytics, strategy research, no-trade explanations) | none | After 3 and 7 |
| 10 | Paper mode, then MT5 demo | MT5 terminal, demo credentials as environment variables, your explicit approval | Blocked; live stays disabled |

Constraints that stay in force: no martingale, 0.25% per trade, 1% aggregate, 1.5% daily lockout, no real-money execution, live disabled, nothing pushed without your approval.
