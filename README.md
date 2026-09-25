# Lotto Alerts

Selective intraday **potential** options ideas using Schwab, Railway and Discord.
No orders are placed. Scores are research rules, not probabilities or promised returns.

## Universe and collection

The default universe is the same 102 names as the unusual-options scanner.
Set `LOTTO_UNIVERSE` explicitly in Railway; an old environment value overrides
the code default. No case-study ticker list replaces the configured universe.

Every cycle requests stock quotes for the **whole universe** in one batch.
The worker also rotates `/chains` requests across every configured symbol:
up to `LOTTO_SWEEP_PER_CYCLE=24` non-promoted names per minute, due again after
`LOTTO_SWEEP_MINUTES=5`. Up to `LOTTO_CHAIN_CAPACITY=18` developing names
receive minute bars and chains each cycle, plus previously alerted names for
outcome tracking. At normal request speed, the 102-name chain sweep completes
in roughly four cycles. The log reports the actual count covered within five
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
next cycle. Before the open, historical volume baselines are loaded eight names
per cycle. One worker and a persistent volume are required.

## Developing setups

The same symmetric call/put rules apply to every name:

- Opening break: the first ten-minute range breaks in the direction of the open,
  prior close and VWAP.
- Reversal: VWAP is reclaimed or lost with directional acceleration. A bullish
  reversal need not already be above yesterday's close.
- Coiled continuation: a prior impulse followed by a 30-, 60- or 120-minute
  tight range, VWAP acceptance and sustained options activity.
- Continuation: directional efficiency, VWAP acceptance and proximity to the
  current session extreme.

Alerts distinguish **BUILDING** from **TRIGGERED**, with a trigger, invalidation,
stock price, returns from open/prior close, stock volume, options activity and
independent market/sector context. TSM maps to SMH; QQQ uses SPY rather than itself.
Peer confirmation excludes the target. Relative ETF leadership can qualify
against a flat benchmark.

Stock volume can qualify through either high cumulative same-time pace or a
local five-minute volume burst accompanied by ATR expansion. Baselines use prior
sessions only. ATR uses 14 prior true ranges. Thresholds remain uncalibrated
research defaults; the historical case study is not proof of an edge.

Options evidence requires synchronized fresh quotes, multiple neighboring active
strikes, ten minutes of comparable counters and either acceleration or sustained
twenty-minute activity. Coils require the sustained path. Volume-counter resets,
missing observations and large time gaps invalidate that evidence. Chains do
**not** establish ask-side buying, aggressor direction or opening-position intent.

Contract selection checks spread, price, delta, gamma, moneyness and DTE.
Unavailable signed-flow and catalyst evidence receives no points: the research
score's maximum is 87/100, with a default threshold of 72.

## Alert restraint and observability

Defaults: five ideas per day, one per cycle, two per ticker, two minutes of
confirmation across three distinct completed bars, 45-minute ticker cooldown and a fresh
reset/new contract for re-alerting. The earliest possible opening signal is
approximately 9:44 ET if observations begin at the open and all gates qualify.
New ideas stop 30 minutes before the actual session close.

Logs show the whole-universe quote count, promoted chain count, missing-data
conditions, and recent rejection reasons. Decisions persist the setup, features,
score, contract and blockers. Ranking rewards rising scores; materially falling
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
