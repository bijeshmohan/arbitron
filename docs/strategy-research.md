# Strategy research

How candidate strategies are judged before any of them gets near live money.
The method and the pass criteria below were **written before any backtest was
run**, and must not be edited after results are seen. Results go in the log at
the bottom, pass or fail.

Nothing here is evidence of future returns. A backtest is a simulation on
history, and passing it only earns a candidate a forward paper test.

## What is being tested

| Strategy | Idea | Parameters tried |
|----------|------|------------------|
| `buy_hold` | baseline: buy once, never sell | none |
| `trend_filter` | hold while close > SMA(length) | length: 50, 100, 200, 400 |
| `breakout` | buy a close above the prior `entry`-candle high, sell below the prior `exit`-candle low | entry: 24, 48, 96, 168; exit: 12, 24, 48 (all 12 combinations) |
| `mean_reversion` | buy RSI(length) < oversold, sell RSI > exit_level | length: 7, 14; oversold: 20, 30; exit_level: 50, 60 |

That is 4 + 12 + 8 = **24 parameter sets** plus the baseline. Each result is
one draw: with enough draws one looks good by luck, so the grids are small and
fixed here. Adding more variants after seeing results means the grid count in
the log must go up, and the pass bar is reread in that light.

All strategies are `allowed_in_live = False` and stay that way until a human
review says otherwise.

## Setup (fixed)

- **Market:** BTC/USDT on Binance spot, **1-hour candles**.
- **Data:** 2024-11-14 to 2026-11-14 (UTC, end exclusive), from `market backfill`.
- **In-sample (tuning):** 2024-11-14 to 2025-11-14.
- **Out-of-sample (judging):** 2025-11-14 to 2026-11-14. Looked at **once**,
  for the one parameter set selected in-sample per strategy.
- **Costs:** the backtest defaults: fee 0.1% per fill, slippage 5 bps, spread
  1 bp, fills at the next candle's open.
- **Capital:** 1000 USDT start, a fixed 800 USDT per entry
  (`configs/example.research.toml`), so 80% of the account is invested while a
  position is held. Returns and drawdowns are on the whole 1000 USDT account,
  so they read about 20% smaller than a fully invested position would show.
  The fixed entry size can't exceed the account balance, so a run that falls
  below about 800 USDT can't enter again. That is a drawdown of about 20%,
  which already fails criterion 2. Risk limits are loose so the kill switch
  doesn't halt a run. `buy_hold` uses the same entry size, so the comparison
  is like for like.

## Selection rule (in-sample only)

For each strategy, among its parameter sets with at least 30 round trips and
a max drawdown under 20% **in-sample**, take the one with the highest
in-sample total return. If none qualify, the strategy fails without an
out-of-sample look. Out-of-sample columns of other parameter sets are not used
to choose.

## Pass criteria (out-of-sample, all required)

For the selected parameter set, over the out-of-sample year:

1. **Beats the baseline after costs:** total return above the `buy_hold`
   strategy's total return over the same year, run in the same engine.
2. **Worst loss under 20%:** `max_drawdown_pct` below 20.
3. **Enough trades to mean something:** at least 30 round trips.
4. **Not a one-trade story:** removing the single best round trip leaves a
   positive total return.

Failing any one fails the candidate. A candidate that passes is **not
approved**. It goes to a forward paper test of at least 4 weeks on the paper
stack, judged against the same criteria without retuning. Beating buy-and-hold
in a falling market is the weakest kind of pass: report the market's direction
over both periods next to the results.

## Honest expectations

Round trips cost about 0.2% in fees plus slippage, and most simple rules fail
to beat buy-and-hold after costs. "No candidate passes" is a likely, valid
result, and the correct response is to stay on paper trading or keep
researching, not to loosen the criteria.

## Round 2: wider test (pre-registered 2026-11-14)

Written after round 1 and **before any round 2 backtest was run**. Round 1
found nothing, on one asset in a falling year. This round asks the same
question across several market regimes, two assets and two timeframes.

**Contamination, stated up front.** I (the agent) and the operator have seen
round 1's results, which covered BTC/USDT from 2024-11-14 to 2026-11-14, and
both of us know roughly how crypto prices moved in earlier years. No
out-of-sample period is perfectly clean. The 2025 fold below overlaps the
period round 1 looked at, so it is the weakest evidence.

### Setup (fixed)

- **Assets:** BTC/USDT and ETH/USDT. **Timeframes:** 4h and 1d. 1h is dropped
  (fees dominated it in round 1).
- **Data:** 2019-01-01 to 2026-11-14 (end exclusive), from `market backfill`.
- **Strategies and grids:** the same 24 parameter sets per (asset, timeframe)
  as round 1, in candles of that timeframe. Plus the `buy_hold` baseline.
  That is 24 sets x 2 assets x 2 timeframes = **96 new parameter sets**, on
  top of round 1's 24: **120 trials in total**.
- **Costs and sizing:** unchanged (fee 0.1%, slippage 5 bps, 1000 USDT
  account, 800 USDT per entry).
- **Walk-forward folds.** Four out-of-sample years: 2022, 2023, 2024, 2025.
  For each, parameters are selected on all data from 2019-01-01 up to the
  start of that year (expanding window), by the round 1 selection rule, and
  the fold's year is then judged once. 2022 was a bear year, 2023 a recovery,
  2024 a bull year, 2025 mixed (and partly seen).
- **Selection rule per fold:** among a strategy's parameter sets with at
  least 20 round trips (40 fills) in-sample and in-sample max drawdown under
  30%, the highest in-sample total return. If none qualify, that strategy
  has no entry for the fold and counts as flat (0% return, 0 trades). The
  looser thresholds than round 1 reflect that daily candles trade rarely.

### Pass criteria

A **line** is one (strategy, asset, timeframe), judged over the four folds
together. All of these are required:

1. **Makes money outright:** the sum of the four out-of-sample fold returns is
   positive. Losing less than the market is not enough.
2. **Beats the baseline, or is a clearly better ride.** Either its summed
   return exceeds `buy_hold`'s over the same four folds, or it earns at least
   half of `buy_hold`'s summed return while its worst fold drawdown is under
   half of `buy_hold`'s worst fold drawdown.
3. **Bounded loss:** max drawdown under 25% in every fold.
4. **Enough trades:** at least 20 round trips in total across the folds.
5. **Consistent:** positive in at least 2 of the 4 folds.
6. **Not luck on one asset:** the same (strategy, timeframe) line passes
   criteria 1 to 5 on **both** BTC/USDT and ETH/USDT. The selected
   parameters may differ.

A candidate that passes everything is **not approved**. It goes to a forward
paper test of at least 4 weeks, without retuning, and then to the operator's
decisions. With 120 trials, one or two apparent passes can be luck: criterion
6 exists to cut that, and it does not remove it.

### Reporting

For each line and fold: selected parameters, in-sample return, out-of-sample
return, drawdown, round trips, and the `buy_hold` return for that fold. All
fold results are reported, pass or fail, in the log.

## Log

| When | What | Result |
|------|------|--------|
| 2026-11-14 | Harness check before the real run: a first `trend_filter` sweep used 990 USDT per entry, which can't be repeated after the first loss (4,600 `insufficient_balance` rejections). Changed to 800 USDT before any real result was read. The broken sweep's table, out-of-sample columns included, was seen: all four lengths showed 2 trades and about -1% to -2% in both periods, so it said nothing about the strategy. | setup fixed above; no parameter or criterion was changed |
| 2026-11-14 | Data: 17,520 hourly BTC/USDT candles, 2024-11-14 to 2026-11-14, no gaps. In-sample sweeps (24 parameter sets). Selection by the rule above (30 round trips = 60 fills, in-sample drawdown under 20%): `trend_filter` length=200 (the only qualifying set; in-sample -19.7%, drawdown 19.7%), `breakout` entry=168 exit=12 (-2.2%), `mean_reversion` length=14 oversold=30 exit_level=60 (+1.6%). Market in-sample: buy-and-hold net of fees +10.1%. | all three were selected before their out-of-sample numbers were read |
| 2026-11-14 | **Out-of-sample year, one look per selected set** (BTC fell about 31% over this year, net of fees). Results on the 1000 USDT account, 800 USDT per entry: `buy_hold` -24.8% (drawdown 33.2%); `trend_filter` -19.9%, drawdown 22.5%, 37 round trips; `breakout` -19.5%, drawdown 19.5%, **24 round trips**; `mean_reversion` -31.0%, drawdown 34.1%, 34 round trips. | **No candidate passes.** `trend_filter` fails criterion 2 (drawdown over 20%). `breakout` fails criterion 3 (24 round trips, under 30). `mean_reversion` fails criterion 1 (worse than the baseline) and 2. Criterion 4 wasn't needed. |

## Reading the result

- **Nothing here is a tradable edge.** In-sample, only 4 of the 24 parameter
  sets finished positive (+0.3% to +2.3%, in a year when buy-and-hold made
  10%). The one of those that was selected, `mean_reversion`, lost 31%
  out-of-sample. After fees (58.97 USDT for the trend filter over
  one year, about 6% of the account) there was nothing left.
- `trend_filter` and `breakout` lost *less* than buy-and-hold only because the
  market fell and they spent more than 90% of the year in cash. That is a weak
  kind of win: it says "being out of a falling market helps", which no one
  needed a backtest for. In the rising in-sample year, the same rules lagged.
- `breakout` came closest (a smaller loss than the baseline, drawdown under
  20%), but it missed the trade-count criterion, and a 24-trade, -19.5% year
  is not a result to build on. The criterion stays as written.
- Both sizing quirks mattered: after a loss the fixed 800 USDT entry gets
  rejected for `insufficient_balance`/`balance_reserve` in the worst runs
  (2,433 times for `trend_filter`). This is the known cost of a fixed
  notional and it flatters the drawdown slightly, because the strategy stops
  trading when it is losing.
- Only one asset, one timeframe and one year of out-of-sample data were
  tested. A different year would give different numbers.

## Where this leaves strategy selection

No strategy is approved, and the live-readiness review's item 2 stays a
blocker. Options, in the order I would take them:

1. Stay on paper trading with no strategy approved, and revisit later.
2. Widen the research honestly: a second asset (ETH/USDT), a longer history
   (Binance has data from 2017, so several market regimes), and 4-hour or
   daily candles. Any new variants count as new trials, and the grid count in
   this document goes up.
3. Accept that a simple rule may not beat buy-and-hold on spot after costs,
   and decide whether the project's goal is still a live strategy.
| 2026-11-14 | **Round 2 run as pre-registered.** Data: BTC/USDT and ETH/USDT, 4h and 1d candles, 2019-01-01 to 2026-11-14 (4h has 5 missing candles from exchange maintenance, e.g. 2020-02-19). 96 new parameter sets per fold set, 120 trials in total. 64 jobs: 48 walk-forward sweeps and 16 `buy_hold` runs. | see round 2 results below |

## Round 2 results

Summed over the four out-of-sample years 2022 to 2025 (account return, 800 USDT
per entry; a year with no qualifying parameter set counts as flat). `buy_hold`
summed: BTC +163.6% (4h) / +154.0% (1d), ETH +46.3% (4h) / +41.2% (1d).

| Line | Sum OOS | Worst-fold drawdown | Round trips | Positive folds | Criteria 1-5 failed |
|------|---------|---------------------|-------------|----------------|---------------------|
| trend_filter BTC 4h | +53.5% | 27.4% | 176 | 2/4 | 2 (below baseline), 3 (drawdown over 25%) |
| trend_filter ETH 4h | +62.2% | 26.6% | 149 | 3/4 | 3 (drawdown over 25%) |
| trend_filter BTC 1d | +108.2% | 29.5% | 25 | 3/4 | 2, 3 |
| trend_filter ETH 1d | 0 (flat) | 0 | 0 | 0/4 | 1, 2, 4, 5 |
| breakout BTC 4h | +75.0% | 33.6% | 24 | 3/4 | 2, 3 |
| breakout ETH 4h | +45.8% | 36.7% | 33 | 3/4 | 2 (just below the baseline's +46.3%), 3 |
| breakout BTC 1d | -16.0% | 23.9% | 5 | 0/4 | 1, 2, 4, 5 |
| breakout ETH 1d | 0 (flat) | 0 | 0 | 0/4 | 1, 2, 4, 5 |
| mean_reversion BTC 4h | +36.7% | 9.1% | 9 | 2/4 | 2, 4 |
| mean_reversion ETH 4h | 0 (flat) | 0 | 0 | 0/4 | 1, 2, 4, 5 |
| mean_reversion BTC 1d | 0 (flat) | 0 | 0 | 0/4 | 1, 2, 4, 5 |
| mean_reversion ETH 1d | +14.5% | 15.5% | 4 | 1/4 | 2, 4, 5 |

**Criterion 6 (same strategy and timeframe passes on both assets): no
strategy and timeframe passes on both. No candidate passes.** Only
`trend_filter` ETH 4h passed criteria 1, 2, 4 and 5 on its own, and it failed
criterion 3 by 1.6 points of drawdown (26.6% against a 25% limit). Its BTC
twin failed on 2 and 3. The criteria stay as written.

Per-fold detail (selected parameters, in-sample and out-of-sample returns,
buy-and-hold) is in the analysis output; the pattern is:

- **2022 (bear):** `trend_filter` ETH 4h made +12.5% while buy-and-hold lost
  54%. Its BTC twin lost 22%, and breakout lost 22% to 27%. Staying out
  helps in a bear year only if the rule actually exits in time.
- **2023 and 2024 (recovery and bull):** every trend and breakout line that
  traded made money, but on BTC earned roughly a third to two-thirds of
  buy-and-hold (for example +58% against +123% in 2023).
- **2025 (mixed, partly seen):** results ranged from +0.7% to +14.5%, except
  `trend_filter` 4h (about -21% on both assets) and `breakout` BTC 1d (-16%);
  buy-and-hold lost 5% to 9%.
- The 1d lines mostly sat out: with 2019-2021 as the only in-sample history
  for the 2022 fold, few parameter sets reached 20 round trips, so several
  folds were flat. This is a limit of the pre-registered rule, not a result
  about daily candles. A different rule would count as a new trial.

## Where this leaves things (end of round 2)

The honest summary: **simple trend and breakout rules on 4-hour candles were
profitable in most years, but they give up a large part of the upside, and
they have drawdowns of 25% to 37% in the bad years.** No line meets the
pre-registered bar of beating or closely matching buy-and-hold on both assets
with a bounded loss. That is consistent with the common finding that simple
rules mostly reduce risk, they don't create returns.

Further variants are available, but each is another trial, and each time the
bar should be read in the light of the growing trial count (now 120). The
nearest candidate, `trend_filter` on ETH 4h, would be the natural subject of
a different kind of test (a forward paper run with the criteria fixed in
advance), not of more parameter searching.

