# Lotto scanner: setup and event audit (28 October 2026)

## Decision

The repair on `codex/setup-confirmation-repair` is a review branch. It fixes a
reproducible same-bar stock-event loss and records strategy confirmation in
candidate metrics. `LOTTO_SETUP_CONFIRMATION_ENABLED=0` keeps the new posting
gate off. **Do not enable the proposed gate from this replay:** its 15-minute
hit rate was lower than the held set on the available older data. PR #19,
which this branch builds on, is also unmerged. Nothing here is live.

## What the current code does

| Stage | Code | Consequence |
| --- | --- | --- |
| Poll | `lotto/main.py::main`, `lotto/schwab.py::poll` | A 60-second loop gets bulk stock quotes, runs discovery, then spends up to 27 seconds on chain sweep and a separate deep-fetch budget. A stock event can be observed before its option chain is ready. This is polling, so a sub-minute alert requires a second evaluation when a new quote or event arrives. |
| Candidate | `lotto/features.py::candidates` | The traditional option path needs the same contract through a five/ten-minute observation window, active neighboring strikes, volume acceleration and contract selection. Those are strong contract-history assumptions and can suppress an otherwise real stock move. |
| Setup | `lotto/patterns.py::detect_setup` | Opening drive, pullback, level reclaim, opening break, coil, reversal and continuation share one detector. A returned `Setup` means a pattern was detected; it does not consistently mean that a fresh cross, volume and renewed push happened at the card timestamp. |
| Score | `lotto/confluence.py::additive_features`, `apply_card_layers`, `config/vince_additive_weights_v4c.json` | Market, flow, spread, VWAP and EMA terms sum into a post score. With the new gate off, `post_ok` still depends on a score floor, positive-row count and contract gate. Those additive points cannot reliably distinguish a flat EMA touch from a resumed move. The old `level_break` feature can stay true after a morning break; this branch adds a fresh version only behind the off switch. |
| Event handoff | `lotto/engine.py::process_model_v`, `lotto/stock_thesis.py` | The pre-fix engine skipped a second snapshot when its last completed bar was unchanged, even if a stock event first arrived inside that bar. The branch reevaluates a newly arrived event and logs the same-bar test. PR #19 separately uses a nearest-expiry, near-money contract after a stock thesis. |
| Send | `lotto/discord.py::deliver_pending` | PR #18/19 requotes at send time and withholds a card if a fresh quote is unavailable. The displayed entry must be graded from the executable ask at send, not an earlier chain ask. |

These findings are from code. They do not prove that any one blocker caused a
specific October 28 miss; the current session's full decision and option-bid
paths were unavailable to this local replay.

## Architecture borrowed from the seven projects

| Reference | Reusable pattern for this scanner |
| --- | --- |
| [LEAN consolidators](https://www.quantconnect.com/docs/v2/writing-algorithms/consolidating-data/getting-started) | Keep quote observations and completed bar updates as distinct events. Consolidate a bar once, then evaluate the current quote against levels known at that timestamp. |
| [NautilusTrader data and strategies](https://nautilustrader.io/docs/latest/concepts/data/) | Carry event time and receive time separately; route stock quotes, bars and option quotes to a stateful setup evaluator. |
| [Freqtrade lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/) | Replay the exact time-visible feature set and test for future-data leakage before trusting a rule. |
| [StockSharp strategy rules](https://github.com/StockSharp/StockSharp) | Model a setup as a rule with activation, confirmation, expiry and rearm, instead of one Boolean that remains true for the session. |
| [vectorbt portfolio signals](https://vectorbt.dev/api/portfolio/base/) | Sweep candidate thresholds in research and grade entries from contemporaneous asks against subsequent bids. Preserve the full result matrix, including winners excluded. |
| [Backtrader operating model](https://www.backtrader.com/docu/operating/) | Make warmup explicit and handle more than one update within a bar without treating it as a duplicate completed bar. |
| [alpaca-py live stock data](https://alpaca.markets/sdks/python/api_reference/data/stock/live.html) | Keep trade/quote and bar event paths distinct. This SDK is a data interface reference, not a source of profitable strategy thresholds. |

These projects offer event and test patterns; none establishes a 1.5 RVOL,
1-2 ATR displacement, five-second quote age, or a target win rate for this
scanner. Those numbers require this scanner's own out-of-sample data.

## Setup state and Boolean prerequisites

The branch adds a pure `Confirmation` result with `detected`, `confirmed` and
`expired`, `event_at`, reason and five-bar time-of-day volume ratio. It is
separate from contract choice and additive rank. The proposed conditions are:

* **PD/PM EMA continuation:** side-appropriate PDH/PMH or PDL/PML context on
  the last two completed bars **AND** a prior-bar EMA9 touch **AND** close
  back on the trend side of EMA9 **AND** an upward/downward EMA slope **AND**
  renewed price beyond the prior bar's directional edge **AND** a measured
  five-bar time-of-day volume ratio. A touch alone remains `detected`.
* **Fresh level or range break:** a crossing of this setup's own trigger in
  the last three completed bars or a newer valid stock quote **AND** renewed
  side-aligned price movement **AND** participation. Opening range, PM, PD,
  HOD/LOD, intraday base and local range may qualify independently; PD/PM and
  EMA are not universal gates.
* **Opening drive:** within the opening window, side-aligned move from open,
  recent movement and participation.
* **Pullback/continuation:** direction from open, renewed movement and
  participation; the pullback family additionally needs a move through the
  preceding bar's directional edge.

Any market, sector, option flow or spread row ranks a **confirmed** card. It
does not make the mandatory stock conditions true. The current candidate
detector still owns the initial `Setup`; a future live version must connect
the state machine to every posting path, including stock events, and use
event-specific rearming. The numeric thresholds in this draft are hypotheses,
not approved production settings.

## Timestamp examples (illustrative, not observed October 28 trades)

* 09:36:00: a 09:35 completed bar tags EMA9 below PDH; no reclaimed bar yet.
  EMA continuation is `detected`; a strong flow score cannot post it under an
  enabled strategy gate.
* 09:37:12: a newer stock quote crosses the already-known opening range high;
  the last completed bars establish direction and participation. The opening
  range strategy can confirm immediately, without waiting for a PDH break.
* 09:45:00: an old premarket-low cross occurred at 09:32 and price is now
  merely retesting the low. The level-break strategy is `expired` until a
  fresh cross or a separately confirmed pullback event appears.

## Read-only replay, and why the new gate is off

`python3 scripts/replay_setup_confirmation.py` reads the local forensic DB
in read-only mode. It uses actual sent `ACTIVE`/`LEGACY` alerts from 25
September through 16 October 2026, before Model V. Outcome is a later
observed bid at least 1.4 times the alert ask within the horizon. It is an
opportunity metric, not a fill or realized return. Three rows lacked setup
data and were omitted.

| Proposed group | Alerts | +40% in 5m | +40% in 10m | +40% in 15m |
| --- | ---: | ---: | ---: | ---: |
| Confirmed | 236 | 21 (8.9%) | 35 (14.8%) | 54 (22.9%) |
| Held | 194 | 16 (8.2%) | 36 (18.6%) | 47 (24.2%) |
| All covered | 430 | 37 (8.6%) | 71 (16.5%) | 101 (23.5%) |

Turning this gate on would remove 194/430 sent ideas, including 47 of the
101 15-minute winners. It would reduce covered alert volume 45.1% while
reducing 15-minute winners 46.5%, and the kept hit rate would be lower than
baseline. This is a rejection of the **combined draft gate**, not evidence
against every component. For example, within `LEVEL_RECLAIM`, 54 confirmed
alerts contained 16 15-minute winners, while 54 old/absent-cross alerts
contained 10. This small conditional result is a lead for a new replay.

The available export does not contain a current Model V full-engine replay or
all October 28 decision/quote paths. Thus it cannot report today's exact
hit rate, detection delay for every missed mover, incremental alert volume,
or executable option outcomes under the new code. The event-loss unit test
proves the scheduling fix for one same-bar case, not those trading metrics.

## Repair sequence

1. Keep the same-bar event repair and shadow confirmation metrics in a review
   branch. Both are covered by tests; leave `LOTTO_SETUP_CONFIRMATION_ENABLED=0`.
2. Replay the full current engine with timestamped stock, bar, chain and
   send-time quotes from the recent sessions. Produce one row per candidate:
   detected time, confirmed time, chain availability, option selected,
   send-time ask/bid, 5/10/15-minute max bid and adverse bid, suppression
   reason and setup family. Compare against the existing model without
   hindsight and count missed fast movers separately.
3. Change one strategy prerequisite at a time. Keep a condition only if it
   removes losing cards while preserving enough +40% and +100% opportunities
   across more than one session and does not delay fresh movers. Then build
   event-specific rearming and an explicit option selection fallback.
4. Only after that validation, choose a live gate and deploy it. Continue to
   require a fresh executable quote on the posted option card.

