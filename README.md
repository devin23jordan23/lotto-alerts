# Lotto Alerts

Selective intraday potential watches and active options ideas using Schwab, Railway and Discord.
No orders are placed. Scores are research rules, not probabilities or promised returns.

## Model V profiles

`LOTTO_PROFILE` selects the line. Unset matches `conservative`. The live book is v4c. `loose` is aggressive (70), `moderate` is conservative (92), and `balanced` is 84. The additive score is the only post decision unless a gate is turned on. `LOTTO_GATE_ALIGNMENT` and `LOTTO_GATE_S12_BAR` default off. `LOTTO_ALIGNMENT_PACK=1` turns on the proposed alignment penalties (SPY -25, SPY level break -10, QQQ -10, sector -20, both -15, s12 -1 per point capped at -30, plus the location, stock-volume, and flat/slow rows). A set per-term variable still wins. The pack does not turn the hard gates on. Hybrid bars at 40/61 and the handoff floors stay on the card as labels. There is no daily cap or top-N. One posted card per ticker and side is held for 8 minutes (`LOTTO_COOLDOWN_MINUTES`; `0` turns it off). `LOTTO_COOLDOWN_RESET_ON_LEVEL_BREAK=1` clears that hold after a fresh level break in the card's direction. Bypass cards clear `bypass_threshold` and the blockers only.

| Profile | Post | Bypass | Min positive | Bolt features |
|---|---|---|---|---|
| aggressive (`loose`) | 70 | 40 | 0 | 10 |
| middle | 76 | 40 | 0 | 10 |
| balanced | 84 | 40 | 0 | 10 |
| conservative (`moderate`, default) | 92 | 40 | 0 | 10 |

### volume_trend

`volume_trend_aggressive`, `volume_trend_balanced`, and `volume_trend_conservative` are code profiles. `volume_trend` is the balanced one. They are not in the JSON book, so the live `balanced` line stays 84 until `LOTTO_PROFILE` changes. **Do not merge this branch to main.** Merging deploys it.

The three profiles share one weight block. Only the post line differs: 75, 102, and 120.

Primary cards only for the new sector and SPY points:

| Row | Points | When |
|---|---|---|
| `SPY_ALIGN_TURN_3M` | 15 | SPY return over `SPY_ALIGN_TURN_WINDOW_MIN` (3.5, interpolated) is 0.05%–0.12% in the card's direction |
| `SPY_ALIGN_RALLY_DAY` | 18 / 18 / 0 | 60–75% / 75–90% / ≥90% of sector-map names green (red for puts) |
| `SECTOR_ETF_PCT_TS` | 16 | Chosen sector ETF % from the open is 1.5–2.5× its 20-day same-minute typical move, then × `SECTOR_ETF_WEIGHT` |
| `SPY_ALIGN_OPEN` | 1 | SPY on the card's side of the open and of VWAP |
| `SECTOR_L2_PMH`, `SECTOR_ETF_PCT` | 0 | Alternates, still computed |
| `SECTOR_PEER_COUNT` | 33 | Exactly 3 peers move with the card |
| `BASKET_*` | 0 | Basket rows. The basket lists are in `SECTOR_BASKETS` |

`SPY_ALIGN_TURN_WINDOW_MIN` is a float. 3.5 minutes is a 210-second lookback. The reference price is the linear interpolate between the two 1-minute closes that bracket that time. Set the key to 3 for a whole-minute window.

The fitted volume rows are 3 to 11 points: `RVOL_CUM_15_25` is 7, `RVOL_CUM_ge25` is 11, `BU_5_TOD_10` is 4, and the break-bar rows are 4, 3, and 4. `vol_bin_flags` turns the measured ratios into those 0/1 flags. A missing ratio is 0.

`SECTOR_ETF_PCT_TS` uses a same-minute typical built at startup from the existing candle history: the median absolute percent from the open over the last 20 sessions, fewer days when that is all the history, otherwise the ETF's ATR-scaled curve. The low group (`XLI`, `XLU`, `XLB`, `XLP`, `XLRE`) uses `SECTOR_ETF_WEIGHT_LOW` 0.5. A name with `SMH`, `SOXX`, or `XOP` keeps the ETF whose sector points are higher after that multiplier. `QQQ`, `SPY`, and `IWM` have no sector ETF, so their sector rows are 0 and the SPY rows still score.

Ace's volume names are the scored keys (`BRK_VS_DAY`, `BRK_TOD_10/5/3`, `RVOL_CUM`, `SUST_VOL_TOD_1M`, `HOLD_ON_VOL`, and the rest in `lotto/volume_trend.py`). `VOL_*` names are aliases and stay at weight 0. `VOL_RVOL_TOD` names `RVOL_CUM_15_25` and `RVOL_CUM_ge25`. `VOL_PRE_BREAK` names `BU_5_TOD_10`. `VOL_BREAK_BAR` names `BRK_TOD_BRK_08_12`, `BRK_TOD_BRK_12_3`, and `BRK_TOD_BRK_ge3`. An alias adds no points. `RVOL_CHAIN_ge1.5` (+6) and the 1.0–1.5 dead zone (−9) score the same `RVOL_CUM` chain. `BRK_VS_10_ge1.5` (+15) stays the break bar versus the prior 10 bars of today. The backtested book has `BU_5_DAY_ge1` at 0. All three volume-trend profiles keep that 0 so the row does not double-count `BU_5_TOD_10`. The live JSON profiles, including balanced, are unchanged and do not carry the row.

`STACKS` in that same file is the list to edit. Each entry has a name, conditions (`feature`, `op`, `threshold`), and points. Every entry whose conditions all hold adds its points. Missing features are false. `OPT_VOL_ADJ_STRIKES` stays coded. The A+ stack and the 3-of-4 partial are still in the list, and both default to 0 points (`STACK_BONUS` and `STACK_BONUS_3OF4`). Held-out data did not support a stack bonus: the 3-of-4 A+ stack was +2.3pp in the fit and −6.7pp on the test. Nothing in a stack blocks a card. The old-book volume rows are unchanged. The new `VOL_*` rows sit on top of them. Cutoffs are 75 / 102 / 120.

Penalty rows are points only. They do not gate. Spread is `(ask−bid)/mid`, on the same edges as the wide-spread stock bars. A band's points are halved when that band's strong-stock bar is met. `PENALTY_OPT_VOL` uses the 5-minute contract print. It is 0 when the stock bar that waives a thin print is met.

| Row | Points | When |
|---|---|---|
| `PENALTY_SPREAD` | 0 / −10 / −12 / −15 | ≤25% / 25–35% / 35–40% / >40% of mid. Halved when that band's stock bar is met |
| `PENALTY_OPT_VOL` | −8 / 0 | 5-minute contract volume < 50, and 0 when the strong-stock bar is met |

The posted volume-trend score keeps these two rows. Model V's `option_extras` spread penalty stays on the old `score_v2` label and is not added again. The weight book does not carry a second spread or option-volume penalty. `sp5` is still the tight-spread bonus.

The chain is 10-day cumulative, then 5, then 3, then any same-time days, then the bar versus the day so far, then a linear time-scaled full-day volume, then 1.0. The source is logged. Early bars (index under 10) extend the day-so-far average back to 04:00. Holds score only after `HOLD_W` bars exist past the break. Puts mirror.

`EXTRA_UNIVERSE` in `lotto/volume_trend.py` is the full ranked list. Startup loads only the first `EXTRA_UNIVERSE_MAX` names that are not already in the universe and that return a chain. The default is **20**: OXY, EOSE, RDDT, APP, APLD, DAL, CVS, GILD, TXN, WULF, AAL, ONDS, WFC, NVTS, USAR, MP, SOUN, CLF, CCJ, TWLO. Names already in the core (MARA, RIVN, RIOT, DKNG, HIMS, LUNR, STX, CRWV, U, GME, CORZ, LITE, HUT) are skipped and do not count. Edit the list or `EXTRA_UNIVERSE_MAX` to add or swap later. A set `EXTRA_UNIVERSE_MAX` env value overrides the constant. This does not add deep slots. `LOTTO_CHAIN_CAPACITY` stays 60 and no rate limit changes.

Those 20 add **0 calls per minute**. They join the one `/quotes` batch (103 core + 20 = 123, under the 180 quote batch). A chain outside a deep slot comes only from the existing sweep, which stays at 26 per cycle, so one of these names takes a turn another name would have used. Deep jobs stay at 60. `RUNNER_RANK` only reorders who fills leftover core slots. Startup, once, is 2 calls per accepted name (1 chain probe + 1 history backfill), so 20 names are 40 calls at process start, not per minute.

The steady poll on `main` (`1b730db`, `LOTTO_PROFILE=balanced`) already attempts the same 148, because the profile does not touch the poll:

| Call | Count |
|---|---|
| `GET /quotes` | 1 |
| Sweep `GET /chains` | 26 |
| Deep `GET /pricehistory` | 60 |
| Deep `GET /chains` | 60 |
| SPX proxy `GET /pricehistory` for SPY | 1 |

**1 + 26 + 60 + 60 + 1 = 148** per 60-second cycle when every job finishes. `strikeCount` 80 or 40 is the same call count. The 120 figure (`published_schwab_market_data_per_minute` in `scan_budget`, and `PAID_MARKET_DATA_PER_MINUTE`) is a constant used for the margin math. It does not stop a request. `Schwab.get` spaces starts at 0.25s (about 240/min) and doubles that spacing on a 429, up to 2s. Unfinished sweep jobs drop after 27s and unfinished deep jobs after the rest of 54s. The margin math still says usable calls are 96/min (20% under 120), which would fit 34 deep slots. Configured deep stays 60, so extra deep slots stay 0.

## Universe and collection

The default universe includes the unusual-options scanner's core names (106 in the current deployed configuration).
Set `LOTTO_UNIVERSE` explicitly in Railway; CSCO and HPE are included even when
an older environment value overrides the code default. No case-study ticker list
replaces the configured universe.

Every cycle requests stock quotes for the **whole universe** in one batch.
The worker also rotates `/chains` requests across every configured symbol:
up to `LOTTO_SWEEP_PER_CYCLE=26` non-promoted names per minute, due again after
`LOTTO_SWEEP_MINUTES=3`. Up to `LOTTO_CHAIN_CAPACITY=60` eligible developing names
receive minute bars and chains each cycle, plus previously alerted names for
outcome tracking. At normal request speed, the 106-name chain sweep completes
in roughly two cycles when 60 names are deep. SPY, QQQ and IWM retain deep slots when they have 0/1-day expirations
through `LOTTO_CONTINUOUS_SYMBOLS`; the remaining slots rotate across the universe.
The log reports actual coverage within the configured sweep window in
minutes; request failures or slow responses can lengthen that interval.

Promotion uses price movement, range, stock-volume acceleration, **and** new
option volume in three neighboring strikes of one expiry and side. Sweep
observations alone never issue alerts. A new flow event can enter the deeper
minute-level pool on the next cycle, where the same stock, liquidity, fresh
quote, and persistence rules apply. Leases preserve up to 25 minutes of option
history. All quote discovery and chain-coverage observations are recorded.
Names outside the deep pool have sampled chain coverage rather than minute-level
option history, so an intracycle move can still be missed.

Independent chain and history requests now use six workers with paced request starts
and retries for Schwab throttling. Actual completed deep-chain count and cycle time
must be checked under market-open load; a configured capacity is not proof that
every chain was fetched. Before the open, historical volume baselines are loaded
sixteen names per cycle. One scanner process and a persistent volume are required.

## Developing setups

The same symmetric call/put rules apply to every name:

- Opening break: the first ten-minute range breaks in the direction of the open,
  prior close and VWAP.
- Opening drive: five completed bars can establish early directional momentum,
  VWAP acceptance and a recent pivot; the shorter options baseline requires
  three active strikes, a triggered setup and a higher score.
- Pullback resumption: a meaningful earlier impulse, measurable retracement,
  local recovery and room to retest the earlier extreme relative to invalidation.
- Level reclaim: acceptance through the completed opening range, premarket or
  previous-session boundary. Round numbers are displayed as context.
- Reversal: VWAP is reclaimed or lost with directional acceleration. A bullish
  reversal need not already be above yesterday's close. A local pivot and EMA
  turn can also identify an intraday recovery before reaching session VWAP.
- Coiled continuation: a prior impulse followed by a 30-, 60- or 120-minute
  tight range, VWAP acceptance and sustained options activity.
- Continuation: directional efficiency, VWAP acceptance and proximity to the
  current session extreme; a recovery can use its recent pivot before reaching
  the old high/low.

The scanner records **POTENTIAL TRADE WATCH** internally for developing setups,
but only **ACTIVE TRADE IDEA** posts to the main Discord webhook. When a watch
later qualifies as a triggered setup, the active idea gets its own time and
fresh option ask and can link to the watch ID. A setup already triggered when
first found sends an active idea directly. The watch-to-active transition bypasses
same-leg duplicate check; a fresh price leg or a recorded reset can alert again
without a fixed ticker timer. Internal watches remain in nightly research and do not count as
Discord deliveries.

The active Discord embed shows the contract, ask/bid, stock price, trigger,
invalidation, research score, and supportive sector/market or peer context.
Scores of 90–91 get a trophy title; scores of 92+ get trophy and fire. These
scores are research rules, not calibrated probabilities. Stock and options
activity metrics remain recorded for research but are omitted from the embed.
TSM maps to SMH; QQQ uses SPY rather than itself.
Peer confirmation excludes the target. Relative ETF leadership can qualify
against a flat benchmark.

Stock volume can qualify through either high cumulative same-time pace or a
local five-minute volume burst accompanied by ATR expansion. A quieter-day
recovery may instead qualify through improving local stock volume, directional
progress and stronger multi-strike option acceleration. Baselines use prior
sessions only. ATR uses LOTTO_ATR_DAYS prior true ranges (21 by default, at least
15; set 14 to restore the previous average). A name with fewer than 15 ranges
gets an empty prior_atr on the 21-day window, not the 14-day average. Thresholds remain uncalibrated
research defaults; the historical case study is not proof of an edge.

During the first 45 completed bars, a triggered opening drive, opening break,
level reclaim or continuation can also qualify through broad **absolute**
options volume when the opening impulse reached 0.2 ATR, the stock remains at
least 0.15 ATR from the open and strong against VWAP, and local stock volume is
accelerating. This narrowly handles steady strong flow that fails a relative
options-acceleration test.
The usual score, independent context, spread, trigger/invalidation, short-history
and two-bar confirmation checks still apply. No ticker-specific exception is used.

Options evidence requires synchronized fresh quotes, multiple neighboring active
strikes, ten minutes of comparable counters and either acceleration or sustained
twenty-minute activity. A four-minute baseline is available only with a triggered,
three-strike setup and a higher score; two-strike candidates need stronger
acceleration. The early broad-flow path can use high absolute call/put volume
across at least four neighboring strikes instead of relative acceleration.
Coils may use a fresh burst or sustained activity. Volume-counter resets,
missing observations and large time gaps invalidate that evidence. Chains do
**not** establish ask-side buying, aggressor direction or opening-position intent.

Contract selection checks spread, price, delta, gamma, moneyness and DTE.
By default, lotto candidates are limited to options expiring today or tomorrow;
the full configured universe remains in price and option-chain discovery.
SPX is included even when `LOTTO_UNIVERSE` overrides the defaults. Schwab's
`$SPX` index prices and PM-settled `SPXW` options are scanned under the same
contract and alert rules. Because the index has no traded share volume in its
minute candles, SPX volume-based features use time-aligned SPY share volume;
saved SPX snapshots identify `SPY` as the volume source. Alerts display the
actual option root and label the underlying as an index. Standard SPX option
contracts are excluded, and nightly opportunity summaries split SPXW alerts
from other contracts for separate review.
Phase-one scoring uses price structure, stock/option activity, breadth, entry
location, independent context, liquidity and a capped delta/gamma response
comparison. The default threshold is 70/100. Strike migration is recorded but
does not demand trading farther-out strikes. Scores are not comparable to the
old 87-point-maximum formula and are not calibrated probabilities.

The normal ask ceiling remains $2.50; sufficiently large prior ATR can expand
it to `min($7.50, 0.25 × ATR)`. The minimum ask is $0.10 and minimum absolute
delta is 0.10. A chosen out-of-the-money strike must be within one prior ATR.
Spreads still must satisfy **both** 15% of ask and $0.15 absolute limits. The
delta/gamma ranking proxy holds IV and time fixed; it is not a return forecast.

## Alert restraint and observability

There are no daily, per-cycle, or per-ticker alert-count ceilings by default;
`LOTTO_MAX_ALERTS_PER_DAY`, `LOTTO_MAX_ALERTS_PER_CYCLE`, and
`LOTTO_MAX_ALERTS_PER_TICKER` are `0` (unlimited). Posted cards do cool down:
at most one Discord card per underlying and side (CALL or PUT) inside
`LOTTO_COOLDOWN_MINUTES` (default 8; `0` disables it). The first card keeps
the slot. A later card for that same ticker and side is still scored and
saved, and the decision reason is `cooldown: TICKER SIDE posted Xs ago`, but
it is not sent. The other side is not held. SPX and SPXW share one slot. The
clock is the last posted card, kept for the session and reloaded from today's
posted alerts when the worker starts.

One minute of confirmation requires two distinct
completed bars; poll jitter no longer adds another minute. Same-leg
deduplication governs a new alert, including a new attempt in the
same contract. A distinct price-level break or a recorded reset can alert again
while the ticker is still running. The earliest possible opening signal is
approximately 9:36 ET with complete, timely observations; collection delays can
make it later. New ideas stop 15 minutes before the actual close. In the final
30 minutes, only triggered setups with delta at least 0.20, spread at most 10%,
limited extension and a higher score can qualify.

A developing setup must be within 0.06 ATR of its trigger. A triggered idea
cannot be more than 0.25 ATR beyond it. Pullback ideas need at least as much
stock-price room to the previous extreme as to their invalidation. These are
first-phase hypotheses, not statistically proven cutoffs.

Logs show the whole-universe quote count, promoted chain count, missing-data
conditions, and recent rejection reasons. Decisions persist the setup, features,
score, contract and blockers. All evaluated expiry/side candidates also retain
the strategy version and full settings. Ranking rewards rising scores; materially falling
scores cannot alert. An empty alert day is investigated through those records,
not solved by manufacturing signals.

## Nightly review

The worker automatically reviews the session five minutes after Schwab's actual
close, including early closes. Completion is persisted; a restart can catch up
the most recent captured session outside market hours.

The review downloads minute history for **every configured name**, including
unpromoted names, and evaluates price prefixes without future input leakage.
It records early-session feature landmarks for all names, labels eventual
near-high/near-low closes, and measures stock returns after observed multi-strike
flow events so missed patterns can be compared with alerted ones.
It saves failed and successful price hypotheses, captured candidate features,
subsequent 5/15/30/60-minute stock returns, rejection reasons and discovery
coverage. Reports are written to `DATA_DIR/nightly/YYYY-MM-DD/report.json`.
This establishes a research feedback loop; it does not automatically rewrite
live thresholds or claim to be a trained predictive model. Nightly feedback keeps
potential and active alerts separate, and measures each active idea from its own
alert time. It retrieves later one-minute option trade highs for both phases so
the research can measure price potential without treating a watch as an active
trade. Missing option trade bars remain unavailable rather than counted as a
failure. Existing pre-upgrade alerts are labeled `LEGACY` and never backfilled as
active ideas.

Captured option returns use entry ask to subsequent bid, fresh deduplicated
quotes and actual quote timestamps. Reports distinguish observed intervals from
open-to-close coverage. The latter is unavailable without quotes in the first
and last session minutes. Zero bids remain valid -100% observations. No final
chain snapshot can reconstruct unrecorded intraday option quotes or peak returns.
Returns are sampled quotes, not actual executions.

Alert feedback additionally records the first sampled doubling, first sampled
stock invalidation, and best sampled bid before that invalidation. Trader
feedback remains separate from measured outcomes and can be recorded by ID:

```bash
python3 -m lotto.main feedback --alert-id ID --outcome mixed --notes "First partial worked; remainder faded"
```

Offline report from captured observations:

```bash
python3 -m lotto.main nightly --db /app/data/live.sqlite3 --day YYYY-MM-DD
```

Offline reports do not mark the automatic full-universe job complete. Daily
cohorts count only the first symbol/side/setup observation, rather than treating
overlapping minute samples as independent trials. No automatic parameter
promotion occurs.

## Railway

- GitHub: `devin23jordan23/lotto-alerts`, branch `main`.
- Dockerfile and `railway.json` run `python -m lotto.main live`.
- One replica, persistent volume at `/app/data`, `DATA_DIR=/app/data`.
- Shared authentication: `SCHWAB_TOKEN_BROKER_URL` and
  `SCHWAB_TOKEN_BROKER_KEY`. Direct client/refresh credentials remain supported.
- Dedicated channel: `DISCORD_LOTTO_WEBHOOK`.
- Set `LOTTO_SEND_ALERTS=true` for delivery. False is shadow observation.
- Set the complete `LOTTO_UNIVERSE`; see [.env.example](.env.example).

The worker logs a startup preflight. It reads quotes, a sample chain, session
hours, database integrity and webhook metadata. It sends no test message.
The manual equivalent is:

```bash
python3 -m lotto.main check
```

An after-hours preflight verifies configuration and connectivity, not intraday
freshness or strategy performance. Snapshots and archived bars are compressed;
older uncompressed snapshots remain readable. Raw observations accumulate; monitor volume
usage and archive older sessions. Historical baseline caches are rebuilt after
restart.

Discord messages are queued before delivery. Uncertain sends are not
automatically repeated; expired, failed and uncertain states remain visible.
Potential watches stay internal; only active ideas are posted to Discord.
Milestones and nightly research remain in the database/reports.

## Verification

Python 3.11+ on macOS/Linux; standard library only.

```bash
python3 -m unittest discover -s tests -v
python3 -m lotto.main demo --db /tmp/lotto-demo.sqlite3
python3 -m lotto.main replay --input observations.jsonl --db /tmp/lotto-replay.sqlite3
```

Demo and replay never send to Discord. Use a fresh database for independent
experiments. Replay JSONL contains arrays of `Snapshot.to_dict()` observations,
one array per cycle. All timestamps need offsets.

Case-study research: [September 21–22 review](docs/session-research-2026-09-22.md).
Phase-one release: [October 2 validation and rollout](docs/phase-one-release-2026-10-02.md).
