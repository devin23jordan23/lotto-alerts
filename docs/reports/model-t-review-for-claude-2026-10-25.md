# Model T review for Claude and Andre — Oct 25, 2026

**Status:** research branch only. No Model T production code, flag, deployment,
or alert behavior was changed. Source spec:
`/Users/owner/Downloads/MODEL-T-SPEC-2026-10-25.md` rev 1.1.

## Decision

Keep Model T's architecture: discover stock setups first, merge independent
option-flow discoveries, rank the **stock** move, and attach a contract last.
Do **not** ship the rev 1.1 hard-veto list or A/B cut points unchanged. The
spec itself says those tiers did not separate clean winners in H1. My
independent alert-cohort replay finds the proposed pace-climax veto would
remove many large winners.

## Reproducible stop-first check

`scripts/model_t_set_a_replay.py` ran read-only against the local archived
`audit.sqlite3`. It found 340 sent ACTIVE alerts from Oct 7–16 with a matching
candidate feature row and 30-minute quote path. Entry is the alert ask;
subsequent sampled bids must be fresh, with +40% occurring before -28% or
-35%. This is a **sent-alert subset**, not Claude's 1,217 deduplicated
opportunities or a replay of newly discovered stock events. H1 has 71 alerts,
H2 has 269. The complete machine-readable result is in
`docs/reports/model-t-set-a-alert-cohort-2026-10-25.json`.

| Rule removed from sends | Removed | Removed clean +40 before -28 | +100% paths removed | Kept clean rate |
|---|---:|---:|---:|---:|
| Intraday reversal | 11 | 1 | 0 | 30.4% |
| Against open, excluding REVERSAL | 17 | 1 | 0 | 31.0% |
| 1DTE | 89 | 16 | 3 | 33.9% |
| Local RVOL < 0.8 | 19 | 3 | 2 | 30.5% |
| **Pace persistence > 1.015** | **117** | **36** | **15** | **29.1%** |

Baseline is 340 alerts, 29.7% clean before -28%, 34.4% clean before -35%,
and 47 contract paths reaching +100% at any point within 30 minutes.
The pace-climax removed group was **30.8% clean** versus **29.1% kept**;
it contains a META +176%, PLTR +232%, AMD +200%, and 12 other +100%
paths. A blanket pace veto moves in the wrong direction on this cohort.

The proposed five-veto stack keeps 139 alerts at 39.6% clean, but removes
**18** +100% paths and **46** clean winners. Removing the pace veto from that
stack keeps 227 alerts at 36.1% clean while losing only **5** +100% paths.
That is a much better winner-preservation trade-off, but it is still an
in-sample filter on existing alerts and cannot establish Model T's full
performance. The reversal/against-open pair removes 18 alerts, loses no
+100% paths, and removes one clean winner; the sample is too small to make
either a universal veto without the full opportunity replay.

The spec's A rule (trend >=0.4 ATR, VWAP distance >=0.3 ATR, RVOL >=0.8)
graded 25.0% clean in H1 (n=20) and 38.1% in H2 (n=113). The proposed B
rule had n=7 and n=13. These are not stable tier cut points. Sustained
20-minute option activity in this **sent-alert subset** did not add clean
separation to A in H1 (25% versus 25%); Claude's larger blocked-study
comparison remains a separate hypothesis and should be reconciled row by row.

## Stock-event sanity check

`scripts/model_t_stock_replay.py` independently parsed 63,263 archived
stock-path rows from Sep 25–Oct 16. Twelve sessions had enough observations
to grade an event. It used as-of prior-day, premarket, 3-minute ORB,
5-minute ORB, fresh session extreme, and base-break levels. Dedupe is by
symbol/side/setup/0.05-ATR level. The **stock-only** outcome is +0.25 ATR
before -0.15 ATR within 30 minutes. This is deliberately **not** an option
win-rate estimate, and it does not include Oct 19/20/23.

| Raw stock event | Events | Further +0.25 ATR before -0.15 ATR |
|---|---:|---:|
| PUSH | 550 | 21.5% |
| Prior-day level cross | 124 | 21.0% |
| Premarket level cross | 183 | 20.8% |
| ORB3 cross | 117 | 18.0% |
| ORB5 cross | 107 | 14.0% |
| Base break | 207 | 9.7% |

These raw trigger rates show why **level touch/break alone cannot become an
ACTIVE alert**. They also show that the current 0.25-ATR PUSH and 15-minute
base rule are coverage generators, not complete quality rules. The next
test must add as-of acceptance, range location, displacement, and market
context one at a time, while naming every big mover each addition loses.
Machine-readable details: `docs/reports/model-t-stock-events-2026-10-25.json`.

## Changes to the build spec before code promotion

1. Make `STOCK THESIS` and `ACTIVE TRADE IDEA` distinct timestamps. A stock
   level break can create a thesis without a contract. An ACTIVE card needs
   measured stock continuation/acceptance and a current quote; its option
   outcome begins at its own ask. An option defect cannot erase the thesis.
2. Use prior-day high/low, premarket high/low, ORB3/5, current day high/low,
   daily-range position, and 15/30/60-minute bases **as-of each bar**.
   The current live `stock_first.py` does not consume all these levels.
3. Treat reversal, 1DTE, low RVOL, and pace as candidate features/labels,
   not production vetoes. In particular, reject the blanket pace-climax
   veto pending a setup-specific, held-out result. Keep the stock thesis
   visible even when a priced option is poor.
4. Tier A/B must be fitted on stop-first outcomes and held-out days.
   Do not copy the 91/81 Hybrid score or the rev 1.1 provisional stock
   thresholds into production. Options flow may add confluence **after**
   stock qualification; its weight needs the full study, not this subset.
5. No fixed cooldown and no hard alerts/day cap. Dedupe the same event and
   level; a new level or post-pullback leg may alert immediately.

## Required proof before live switch

Replay the four full-snapshot sessions Oct 16/19/20/23 for Model S, Hybrid
replica, broad stock events, PUSH+BASE, and Model T. Grade the selected
contract's event-time ask and +2-minute ask against fresh bids at
5/10/15/30/60 minutes and close, +40% before -28%/-35%, and +100%/+300%.
Show first eligible minute, sends/day, every +100% winner lost, and the
Oct 23 PANW/CRWD/NET paths. Select cut points on three days and score the
fourth; rotate. The test must count an unpriced stock watch separately from
a priced ACTIVE idea. Only then should the model run in shadow alongside
live Model S before a production switch.
