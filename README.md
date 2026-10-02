# Lotto Alerts

Selective intraday **potential** options ideas using Schwab, Railway and Discord.
No orders are placed. Scores are research rules, not probabilities or promised returns.

## Universe and collection

The default universe is the same 102 names as the unusual-options scanner.
Set `LOTTO_UNIVERSE` explicitly in Railway; an old environment value overrides
the code default. No case-study ticker list replaces the configured universe.

Every cycle requests stock quotes for the **whole universe** in one batch.
The worker also rotates `/chains` requests across every configured symbol:
up to `LOTTO_SWEEP_PER_CYCLE=26` non-promoted names per minute, due again after
`LOTTO_SWEEP_MINUTES=3`. Up to `LOTTO_CHAIN_CAPACITY=24` developing names
receive minute bars and chains each cycle, plus previously alerted names for
outcome tracking. At normal request speed, the 102-name chain sweep completes
in roughly three cycles. SPY, QQQ and IWM retain deep slots throughout the session
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

A 27-second sweep budget and 54-second total collection budget prevent slow
responses from silently aging out most observations. Unfinished names resume
next cycle. Before the open, historical volume baselines are loaded sixteen names
per cycle. One worker and a persistent volume are required.

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

Alerts distinguish **DEVELOPING** from **TRIGGERED**, with a trigger, invalidation,
stock price, returns from open/prior close, stock volume, options activity and
independent market/sector context. TSM maps to SMH; QQQ uses SPY rather than itself.
Peer confirmation excludes the target. Relative ETF leadership can qualify
against a flat benchmark.

Stock volume can qualify through either high cumulative same-time pace or a
local five-minute volume burst accompanied by ATR expansion. A quieter-day
recovery may instead qualify through improving local stock volume, directional
progress and stronger multi-strike option acceleration. Baselines use prior
sessions only. ATR uses 14 prior true ranges. Thresholds remain uncalibrated
research defaults; the historical case study is not proof of an edge.

Options evidence requires synchronized fresh quotes, multiple neighboring active
strikes, ten minutes of comparable counters and either acceleration or sustained
twenty-minute activity. A four-minute baseline is available only with a triggered,
three-strike setup and a higher score; two-strike candidates need stronger
acceleration. Coils may use a fresh burst or sustained activity. Volume-counter resets,
missing observations and large time gaps invalidate that evidence. Chains do
**not** establish ask-side buying, aggressor direction or opening-position intent.

Contract selection checks spread, price, delta, gamma, moneyness and DTE.
By default, lotto candidates are limited to options expiring today or tomorrow; all 102
underlyings remain in the stock and option-chain discovery universe.
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
`LOTTO_MAX_ALERTS_PER_TICKER` are `0` (unlimited). One minute of confirmation requires two distinct
completed bars; poll jitter no longer adds another minute. A 30-minute ticker
cooldown and a fresh reset or direction change govern a new leg, including a
new attempt in the same contract. The earliest possible opening signal is
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
live thresholds or claim to be a trained predictive model.

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
Only potential ideas are posted; milestones and nightly research remain in
the database/reports.

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
